# tests/test_cli.py — CLI без сети, LLM и настоящего пайплайна.
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from leadparser import cli
from leadparser.models import Lead, Post, ThreadAnalysis
from leadparser.net.blanktrail import BlankTrail
from leadparser.pipeline import RunResult
from leadparser.storage import Storage

runner = CliRunner()
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}

CONFIG_TEMPLATE = """
lookback_days: 7
db_path: {db}
reports_dir: {reports}
reddit:
  subreddits: [n8n]
llm:
  concurrency: 1
  backends:
    claude_code: {{type: claude_code}}
  stages:
    prefilter: {{backend: claude_code, model: haiku, effort: low, batch_size: 25}}
    score: {{backend: claude_code, model: sonnet, effort: medium}}
    verify: {{backend: claude_code, model: opus, effort: high, threshold: 70}}
report:
  min_score: 55
"""


@pytest.fixture()
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(
            db=tmp_path / "leads.sqlite", reports=tmp_path / "reports"
        ),
        encoding="utf-8",
    )
    return path


class DummyClient:
    def close(self) -> None:
        return None


@pytest.fixture()
def fake_pipeline(monkeypatch):
    box: dict = {"result": RunResult(run_id=1, status="ok", stats={})}

    class FakePipeline:
        def __init__(self, cfg, storage, sources, backends, **kwargs):
            box["cfg"] = cfg
            box["kwargs"] = kwargs

        def run(self, max_posts=None, write_html=True):
            box["max_posts"] = max_posts
            return box["result"]

    monkeypatch.setattr(cli, "build_backends", lambda llm_cfg: {})
    monkeypatch.setattr(cli, "_build_sources", lambda cfg: ({}, DummyClient()))
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    return box


def stats_with(**over) -> dict:
    base = {
        "collected": 0, "skipped": 0, "comments_fetched": 0,
        "source_targets": 3, "source_failures": 0, "failed_sources": [],
        "all_sources_failed": False,
    }
    base.update(over)
    return base


