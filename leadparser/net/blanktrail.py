from __future__ import annotations

import math
import ssl
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..config import BlankTrailConfig

RETRY_429_DELAYS = (30, 60, 120)
RETRY_5XX_ATTEMPTS = 3
# Reddit отдаёт -used/-remaining/-reset на КАЖДЫЙ ответ (и 429, и 200); -reset
# — точные секунды до сброса окна. Ждать ровно столько почти всегда даёт успех
# с первой попытки — слепая лестница ниже спит куда дольше и была главной
# причиной трёхчасового сбора.
RATE_LIMIT_RESET_HEADER = "x-ratelimit-reset"
RATE_LIMIT_REMAINING_HEADER = "x-ratelimit-remaining"
RATE_LIMIT_WAIT_MARGIN_S = 1.0  # запас поверх -reset: часы на двух концах не синхронны
RATE_LIMIT_MAX_WAIT_S = 180.0  # потолок за раз — кривой заголовок не усыпит прогон на час
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# Без явного заголовка httpx отправляет «python-httpx/…» — ровно на том
# маршруте, который блокируют охотнее всего. BlankTrail со spoof_user_agent
# может подменить его своим; здесь важно не отдать наружу дефолт клиента.
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": USER_AGENT,
}
# Подстроки из типичных сообщений об ошибке проверки сертификата — по ним
# отличаем «BlankTrail вскрыл TLS, а httpx ему не доверяет» от обычного обрыва
# соединения, который имеет смысл повторить.
CERT_ERROR_MARKERS = ("certificate", "ssl", "certificate_verify_failed")


class BlankTrailError(RuntimeError):
    """Проблема с локальным BlankTrail или с запросом через него."""


def _looks_like_certificate_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in CERT_ERROR_MARKERS)


def _parse_rate_limit_seconds(raw: str | None) -> float | None:
    """`x-ratelimit-reset`/`-remaining`: строка, может быть дробной (`'0.0'` —
    валидный ноль, не «заголовка нет»). Мусор (не число, отрицательное,
    NaN/inf) — None, вызывающий откатывается к лестнице вместо падения или зависания.
    """
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _rate_limit_wait_s(resp: httpx.Response) -> float | None:
    """`x-ratelimit-reset` + запас, не больше RATE_LIMIT_MAX_WAIT_S. None —
    заголовка нет или он не годится, вызывающий откатывается к RETRY_429_DELAYS."""
    seconds = _parse_rate_limit_seconds(resp.headers.get(RATE_LIMIT_RESET_HEADER))
    if seconds is None:
        return None
    return min(seconds + RATE_LIMIT_WAIT_MARGIN_S, RATE_LIMIT_MAX_WAIT_S)


