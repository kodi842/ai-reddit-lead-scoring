from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from .llm.base import LLMError

REQUIRED_STAGES = ("prefilter", "score", "verify")


class RedditConfig(BaseModel):
    # RSS отдаёт 429 быстрее, чем отдавал JSON: на замерах — уже после полутора
    # десятков быстрых запросов. 2.5 с, которых хватало JSON-эндпоинту, здесь мало.
    request_interval_s: float = 4.0
    # Максимум, который принимает Reddit; пагинации в `.rss` нет.
    listing_limit: int = 100
    max_messages_per_thread: int = 100
    subreddits: list[str] = Field(default_factory=list)


class BlankTrailConfig(BaseModel):
    api_base: str = "http://127.0.0.1:8891/api/v1"
    api_key_env: str = "BLANKTRAIL_API_KEY"
    port: int = 20180
    # BlankTrail вскрывает TLS (MITM); httpx проверяет сертификаты по набору
    # certifi, а не по связке ключей macOS, так что установка сертификата в
    # системный Keychain кнопкой дашборда нашему коду не помогает — нужен путь
    # к файлу. Путь свой на каждой машине — как и остальные секреты этого
    # конфига, он не коммитится в config.yaml, а берётся из переменной
    # окружения (см. ca_cert_env), если не задан явно.
    ca_cert: Path | None = None
    ca_cert_env: str | None = "BLANKTRAIL_CA_CERT"
    # Авторизация самого прокси-порта (если включена в настройках BlankTrail) —
    # без неё запросы получат отказ. Пароль — из окружения, как остальные
    # секреты, и никогда не хранится и не печатается в открытом виде.
    proxy_user: str | None = None
    proxy_pass_env: str | None = None
    profile: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _resolve_ca_cert_from_env(self) -> "BlankTrailConfig":
        """Явное значение в YAML всегда главнее — переменная окружения только
        подставляет то, что в общий, закоммиченный config.yaml класть нельзя.
        """
        if self.ca_cert is None and self.ca_cert_env:
            value = os.environ.get(self.ca_cert_env)
            if value:
                self.ca_cert = Path(value)
        return self

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def proxy_password(self) -> str:
        return os.environ.get(self.proxy_pass_env, "") if self.proxy_pass_env else ""


class HeuristicConfig(BaseModel):
    stop_phrases: list[str] = Field(default_factory=list)
    stop_authors: list[str] = Field(default_factory=list)


class BackendConfig(BaseModel):
    type: str
    base_url: str | None = None
    api_key_env: str | None = None

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


class StageConfig(BaseModel):
    backend: str
    model: str
    effort: str = "medium"
    batch_size: int = 1
    threshold: int = 0


class LLMConfig(BaseModel):
    concurrency: int = 3
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    stages: dict[str, StageConfig] = Field(default_factory=dict)

    def stage(self, name: str) -> StageConfig:
        if name not in self.stages:
            raise LLMError(f"нет настроек для ступени {name!r} в llm.stages")
        return self.stages[name]

    def backend_conf(self, name: str) -> BackendConfig:
        if name not in self.backends:
            raise LLMError(f"нет бэкенда {name!r} в llm.backends")
        return self.backends[name]

    def check_stages(self) -> None:
        """Ступени проверяем на загрузке, а не на первом обращении.

        Пропущенный `verify` раньше всплывал уже после того, как сбор,
        предфильтр и скоринг были оплачены: `stage()` бросал голый KeyError
        в середине прогона.
        """
        missing = [name for name in REQUIRED_STAGES if name not in self.stages]
        unknown = [name for name in self.stages if name not in REQUIRED_STAGES]
        if missing or unknown:
            raise LLMError(
                "llm.stages должен содержать ровно "
                f"{', '.join(REQUIRED_STAGES)}; не хватает: {missing or '—'}, "
                f"лишние: {unknown or '—'}"
            )


class ReportConfig(BaseModel):
    min_score: int = 55
    max_context_messages: int = 20
    context_message_chars: int = 300


class OutreachConfig(BaseModel):
    # Без логина автопроверке не с кем сравнивать авторов в ленте RSS.
    reddit_username: str | None = None
    goal_rub: float = 0
    rates_to_rub: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _upper_currency_codes(self) -> "OutreachConfig":
        self.rates_to_rub = {code.upper(): rate for code, rate in self.rates_to_rub.items()}
        return self


class Config(BaseModel):
    lookback_days: int = 7
    db_path: Path = Path("data/leads.sqlite")
    reports_dir: Path = Path("reports")
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    blanktrail: BlankTrailConfig = Field(default_factory=BlankTrailConfig)
    heuristic: HeuristicConfig = Field(default_factory=HeuristicConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)


def load_config(path: Path) -> Config:
    load_dotenv()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    cfg.llm.check_stages()
    for stage in cfg.llm.stages.values():
        cfg.llm.backend_conf(stage.backend)
    return cfg
