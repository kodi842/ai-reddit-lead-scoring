import inspect
import ssl
from pathlib import Path

import httpx
import pytest

from leadparser.config import BlankTrailConfig, load_config
from leadparser.net import blanktrail as blanktrail_module
from leadparser.net.blanktrail import BlankTrail, BlankTrailError, ThrottledClient


def test_ensure_port_opens_port():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"port": 20180, "status": "open"})

    cfg = BlankTrailConfig(port=20180, profile={"protocol": "http", "mode": "db"})
    bt = BlankTrail(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bt.ensure_port() == 20180
    assert seen["url"].endswith("/ports/open")
    assert '"port": 20180' in seen["body"] or '"port":20180' in seen["body"]
    assert '"mode"' in seen["body"]


def test_ensure_port_tolerates_conflict():
    handler = lambda request: httpx.Response(409, json={"error": "port taken"})
    cfg = BlankTrailConfig(port=20180)
    bt = BlankTrail(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bt.ensure_port() == 20180


def test_ensure_port_raises_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cfg = BlankTrailConfig(port=20180)
    bt = BlankTrail(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlankTrailError, match="BlankTrail"):
        bt.ensure_port()


def test_ensure_port_raises_on_401():
    handler = lambda request: httpx.Response(401, json={"error": "bad key"})
    cfg = BlankTrailConfig(port=20180)
    bt = BlankTrail(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlankTrailError, match="API-?ключ|401"):
        bt.ensure_port()


def test_proxy_url():
    assert BlankTrail(BlankTrailConfig(port=20181)).proxy_url == "http://127.0.0.1:20181"


# Находка 3: если в настройках BlankTrail включена авторизация прокси-порта,
# адрес без user:pass@ получит отказ на каждом запросе.


def test_proxy_url_includes_credentials_when_configured(monkeypatch):
    monkeypatch.setenv("BT_PROXY_PASSWORD", "s3cr3t")
    cfg = BlankTrailConfig(port=20134, proxy_user="user", proxy_pass_env="BT_PROXY_PASSWORD")
    assert BlankTrail(cfg).proxy_url == "http://user:s3cr3t@127.0.0.1:20134"


def test_proxy_url_omits_credentials_when_password_env_is_empty(monkeypatch):
    monkeypatch.delenv("BT_PROXY_PASSWORD", raising=False)
    cfg = BlankTrailConfig(port=20134, proxy_user="user", proxy_pass_env="BT_PROXY_PASSWORD")
    assert BlankTrail(cfg).proxy_url == "http://127.0.0.1:20134"


def test_proxy_url_omits_credentials_when_user_is_not_set(monkeypatch):
    monkeypatch.setenv("BT_PROXY_PASSWORD", "s3cr3t")
    cfg = BlankTrailConfig(port=20134, proxy_pass_env="BT_PROXY_PASSWORD")
    assert BlankTrail(cfg).proxy_url == "http://127.0.0.1:20134"


def test_proxy_url_percent_encodes_special_characters_in_credentials(monkeypatch):
    """Без percent-encoding '/', '#', '?' и голый '%' в логине/пароле ломают разбор
    адреса самим httpx (например, '/' в пароле даёт httpx.InvalidURL: Invalid port —
    httpx.InvalidURL не наследует httpx.HTTPError, и run() её не ловит: голый
    traceback ровно того класса, который чинит находка 1, только с другой стороны).
    """
    user = "u/ser"
    password = "p/a#s?s%wd"
    monkeypatch.setenv("BT_PROXY_PASSWORD", password)
    cfg = BlankTrailConfig(port=20134, proxy_user=user, proxy_pass_env="BT_PROXY_PASSWORD")
    url = BlankTrail(cfg).proxy_url

    parsed = httpx.URL(url)  # не должно бросать httpx.InvalidURL
    assert parsed.username == user
    assert parsed.password == password

    client = ThrottledClient(proxy_url=url, interval_s=0)  # тоже не должно бросать
    client.close()


def test_real_config_disables_port_idle_timeout(config_path: Path):
    """Находка 2: без idle_seconds=0 порт закрывается сам через 30 минут простоя.

    Сбор (~15 мин трафика к Reddit) → предфильтр (десятки минут одних вызовов
    модели без единого запроса к Reddit) → скачивание комментариев: к этому
    моменту порт из настоящего config.yaml обязан ещё быть открыт."""
    cfg = load_config(config_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"port": cfg.blanktrail.port, "status": "open"})

    # cfg.blanktrail.ca_cert здесь всегда None, что бы ни лежало в реальном .env
    # этой машины: автоюз-фикстура isolate_blanktrail_ca_cert_env (conftest.py)
    # не даёт load_dotenv() подставить туда значение, так что тест не зависит
    # от существования настоящего файла сертификата — под проверкой один idle_seconds.
    bt = BlankTrail(cfg.blanktrail, http=httpx.Client(transport=httpx.MockTransport(handler)))
    bt.ensure_port()
    assert '"idle_seconds": 0' in seen["body"] or '"idle_seconds":0' in seen["body"]


def test_get_json_throttles_between_requests():
    calls = []
    slept = []
    clock = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    client = ThrottledClient(
        proxy_url=None, interval_s=2.5, sleep=sleep, monotonic=lambda: clock["t"],
        transport=httpx.MockTransport(handler),
    )
    client.get_json("https://www.reddit.com/r/n8n/new.json")
    client.get_json("https://www.reddit.com/r/n8n/new.json?after=t3_x")
    assert len(calls) == 2
    assert slept and slept[0] == pytest.approx(2.5, abs=0.01)


def test_get_json_backs_off_on_429_then_succeeds():
    responses = [429, 429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down")

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [30, 60]


def test_get_json_raises_after_429_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="429"):
        client.get_json("https://www.reddit.com/x.json")


# Reddit присылает x-ratelimit-used/-remaining/-reset на каждый ответ.
# -reset — точное число секунд до сброса окна, и это надёжнее слепой лестницы
# RETRY_429_DELAYS, которая гадает и обычно спит куда дольше необходимого.


def test_get_json_waits_exact_seconds_from_rate_limit_reset_header():
    """Точное ожидание по заголовку заменяет первый шаг лестницы (30 с), когда
    Reddit сам говорит, что окно короче."""
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down", headers={"x-ratelimit-reset": "5"})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    # 5 с из заголовка + секундный запас — не 30 с из лестницы.
    assert slept == [pytest.approx(6.0)]


def test_get_json_parses_fractional_reset_header_including_zero():
    """'0.0' — не то же самое, что «заголовка нет»: наивная проверка `if value`
    приняла бы 0.0 за ложь и откатилась бы на лестницу вместо секундного запаса."""
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down", headers={"x-ratelimit-reset": "0.0"})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [pytest.approx(1.0)]


def test_get_json_caps_rate_limit_wait_at_180_seconds():
    """Кривой (или просто огромный) x-ratelimit-reset не должен усыплять прогон
    на час — потолок ожидания за раз держит паузу в разумных рамках."""
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down", headers={"x-ratelimit-reset": "3600"})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [180.0]


def test_get_json_falls_back_to_ladder_on_malformed_reset_header():
    """Мусор в заголовке (не число) не должен ронять запрос — откат на
    прежнюю лестницу, как если бы заголовка не было вовсе."""
    responses = [429, 429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(
            429, text="slow down", headers={"x-ratelimit-reset": "soon(tm)"}
        )

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [30, 60]


def test_get_json_falls_back_to_ladder_on_negative_reset_header():
    """Отрицательное значение — тоже мусор, а не «уже можно», и не должно
    трактоваться как отрицательная (нулевая) пауза."""
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down", headers={"x-ratelimit-reset": "-5"})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [30]


def test_get_json_falls_back_to_ladder_on_nan_reset_header():
    """`float('nan')` не бросает ValueError — Python парсит строку 'nan' как
    настоящий NaN. Без явной проверки на конечность NaN проходит мимо `< 0`
    (сравнения с NaN всегда ложны) и ломает потолок ожидания: `min(nan, 180)`
    равен `nan`, а `time.sleep(nan)` бросает ValueError прямо в проде."""
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": "fine"})
        return httpx.Response(429, text="slow down", headers={"x-ratelimit-reset": "nan"})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [30]


def test_get_json_waits_before_next_request_when_quota_exhausted():
    """x-ratelimit-remaining приходит и на успешных ответах: если он у нуля,
    разумнее выждать x-ratelimit-reset сразу, чем гарантированно поймать 429
    на следующем запросе."""
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": "fine"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3"},
        )

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == [pytest.approx(4.0)]