class BlankTrail:
    def __init__(self, cfg: BlankTrailConfig, http: httpx.Client | None = None) -> None:
        if cfg.ca_cert is not None and not cfg.ca_cert.exists():
            raise BlankTrailError(
                f"Файл сертификата BlankTrail не найден: {cfg.ca_cert}. Скачайте "
                "его кнопкой «CA Certificate» в шапке дашборда BlankTrail и "
                "укажите путь в blanktrail.ca_cert."
            )
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=15.0)

    @property
    def proxy_url(self) -> str:
        # Находка 3: если в настройках BlankTrail включена авторизация самого
        # прокси-порта, адрес без user:pass@ получит отказ на каждом запросе.
        if self.cfg.proxy_user and self.cfg.proxy_password:
            # quote(..., safe=""): без него '/', '#', '?' или голый '%' в логине
            # или пароле ломают разбор URL самим httpx (например, httpx.InvalidURL:
            # Invalid port от '/' в пароле) — а это не httpx.HTTPError, и run() её
            # не поймает.
            user = quote(self.cfg.proxy_user, safe="")
            password = quote(self.cfg.proxy_password, safe="")
            return f"http://{user}:{password}@127.0.0.1:{self.cfg.port}"
        return f"http://127.0.0.1:{self.cfg.port}"

    def ensure_port(self) -> int:
        payload: dict[str, Any] = {"port": self.cfg.port, **self.cfg.profile}
        url = f"{self.cfg.api_base.rstrip('/')}/ports/open"
        try:
            resp = self._http.post(
                url, json=payload, headers={"X-API-Key": self.cfg.api_key}
            )
        except httpx.HTTPError as exc:
            raise BlankTrailError(
                f"BlankTrail недоступен по адресу {self.cfg.api_base}. "
                "Запустите приложение BlankTrail и повторите."
            ) from exc
        if resp.status_code == 409:
            return self.cfg.port
        if resp.status_code == 401:
            raise BlankTrailError(
                f"BlankTrail отклонил API-ключ (401). Проверьте {self.cfg.api_key_env} в .env"
            )
        if resp.status_code >= 400:
            raise BlankTrailError(
                f"BlankTrail не открыл порт {self.cfg.port}: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        return int(resp.json().get("port", self.cfg.port))

    def license_status(self) -> dict[str, Any]:
        """`plan`, `js_solver_max_procs`, `allowed_domains` — для предполётной
        диагностики (находка 4). Любая ошибка здесь — BlankTrailError; вызывающий
        сам решает, что это только предупреждение, а не повод остановить прогон."""
        url = f"{self.cfg.api_base.rstrip('/')}/license/status"
        try:
            resp = self._http.get(url, headers={"X-API-Key": self.cfg.api_key})
        except httpx.HTTPError as exc:
            raise BlankTrailError(f"не удалось запросить {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise BlankTrailError(f"{resp.status_code} от {url}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise BlankTrailError(f"не JSON в ответе {url}: {resp.text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise BlankTrailError(f"ответ {url} не объект: {resp.text[:200]!r}")
        return data


class ThrottledClient:
    """HTTP-клиент через прокси с фиксированным интервалом между запросами."""

    def __init__(
        self,
        proxy_url: str | None,
        interval_s: float,
        timeout_s: float = 30.0,
        verify: bool | str = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.interval_s = interval_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        kwargs: dict[str, Any] = {
            "timeout": timeout_s, "headers": DEFAULT_HEADERS, "verify": verify,
        }
        if transport is not None:
            kwargs["transport"] = transport
        elif proxy_url:
            kwargs["proxy"] = proxy_url
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def _wait_turn(self) -> None:
        if self._last_request_at is None or self.interval_s <= 0:
            self._last_request_at = self._monotonic()
            return
        elapsed = self._monotonic() - self._last_request_at
        if elapsed < self.interval_s:
            self._sleep(self.interval_s - elapsed)
        self._last_request_at = self._monotonic()

    def _wait_out_exhausted_quota(self, resp: httpx.Response) -> None:
        """remaining≈0 на успешном ответе — ждём -reset здесь, а не после
        гарантированного 429 на следующем запросе (заголовок есть на обоих)."""
        remaining = _parse_rate_limit_seconds(resp.headers.get(RATE_LIMIT_REMAINING_HEADER))
        if remaining is None or remaining > 0:
            return
        wait_s = _rate_limit_wait_s(resp)
        if wait_s is not None:
            self._sleep(wait_s)

    def _get(self, url: str) -> httpx.Response:
        rate_limit_hits = 0
        server_errors = 0
        while True:
            self._wait_turn()
            try:
                resp = self._http.get(url)
            except (httpx.HTTPError, ssl.SSLError) as exc:
                if _looks_like_certificate_error(exc):
                    # Не исчезнет от повторной попытки — только от исправленного
                    # blanktrail.ca_cert, так что не тратим на неё бюджет ретраев.
                    raise BlankTrailError(
                        "BlankTrail вскрывает TLS (MITM), и httpx не доверяет его "
                        "сертификату. Укажите путь к корневому сертификату "
                        f"BlankTrail в blanktrail.ca_cert. Исходная ошибка: {exc}"
                    ) from exc
                server_errors += 1
                if server_errors >= RETRY_5XX_ATTEMPTS:
                    raise BlankTrailError(f"Сетевая ошибка при запросе {url}: {exc}") from exc
                self._sleep(5 * server_errors)
                continue
            if resp.status_code == 429:
                if rate_limit_hits >= len(RETRY_429_DELAYS):
                    raise BlankTrailError(
                        f"429 от {url} после {rate_limit_hits} попыток — "
                        "увеличьте reddit.request_interval_s"
                    )
                # Бюджет попыток (len(RETRY_429_DELAYS)=3) не меняем: с точным
                # ожиданием повтор почти всегда удаётся с первого раза; лестница
                # — резерв на случай отсутствующего или негодного заголовка.
                wait_s = _rate_limit_wait_s(resp)
                if wait_s is None:
                    wait_s = RETRY_429_DELAYS[rate_limit_hits]
                self._sleep(wait_s)
                rate_limit_hits += 1
                continue
            if resp.status_code >= 500:
                server_errors += 1
                if server_errors >= RETRY_5XX_ATTEMPTS:
                    raise BlankTrailError(f"{resp.status_code} от {url}")
                self._sleep(5 * server_errors)
                continue
            if resp.status_code >= 400:
                raise BlankTrailError(
                    f"{resp.status_code} от {url}: {resp.text[:200]}"
                )
            self._wait_out_exhausted_quota(resp)
            return resp

    def get_json(self, url: str) -> Any:
        return self._get(url).json()

    def get_text(self, url: str) -> str:
        """Сырой текст ответа — Reddit RSS отдаёт Atom-XML, а не JSON."""
        return self._get(url).text