def test_run_exits_1_when_every_subreddit_failed(cfg_file: Path, fake_pipeline):
    """A1: полный провал сбора — это код 1 и заметные строки, а не зелёный «ok»."""
    fake_pipeline["result"] = RunResult(run_id=1, status="ok", stats=stats_with(
        source_failures=3, all_sources_failed=True,
        failed_sources=["r/webdev: 403", "r/n8n: 403", "r/hiring: 403"],
    ))
    result = runner.invoke(cli.app, ["run", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 1
    assert "r/webdev" in result.output


def test_run_exits_0_when_collection_worked_but_found_nothing(cfg_file: Path, fake_pipeline):
    """A1: «всё сработало, нового нет» обязано остаться кодом 0."""
    fake_pipeline["result"] = RunResult(run_id=1, status="ok", stats=stats_with())
    result = runner.invoke(cli.app, ["run", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 0


def test_lookback_zero_is_not_silently_ignored(cfg_file: Path, fake_pipeline):
    """B5: `--lookback 0` («только сегодняшние») — валидное значение."""
    result = runner.invoke(
        cli.app, ["run", "--config", str(cfg_file), "--lookback", "0"], env=ENV
    )
    assert result.exit_code == 0
    assert fake_pipeline["cfg"].lookback_days == 0


def seed(db_path: Path, ages: dict[str, int]) -> None:
    storage = Storage(db_path)
    run_id = storage.start_run()
    for pid, days in ages.items():
        post = Post(
            thread_id=f"reddit:{pid}", source_key="reddit:n8n",
            url=f"https://www.reddit.com/r/n8n/comments/{pid}/", title="Need a bot",
            body="paying", author="bob", author_url="https://www.reddit.com/user/bob",
            created_at=NOW - timedelta(days=days), flair=None, num_comments=1,
            stickied=False, removed=False, raw_json={},
        )
        storage.upsert_post(post)
        storage.save_analysis(post.thread_id, "scorer_v1", True, ThreadAnalysis(
            problem_title_ru="Нужен бот", status="open",
            leads=[Lead(
                username="bob", score=82, quote="paying", quote_ru="платим",
                lang="en", risk_level="green", risk_note="своя задача",
                why="прямой заказ", offer="предложить бота",
            )],
        ), run_id)
    storage.close()


def newest_report(reports_dir: Path) -> str:
    files = sorted(reports_dir.glob("threads_*.html"))
    assert files, f"отчёт не создан в {reports_dir}"
    return files[-1].read_text(encoding="utf-8")


def test_report_since_filters_by_thread_age(cfg_file: Path, tmp_path: Path):
    """A3: у `report` есть `--since` из §9 спеки, и он режет по дате треда."""
    seed(tmp_path / "leads.sqlite", {"fresh1": 1, "old1": 30})
    result = runner.invoke(
        cli.app,
        ["report", "--config", str(cfg_file), "--since", "2026-09-15"],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    html = newest_report(tmp_path / "reports")
    assert "comments/fresh1/" in html
    assert "comments/old1/" not in html


def test_report_without_since_keeps_every_thread(cfg_file: Path, tmp_path: Path):
    seed(tmp_path / "leads.sqlite", {"fresh1": 1, "old1": 30})
    result = runner.invoke(cli.app, ["report", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 0, result.output
    html = newest_report(tmp_path / "reports")
    assert "comments/fresh1/" in html
    assert "comments/old1/" in html


def test_run_exits_1_when_a_stage_failed(cfg_file: Path, fake_pipeline):
    """B2: сорвавшаяся ступень — код 1 и внятная строка, а не тихий успех."""
    fake_pipeline["result"] = RunResult(run_id=1, status="error", stats=stats_with(
        error="бэкенд 'его-нет' не создан", failed_at="verify",
    ))
    result = runner.invoke(cli.app, ["run", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 1
    assert "verify" in result.output


def test_run_drives_a_stage_progress_bar(cfg_file: Path, fake_pipeline, monkeypatch):
    """C4: CLI отдаёт пайплайну колбэк прогресса и рисует по нему ступени."""
    seen: list[tuple[str, int, int | None]] = []

    class ProgressingPipeline:
        def __init__(self, cfg, storage, sources, backends, **kwargs):
            self.progress = kwargs.get("progress")

        def run(self, max_posts=None, write_html=True):
            assert self.progress is not None, "CLI не передал колбэк прогресса"
            for done in (1, 2):
                self.progress("prefilter", done, 2)
                seen.append(("prefilter", done, 2))
            return RunResult(run_id=1, status="ok", stats=stats_with())

    monkeypatch.setattr(cli, "Pipeline", ProgressingPipeline)
    result = runner.invoke(cli.app, ["run", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 0, result.output
    assert seen == [("prefilter", 1, 2), ("prefilter", 2, 2)]
    assert "предфильтр" in result.output


def test_run_prints_failures_but_exits_0_when_something_was_collected(
    cfg_file: Path, fake_pipeline
):
    """Частичный сбой показываем красным, но кодом возврата не врём."""
    fake_pipeline["result"] = RunResult(run_id=1, status="ok", stats=stats_with(
        collected=3, source_failures=3, all_sources_failed=False,
        failed_sources=["r/webdev: 429", "r/n8n: 429", "r/hiring: 429"],
    ))
    result = runner.invoke(cli.app, ["run", "--config", str(cfg_file)], env=ENV)
    assert result.exit_code == 0
    assert "r/webdev" in result.output


def test_broken_yaml_is_a_red_line_not_a_traceback(cfg_file: Path, tmp_path: Path):
    """Конфиг, который не читается, — это сообщение владельцу, а не трейсбек."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("llm: [не, отображение\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["stats", "--config", str(broken)], env=ENV)
    assert result.exit_code == 1
    assert "Конфиг не годится" in result.output


def test_config_failing_validation_is_a_red_line(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("lookback_days: не число\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["stats", "--config", str(bad)], env=ENV)
    assert result.exit_code == 1
    assert "Конфиг не годится" in result.output


def test_build_sources_passes_ca_cert_path_as_verify(monkeypatch, tmp_path: Path):
    """Находка 1: путь к сертификату обязан дойти до httpx как verify, а не
    просто осесть в конфиге."""
    cert = tmp_path / "blanktrail-ca.pem"
    cert.write_text("dummy", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(db=tmp_path / "leads.sqlite", reports=tmp_path / "reports")
        + f"blanktrail:\n  ca_cert: {cert}\n",
        encoding="utf-8",
    )
    cfg = cli._load_config(config_path)
    monkeypatch.setattr(BlankTrail, "ensure_port", lambda self: self.cfg.port)
    # check_license звонит на настоящий локальный BlankTrail, если его не подменить —
    # ensure_port тут ни при чём, это отдельный вызов сразу после него.
    monkeypatch.setattr(cli, "check_license", lambda *a, **k: None)
    captured: dict = {}

    class SpyThrottledClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "ThrottledClient", SpyThrottledClient)
    _, client = cli._build_sources(cfg)
    client.close()
    assert captured["verify"] == str(cert)


def test_build_sources_never_prints_proxy_password(monkeypatch, tmp_path: Path):
    """Находка 3: если у порта включена авторизация, пароль подставляется в
    proxy_url для httpx, но не должен светиться в выводе CLI."""
    monkeypatch.setenv("BT_PROXY_PASSWORD", "s3cr3t-value")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(db=tmp_path / "leads.sqlite", reports=tmp_path / "reports")
        + "blanktrail:\n  proxy_user: user\n  proxy_pass_env: BT_PROXY_PASSWORD\n",
        encoding="utf-8",
    )
    cfg = cli._load_config(config_path)
    monkeypatch.setattr(BlankTrail, "ensure_port", lambda self: self.cfg.port)
    # check_license звонит на настоящий локальный BlankTrail, если его не подменить —
    # ensure_port тут ни при чём, это отдельный вызов сразу после него.
    monkeypatch.setattr(cli, "check_license", lambda *a, **k: None)
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, no_color=True))
    _, client = cli._build_sources(cfg)
    client.close()
    output = buf.getvalue()
    assert "s3cr3t-value" not in output
    assert "user:s3cr3t-value" not in output


def test_build_sources_checks_license_right_after_ensure_port(monkeypatch, cfg_file: Path):
    """Находка 4: предполётная проверка лицензии печатается сразу после того,
    как порт готов — раньше, чем создаётся клиент для похода в Reddit."""
    cfg = cli._load_config(cfg_file)
    calls: list[str] = []
    monkeypatch.setattr(
        BlankTrail, "ensure_port",
        lambda self: calls.append("ensure_port") or self.cfg.port,
    )

    def fake_check_license(blanktrail, profile, console) -> None:
        calls.append("check_license")

    monkeypatch.setattr(cli, "check_license", fake_check_license)
    _, client = cli._build_sources(cfg)
    client.close()
    assert calls == ["ensure_port", "check_license"]


def test_run_exits_1_with_clear_message_when_ca_cert_missing(monkeypatch, tmp_path: Path):
    """Находка 1: несуществующий сертификат — красная строка при старте, а не
    голый traceback httpx посреди сбора.

    ensure_port и Pipeline замоканы независимо от того, успевает ли код дойти
    до них: до фикса ничего не мешает `_build_sources` пройти насквозь, и без
    этих заглушек тест обратился бы к настоящему BlankTrail и настоящему
    пайплайну вместо честного падения на отсутствующем сертификате.
    """
    missing_cert = tmp_path / "blanktrail-ca.pem"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(db=tmp_path / "leads.sqlite", reports=tmp_path / "reports")
        + f"blanktrail:\n  ca_cert: {missing_cert}\n",
        encoding="utf-8",
    )

    def pipeline_should_not_run(*args, **kwargs):
        raise AssertionError("Pipeline не должен запускаться без сертификата")

    monkeypatch.setattr(cli, "build_backends", lambda llm_cfg: {})
    monkeypatch.setattr(BlankTrail, "ensure_port", lambda self: self.cfg.port)
    monkeypatch.setattr(cli, "Pipeline", pipeline_should_not_run)
    result = runner.invoke(cli.app, ["run", "--config", str(config_path)], env=ENV)
    assert result.exit_code == 1
    assert "Не могу начать прогон" in result.output
    assert str(missing_cert) in result.output
