import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from leadparser.config import BlankTrailConfig, OutreachConfig, load_config
from leadparser.llm.base import LLMError
from conftest import isolate_blanktrail_ca_cert_env

def test_blanktrail_ca_cert_defaults_to_none():
    """Находка 1: сертификат — необязательное поле, по умолчанию не проверяем."""
    assert BlankTrailConfig().ca_cert is None

def test_ca_cert_resolves_from_env_when_not_set_in_yaml(monkeypatch, tmp_path: Path):
    """Путь к сертификату — машинно-зависимый, как остальные секреты этого
    конфига: если не задан в YAML, берём его из переменной окружения, а не
    коммитим в config.yaml (тот путь, что был закоммичен, ломал чистый чекаут:
    сам файл лежит в certs/, а он в .gitignore)."""
    cert = tmp_path / "blanktrail-ca.pem"
    cert.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("BLANKTRAIL_CA_CERT", str(cert))
    assert BlankTrailConfig().ca_cert == cert

def test_ca_cert_explicit_yaml_value_wins_over_env(monkeypatch, tmp_path: Path):
    yaml_cert = tmp_path / "yaml.pem"
    monkeypatch.setenv("BLANKTRAIL_CA_CERT", str(tmp_path / "env.pem"))
    assert BlankTrailConfig(ca_cert=yaml_cert).ca_cert == yaml_cert

def test_ca_cert_stays_none_without_env_or_yaml(monkeypatch):
    """Чистый чекаут без .env: не настроено — не проверяем, как и раньше."""
    monkeypatch.delenv("BLANKTRAIL_CA_CERT", raising=False)
    assert BlankTrailConfig().ca_cert is None

def test_ca_cert_env_name_is_itself_configurable(monkeypatch, tmp_path: Path):
    cert = tmp_path / "custom.pem"
    cert.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("MY_CUSTOM_CERT_VAR", str(cert))
    assert BlankTrailConfig(ca_cert_env="MY_CUSTOM_CERT_VAR").ca_cert == cert


class _FakeNode:
    """Минимальная замена pytest.Item: только то, что читает сама фикстура."""

    def __init__(self, integration: bool):
        self._integration = integration

    def get_closest_marker(self, name: str):
        return object() if name == "integration" and self._integration else None


def test_isolate_ca_cert_fixture_exempts_integration_marked_nodes(monkeypatch):
    """Автоюз-фикстура (conftest.py) обязана оставить BLANKTRAIL_CA_CERT в покое
    для integration-тестов. `pytest -m 'not integration'` (наш дефолтный прогон)
    просто не собирает их — деселект по маркеру фикстуры не касается вовсе, она
    отрабатывает для любого теста, который реально выполняется. Но стоит
    запустить `pytest -m integration` (ровно то, что документирует модульный
    докстринг test_integration.py), и без этого исключения
    test_reddit_listing_through_blanktrail получил бы `verify=True` вместо
    настоящего сертификата BlankTrail — и упал бы на проверке TLS раньше
    первого запроса. Прежде путь был захардкожен в config.yaml и integration-
    тест это не задевало; теперь, когда он в окружении, изоляция обязана
    уважать эту границу.

    Дергаем функцию фикстуры напрямую через `__wrapped__` (pytest начиная с
    9.x запрещает звать `@pytest.fixture`-функции как обычные — `Failed:
    Fixture "..." called directly`), а не через `pytester`: это не новая
    зависимость, `__wrapped__` — тот же механизм, которым сам pytest хранит
    исходную функцию, и здесь проверяется именно её ветвление, а не
    интеграция с системой фикстур pytest.
    """
    raw_fixture = isolate_blanktrail_ca_cert_env.__wrapped__
    monkeypatch.setenv("BLANKTRAIL_CA_CERT", "/real/machine/blanktrail-ca.pem")

    integration_request = SimpleNamespace(node=_FakeNode(integration=True))
    raw_fixture(integration_request, monkeypatch)
    assert os.environ["BLANKTRAIL_CA_CERT"] == "/real/machine/blanktrail-ca.pem"

    plain_request = SimpleNamespace(node=_FakeNode(integration=False))
    raw_fixture(plain_request, monkeypatch)
    assert os.environ["BLANKTRAIL_CA_CERT"] == ""


def test_blanktrail_proxy_credentials_default_to_none():
    """Находка 3: авторизация порта — необязательные поля."""
    cfg = BlankTrailConfig()
    assert cfg.proxy_user is None
    assert cfg.proxy_pass_env is None

