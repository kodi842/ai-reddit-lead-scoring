# tests/test_outreach_cli.py — команды `out ...` и `karma` через настоящий cli.app.
# Без сети: `check` подменяет cli._build_sources, остальные команды бьют только
# по временной sqlite-базе.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leadparser import cli
from leadparser.models import Message
from leadparser.net.blanktrail import BlankTrailError
from leadparser.outreach import OutreachStore
from leadparser.storage import Storage

runner = CliRunner()
ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

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


def read_rows(config: Path) -> list[dict]:
    import yaml
    db_path = yaml.safe_load(config.read_text(encoding="utf-8"))["db_path"]
    storage = Storage(db_path)
    try:
        rows = OutreachStore(storage).list_rows(show_all=True)
        return [dict(r) for r in rows]
    finally:
        storage.close()


# --- out add ---

def test_out_add_records_a_comment_and_a_dm_on_the_same_thread(cfg_file: Path):
    """Ровно сценарий из брифа: владелец уже написал комментарий и личку
    одному автору по одному треду — обе строки должны завестись отдельно."""
    r1 = invoke("out", "add", COMMENT_URL, "--channel", "comment",
                "--message", "предложил помощь", config=cfg_file)
    assert r1.exit_code == 0, r1.output
    r2 = invoke("out", "add", COMMENT_URL, "--channel", "dm",
                "--message", "продублировал в личку", config=cfg_file)
    assert r2.exit_code == 0, r2.output

    rows = read_rows(cfg_file)
    assert len(rows) == 2
    assert {r["channel"] for r in rows} == {"comment", "dm"}
    assert all(r["subreddit"] == "AI_Agents" for r in rows)
    assert all(r["status"] == "sent" for r in rows)


def test_out_add_rejects_unknown_channel(cfg_file: Path):
    result = invoke("out", "add", COMMENT_URL, "--channel", "carrier-pigeon", config=cfg_file)
    assert result.exit_code == 1
    assert "comment" in result.output and "dm" in result.output


def test_out_add_warns_when_url_mentions_reddit_but_does_not_parse(cfg_file: Path):
    """Ревью (Important #3): ссылка, которая явно про Reddit, но не подходит ни
    под одну из известных форм, должна быть видна сразу, а не обнаружиться
    неделями позже, когда out check тихо никогда её не проверял."""
    odd_url = "https://www.reddit.com/user/Cool-Background-8360/"
    result = invoke("out", "add", odd_url, "--channel", "dm", config=cfg_file)
    assert result.exit_code == 0, result.output  # всё равно заносим, только предупреждаем
    assert "не разобрал" in result.output.lower()
    row = read_rows(cfg_file)[0]
    assert row["subreddit"] is None


def test_out_add_does_not_warn_for_short_redd_it_link(cfg_file: Path):
    result = invoke(
        "out", "add", "https://redd.it/1wowt2l", "--channel", "comment", config=cfg_file
    )
    assert result.exit_code == 0, result.output
    assert "не разобрал" not in result.output.lower()


def test_out_add_does_not_warn_for_a_plain_non_reddit_url(cfg_file: Path):
    """Предупреждение — только для ссылок, которые ПОХОЖИ на Reddit; обычный
    сторонний URL по-прежнему заносится молча, как и раньше."""
    result = invoke(
        "out", "add", "https://example.com/thread", "--channel", "comment", config=cfg_file
    )
    assert result.exit_code == 0, result.output
    assert "не разобрал" not in result.output.lower()


# --- out set / won / paid ---