def test_get_json_does_not_wait_when_quota_remains():
    """Платить временем нужно только когда лимит реально исчерпан — не при
    любом положительном remaining."""
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": "fine"},
            headers={"x-ratelimit-remaining": "5", "x-ratelimit-reset": "3"},
        )

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"data": "fine"}
    assert slept == []


def test_get_json_retries_server_error():
    codes = [500, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        code = codes.pop(0)
        return httpx.Response(code, json={"ok": True} if code == 200 else None)

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_json("https://www.reddit.com/x.json") == {"ok": True}


def test_get_text_retries_429_and_returns_raw_body():
    """Reddit RSS отдаёт Atom-XML, а не JSON: источнику нужен сырой текст.

    `get_text` обязан пройти через тот же троттлинг/ретраи, что и `get_json`,
    а не завести отдельную, ничем не проверенную копию этой логики.
    """
    responses = [429, 200]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        if code == 200:
            return httpx.Response(200, text="<feed>ok</feed>")
        return httpx.Response(429, text="slow down")

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    assert client.get_text("https://www.reddit.com/comments/x/.rss") == "<feed>ok</feed>"
    assert slept == [30]


def test_get_json_raises_on_403():
    handler = lambda request: httpx.Response(403, text="blocked")
    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="403"):
        client.get_json("https://www.reddit.com/x.json")