def test_loads_default_config(config_path: Path):
    cfg = load_config(config_path)
    # 2, а не 7: инструмент рассчитан на ежедневный запуск (двойка, а не
    # единица — чтобы пропущенный день не оставлял слепую зону).
    assert cfg.lookback_days == 2
    assert "n8n" in cfg.reddit.subreddits
    assert cfg.reddit.request_interval_s == 4.0
    assert cfg.reddit.listing_limit == 100
    assert cfg.blanktrail.port == 20180
    assert cfg.llm.concurrency == 3
    assert cfg.llm.stage("prefilter").model == "haiku"
    assert cfg.llm.stage("prefilter").batch_size == 25
    assert cfg.llm.stage("score").model == "sonnet"
    assert cfg.llm.stage("verify").model == "opus"
    assert cfg.llm.stage("verify").threshold == 70
    assert cfg.llm.backend_conf("claude_code").type == "claude_code"
    assert cfg.llm.backend_conf("deepseek").type == "openai_compat"

def test_dead_subreddits_removed_from_real_config(config_path: Path):
    """Первый полный прогон (1465 постов за неделю): у этих — ни одного лида.
    Основания разные (совсем без свежих постов / большая выборка без лидов /
    малая выборка без кандидатов), но список из брифа удаляется целиком."""
    cfg = load_config(config_path)
    removed = {
        # Мёртвые: свежайшая запись 24-65 дней назад.
        "RequestABot", "jobbit", "hireaprogrammer", "NoCodeSaaS", "programmingrequests",
        # Большая выборка (по 100 постов), ноль лидов.
        "hiring", "Upwork", "SideProject",
        # Малая выборка, но кандидатов ноль.
        "PythonJobs", "reactjs", "javascript", "indiehackers", "freelance", "GeeLark",
    }
    present = set(cfg.reddit.subreddits)
    assert not (removed & present), f"не удалены: {removed & present}"
    assert len(present) == 23


def test_undecided_subreddits_kept_in_real_config(config_path: Path):
    """Лидов не было, но были кандидаты — одной недели мало, чтобы судить."""
    cfg = load_config(config_path)
    for kept in ("Entrepreneur", "ProxyEngineering", "Playwright"):
        assert kept in cfg.reddit.subreddits


def test_unknown_stage_raises_llm_error(config_path: Path):
    """B2: голый KeyError не ловил никто — он ронял прогон в статусе running."""
    cfg = load_config(config_path)
    with pytest.raises(LLMError):
        cfg.llm.stage("nope")

def test_api_key_read_from_env(monkeypatch, config_path: Path):
    monkeypatch.setenv("BLANKTRAIL_API_KEY", "secret-123")
    cfg = load_config(config_path)
    assert cfg.blanktrail.api_key == "secret-123"


INCOMPLETE_CONFIG = """
llm:
  backends:
    claude_code: {type: claude_code}
  stages:
    prefilter: {backend: claude_code, model: haiku, effort: low, batch_size: 25}
    score: {backend: claude_code, model: sonnet, effort: medium}
"""


def test_missing_stage_is_rejected_at_load_time(tmp_path: Path):
    """B2: отсутствующий `verify` обязан всплыть до того, как за всё заплачено.

    Раньше опечатка в `llm.stages` доживала до конца скоринга и вылетала голым
    KeyError, оставляя прогон в статусе running и без отчёта.
    """
    path = tmp_path / "config.yaml"
    path.write_text(INCOMPLETE_CONFIG, encoding="utf-8")
    with pytest.raises(LLMError, match="verify"):
        load_config(path)


def test_unknown_stage_name_is_rejected_at_load_time(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        INCOMPLETE_CONFIG
        + "    verify: {backend: claude_code, model: opus, effort: high}\n"
        + "    scorer: {backend: claude_code, model: opus, effort: high}\n",
        encoding="utf-8",
    )
    with pytest.raises(LLMError, match="scorer"):
        load_config(path)


def test_unknown_backend_lookup_raises_llm_error(config_path: Path):
    cfg = load_config(config_path)
    with pytest.raises(LLMError):
        cfg.llm.backend_conf("nope")


def test_outreach_config_defaults_are_safe_for_configs_without_the_section():
    """Существующие config.yaml (в т.ч. в тестах) не знают про outreach —
    загрузка не должна на этом падать."""
    cfg = OutreachConfig()
    assert cfg.reddit_username is None
    assert cfg.goal_rub == 0
    assert cfg.rates_to_rub == {}


def test_outreach_config_uppercases_currency_codes():
    """`won --currency aud` и конфиг `rates_to_rub: {usd: 85}` не должны молча
    разъезжаться в регистре и терять курс."""
    cfg = OutreachConfig(rates_to_rub={"usd": 85})
    assert cfg.rates_to_rub == {"USD": 85}


def test_loads_outreach_section_from_real_config(config_path: Path):
    cfg = load_config(config_path)
    assert cfg.outreach.reddit_username == "Slow-Plate4355"
    assert cfg.outreach.goal_rub == 15000
    assert cfg.outreach.rates_to_rub == {"USD": 85}