def test_out_set_changes_status(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke("out", "set", "1", "replied", "--note", "ответил", config=cfg_file)
    assert result.exit_code == 0, result.output
    row = read_rows(cfg_file)[0]
    assert row["status"] == "replied"
    assert row["note"] == "ответил"


def test_out_set_unknown_id_is_a_red_line_not_a_traceback(cfg_file: Path):
    result = invoke("out", "set", "999", "replied", config=cfg_file)
    assert result.exit_code == 1
    assert "999" in result.output


def test_out_won_records_deal_and_shows_in_list(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke(
        "out", "won", "1", "--amount", "200", "--currency", "aud", "--due", "2026-10-01",
        config=cfg_file,
    )
    assert result.exit_code == 0, result.output
    row = read_rows(cfg_file)[0]
    assert row["status"] == "won"
    assert row["amount"] == 200
    assert row["currency"] == "AUD"
    assert row["due_date"] == "2026-10-01"


def test_out_won_rejects_bad_due_date(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke(
        "out", "won", "1", "--amount", "200", "--currency", "USD", "--due", "не дата",
        config=cfg_file,
    )
    assert result.exit_code == 1


def test_out_paid_sets_paid_at(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "1", "--amount", "50", "--currency", "USD", config=cfg_file)
    result = invoke("out", "paid", "1", config=cfg_file)
    assert result.exit_code == 0, result.output
    assert read_rows(cfg_file)[0]["paid_at"] is not None


def test_out_set_won_is_rejected_with_a_pointer_to_out_won(cfg_file: Path):
    """Ревью: `out set <id> won` в обход `out won` оставлял amount/currency
    пустыми, и строка всё равно попадала в "заказов"."""
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke("out", "set", "1", "won", config=cfg_file)
    assert result.exit_code == 1
    assert "out won" in result.output
    assert read_rows(cfg_file)[0]["status"] == "sent"


def test_out_paid_before_won_is_rejected(cfg_file: Path):
    """Ревью: paid_at по схеме относится к выигранной сделке."""
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    result = invoke("out", "paid", "1", config=cfg_file)
    assert result.exit_code == 1
    assert "won" in result.output.lower()
    assert read_rows(cfg_file)[0]["paid_at"] is None


# --- out list ---

def test_out_list_hides_lost_and_ghosted_unless_all(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "set", "2", "ghosted", config=cfg_file)

    default = invoke("out", "list", config=cfg_file)
    assert default.exit_code == 0, default.output
    assert "ghosted" not in default.output  # строка #2 скрыта из таблицы

    everything = invoke("out", "list", "--all", config=cfg_file)
    assert "ghosted" in everything.output


def test_out_list_shows_unrated_currency_separately_from_rub_total(cfg_file: Path):
    """Ключевое требование брифа: GBP без курса — отдельной строкой, не в
    рублёвом итоге, и явно сказано, что курс не задан."""
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "1", "--amount", "100", "--currency", "USD", config=cfg_file)
    invoke("out", "paid", "1", config=cfg_file)  # заработано считает только оплаченное
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "2", "--amount", "150", "--currency", "GBP", config=cfg_file)

    result = invoke("out", "list", config=cfg_file)
    assert result.exit_code == 0, result.output
    assert "8500" in result.output.replace(",", "").replace(".0", "")  # 100 USD * 85
    assert "GBP" in result.output
    assert "курс" in result.output.lower()
    assert "не задан" in result.output.lower()


def test_out_list_shows_progress_towards_goal(cfg_file: Path):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "1", "--amount", "100", "--currency", "USD", config=cfg_file)
    invoke("out", "paid", "1", config=cfg_file)
    result = invoke("out", "list", config=cfg_file)
    assert "15000" in result.output.replace(",", "")
    assert "%" in result.output


def test_out_list_separates_paid_earnings_from_agreed_unpaid_money(cfg_file: Path):
    """Ревью (Important #1): "заработано" — это оплаченное; выигранная, но не
    оплаченная сделка не должна незаметно поднимать прогресс к недельной цели,
    но и не должна пропадать из денежной картины — показываем её отдельно."""
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "1", "--amount", "100", "--currency", "USD", config=cfg_file)
    invoke("out", "paid", "1", config=cfg_file)
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    invoke("out", "won", "2", "--amount", "50", "--currency", "USD", config=cfg_file)  # не оплачено

    result = invoke("out", "list", config=cfg_file)
    assert result.exit_code == 0, result.output
    clean = result.output.replace(",", "").replace(".0", "")
    assert "8500" in clean   # заработано: только строка #1 (100 * 85)
    assert "4250" in clean   # договорено, не оплачено: строка #2 (50 * 85)
    assert "договорено" in result.output.lower()
    # построчно видно, что именно оплачено, а что нет — не только в сумме воронки
    assert "оплачен" in result.output.lower()
    assert "не оплачен" in result.output.lower()


