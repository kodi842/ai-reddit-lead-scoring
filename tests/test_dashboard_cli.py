# tests/test_dashboard_cli.py — команда `leadparser dashboard` через настоящий
# cli.app. Без сети: только временная sqlite-база, как и out/karma в
# tests/test_outreach_cli.py.
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from leadparser import cli

runner = CliRunner()
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
outreach:
  reddit_username: Slow-Plate4355
  goal_rub: 15000
  rates_to_rub:
    USD: 85
"""

COMMENT_URL = "https://www.reddit.com/r/AI_Agents/comments/1wowt2l/ai_agent_developer_needed/"


@pytest.fixture()
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(db=tmp_path / "leads.sqlite", reports=tmp_path / "reports"),
        encoding="utf-8",
    )
    return path


def invoke(*args: str, config: Path):
    return runner.invoke(cli.app, [*args, "--config", str(config)], env=ENV)


def test_dashboard_writes_to_the_stable_reports_path(cfg_file: Path, tmp_path: Path):
    result = invoke("dashboard", config=cfg_file)
    assert result.exit_code == 0, result.output
    expected = tmp_path / "reports" / "dashboard.html"
    assert str(expected) in result.output
    assert expected.exists()
    assert expected.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_dashboard_overwrites_the_same_file_instead_of_stamping_new_ones(
    cfg_file: Path, tmp_path: Path
):
    invoke("dashboard", config=cfg_file)
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke("dashboard", config=cfg_file)
    assert result.exit_code == 0, result.output

    reports_dir = tmp_path / "reports"
    html_files = sorted(reports_dir.glob("*.html"))
    assert [f.name for f in html_files] == ["dashboard.html"]
    assert "AI_Agents" in html_files[0].read_text(encoding="utf-8")


def test_dashboard_reflects_outreach_data_added_through_the_out_command(
    cfg_file: Path, tmp_path: Path
):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "1", "--amount", "100", "--currency", "USD", config=cfg_file)
    invoke("out", "paid", "1", config=cfg_file)
    result = invoke("dashboard", config=cfg_file)
    assert result.exit_code == 0, result.output
    html = (tmp_path / "reports" / "dashboard.html").read_text(encoding="utf-8")
    assert "8500" in html.replace(",", "").replace(".0", "")


def test_dashboard_on_broken_config_is_a_red_line_not_a_traceback(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("not: [valid, yaml", encoding="utf-8")
    result = invoke("dashboard", config=config_path)
    assert result.exit_code == 1
    assert "конфиг" in result.output.lower()