def test_get_json_exhausts_connection_error_budget():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        raise httpx.ConnectError("boom", request=request)

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="Сетевая ошибка"):
        client.get_json("https://www.reddit.com/x.json")
    assert len(calls) == 3


def test_get_json_raises_after_three_500s():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(500, text="server error")

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="500"):
        client.get_json("https://www.reddit.com/x.json")
    assert len(calls) == 3


# Находка 4: /license/status — предполётная диагностика (план, allowed_domains,
# js_solver_max_procs), не условие запуска.


def test_license_status_returns_parsed_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={"plan": "enterprise", "js_solver_max_procs": 4, "allowed_domains": []},
        )

    cfg = BlankTrailConfig(api_key_env="BT_KEY")
    bt = BlankTrail(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    status = bt.license_status()
    assert status == {"plan": "enterprise", "js_solver_max_procs": 4, "allowed_domains": []}
    assert seen["url"].endswith("/license/status")


def test_license_status_raises_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    bt = BlankTrail(BlankTrailConfig(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlankTrailError):
        bt.license_status()


def test_license_status_raises_on_bad_status():
    handler = lambda request: httpx.Response(500, text="boom")
    bt = BlankTrail(BlankTrailConfig(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlankTrailError, match="500"):
        bt.license_status()


def test_license_status_raises_on_invalid_json():
    handler = lambda request: httpx.Response(200, content=b"not json")
    bt = BlankTrail(BlankTrailConfig(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlankTrailError):
        bt.license_status()


def test_requests_carry_a_browser_user_agent():
    """C3: без явного UA наружу уходит `python-httpx/...` — на самом
    блокируемом маршруте, запросе к Reddit через прокси."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json={"ok": True})

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda _s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    client.get_json("https://www.reddit.com/r/n8n/new.json")
    assert "Mozilla" in seen["ua"]
    assert "httpx" not in seen["ua"].lower()


# Находка 1: BlankTrail вскрывает TLS (MITM); httpx доверяет набору certifi, а
# не связке ключей macOS, так что без пути к сертификату каждый запрос к
# Reddit падает на проверке сертификата раньше, чем доходит до Reddit.


def test_blanktrail_rejects_missing_ca_cert_file(tmp_path: Path):
    missing = tmp_path / "blanktrail-ca.pem"
    cfg = BlankTrailConfig(port=20180, ca_cert=missing)
    with pytest.raises(BlankTrailError, match="сертификат"):
        BlankTrail(cfg)


def test_blanktrail_accepts_existing_ca_cert_file(tmp_path: Path):
    cert = tmp_path / "blanktrail-ca.pem"
    cert.write_text("dummy pem contents", encoding="utf-8")
    cfg = BlankTrailConfig(port=20180, ca_cert=cert)
    BlankTrail(cfg)  # не должно бросать


def test_throttled_client_verify_defaults_to_true():
    """Без явного verify поведение не должно измениться: проверка сертификатов
    остаётся включённой, если не указано иное."""
    sig = inspect.signature(ThrottledClient.__init__)
    assert sig.parameters["verify"].default is True


def test_throttled_client_forwards_verify_to_httpx_client(monkeypatch):
    """CLI передаёт str(cfg.blanktrail.ca_cert) в этот параметр — здесь проверяем,
    что ThrottledClient действительно доносит его до httpx.Client, а не роняет."""
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr(blanktrail_module.httpx, "Client", FakeHttpxClient)
    ThrottledClient(proxy_url=None, interval_s=0, verify="/path/to/blanktrail-ca.pem")
    assert captured["verify"] == "/path/to/blanktrail-ca.pem"


def test_get_json_raises_clear_error_for_certificate_problem():
    """Сертификатную ошибку не имеет смысла ретраить трижды с паузами — она не
    исчезнет сама, пока не поправят blanktrail.ca_cert."""
    calls = []
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1000)",
            request=request,
        )

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=slept.append, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="ca_cert"):
        client.get_json("https://www.reddit.com/x.json")
    assert len(calls) == 1
    assert slept == []


def test_get_json_raises_clear_error_for_raw_ssl_error():
    """Без этого перехвата ssl.SSLError не является httpx.HTTPError и улетает
    наружу голым стектрейсом мимо всей обработки ошибок get_json."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise ssl.SSLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain"
        )

    client = ThrottledClient(
        proxy_url=None, interval_s=0, sleep=lambda s: None, monotonic=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlankTrailError, match="ca_cert"):
        client.get_json("https://www.reddit.com/x.json")