def test_out_list_with_no_rows_does_not_crash(cfg_file: Path):
    result = invoke("out", "list", config=cfg_file)
    assert result.exit_code == 0, result.output


# --- karma ---

def test_karma_with_no_args_on_empty_history_says_so(cfg_file: Path):
    result = invoke("karma", config=cfg_file)
    assert result.exit_code == 0, result.output
    assert "истори" in result.output.lower()


def test_karma_records_a_value_and_then_shows_it(cfg_file: Path):
    result = invoke("karma", "120", "--note", "старт", config=cfg_file)
    assert result.exit_code == 0, result.output
    shown = invoke("karma", config=cfg_file)
    assert "120" in shown.output
    assert "старт" in shown.output


def test_karma_growth_needs_a_week_of_history(cfg_file: Path):
    invoke("karma", "100", config=cfg_file)
    result = invoke("karma", config=cfg_file)
    assert "7" in result.output  # упоминает недельное окно, а не молчит о нём


# --- out check ---

class FakeReddit:
    def __init__(self, by_thread: dict):
        self.by_thread = by_thread

    def fetch_thread(self, thread_id: str):
        result = self.by_thread[thread_id]
        if isinstance(result, BaseException):
            raise result
        return result


class DummyClient:
    def close(self) -> None:
        return None


def _messages(post_author: str, comments: list[tuple[str, int]]) -> list[Message]:
    base = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    msgs = [Message(thread_id="reddit:1wowt2l", msg_id="post", parent_id=None,
                     author=post_author, body="post", created_at=base, depth=0)]
    for i, (author, minutes) in enumerate(comments):
        msgs.append(Message(
            thread_id="reddit:1wowt2l", msg_id=f"c{i}", parent_id="post", author=author,
            body="c", created_at=base + timedelta(minutes=minutes), depth=1,
        ))
    return msgs


def test_out_check_requires_reddit_username_in_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(db=tmp_path / "leads.sqlite", reports=tmp_path / "reports")
        .replace("reddit_username: Slow-Plate4355", "reddit_username: null"),
        encoding="utf-8",
    )
    result = invoke("out", "check", config=config_path)
    assert result.exit_code == 1
    assert "reddit_username" in result.output


def test_out_check_reports_blanktrail_failure_as_a_red_line(cfg_file: Path, monkeypatch):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)

    def boom(cfg):
        raise BlankTrailError("BlankTrail недоступен")

    monkeypatch.setattr(cli, "_build_sources", boom)
    result = invoke("out", "check", config=cfg_file)
    assert result.exit_code == 1
    assert "Не могу начать проверку" in result.output


def test_out_check_flags_newly_hidden_comment_and_prints_disclaimer(cfg_file: Path, monkeypatch):
    invoke("out", "add", COMMENT_URL, "--channel", "comment", config=cfg_file)
    reddit = FakeReddit({"reddit:1wowt2l": _messages("OP", [("someone_else", 5)])})
    monkeypatch.setattr(cli, "_build_sources", lambda cfg: ({"reddit": reddit}, DummyClient()))

    result = invoke("out", "check", config=cfg_file)
    assert result.exit_code == 0, result.output
    assert "тенев" in result.output.lower() or "не найден" in result.output.lower()
    assert "личн" in result.output.lower()  # дисклеймер про ЛС
    assert "плоск" in result.output.lower() or "родител" in result.output.lower()


def test_out_check_skips_dm_and_reports_the_count(cfg_file: Path, monkeypatch):
    invoke("out", "add", COMMENT_URL, "--channel", "dm", config=cfg_file)
    monkeypatch.setattr(cli, "_build_sources", lambda cfg: ({"reddit": FakeReddit({})}, DummyClient()))

    result = invoke("out", "check", config=cfg_file)
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert "личны" in result.output.lower() or "личных" in result.output.lower()
