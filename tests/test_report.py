from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadparser.config import ReportConfig
from leadparser.models import Lead, Message, Post, ThreadAnalysis
from leadparser.report.render import flag, humanize_age, render_report, write_report
from leadparser.storage import Storage

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
CFG = ReportConfig(min_score=55, max_context_messages=20, context_message_chars=300)


def post(pid: str = "a1", sub: str = "n8n") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key=f"reddit:{sub}",
        url=f"https://www.reddit.com/r/{sub}/comments/{pid}/",
        title="Need HubSpot sync", body="paying $300", author="bob_smith",
        author_url="https://www.reddit.com/user/bob_smith",
        created_at=NOW - timedelta(days=2), flair=None, num_comments=2,
        stickied=False, removed=False, raw_json={},
    )


def lead(username: str = "bob_smith", score: int = 82, **over) -> Lead:
    base = dict(
        username=username, score=score, quote="Will be paying weekly",
        quote_ru="Платить будем еженедельно", lang="en", risk_level="green",
        risk_note="своя задача, оплата названа", why="прямой заказ с оплатой",
        offer="предложить фикс-цену за воркфлоу",
    )
    base.update(over)
    return Lead(**base)


def analysis(*leads: Lead, status: str = "open") -> ThreadAnalysis:
    return ThreadAnalysis(
        problem_title_ru="Нужна синхронизация HubSpot и Google Sheets",
        status=status, leads=list(leads),
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


def test_flag_known_and_unknown():
    assert flag("en") == "🇬🇧"
    assert flag("pt") == "🇵🇹"
    assert flag("zz") == "🏳"


def test_humanize_age():
    assert humanize_age(NOW - timedelta(days=3), NOW) == "3 дн."
    assert humanize_age(NOW - timedelta(hours=5), NOW) == "5 ч."
    assert humanize_age(NOW - timedelta(minutes=12), NOW) == "12 мин."


def test_report_has_head_and_kpi(store: Storage):
    html = render_report([(post(), analysis(lead()), False)], store, CFG, NOW, pending=3)
    assert html.startswith("<!doctype html>")
    assert '<html lang="ru">' in html
    assert "<title>Лиды по тредам</title>" in html
    assert "🧵 Лиды по тредам" in html
    assert '<div class="kc"><b>1</b><span>тредов</span></div>' in html
    assert '<div class="kc"><b>1</b><span>лидов суммарно</span></div>' in html
    assert '<div class="kc"><b>1</b><span>🔴 открытых тем</span></div>' in html
    assert '<div class="kc"><b>3</b><span>⏳ ждут моего разбора</span></div>' in html


def test_report_renders_thread_header_and_columns(store: Storage):
    html = render_report([(post(), analysis(lead()), False)], store, CFG, NOW)
    assert "🧵 Нужна синхронизация HubSpot и Google Sheets" in html
    assert '<span class="st open">🔴 открыта</span>' in html
    assert "reddit:n8n" in html
    assert "https://www.reddit.com/r/n8n/comments/a1/" in html
    for column in ("Балл", "Бэнд", "@username", "Язык", "Дата", "Цитата", "Риск",
                   "Почему", "Что предложить"):
        assert f"<th>{column}</th>" in html


def test_report_renders_lead_cells(store: Storage):
    html = render_report([(post(), analysis(lead()), False)], store, CFG, NOW)
    assert "<td class='sc'>82</td>" in html
    assert "<td>🔥</td>" in html
    assert "@bob_smith" in html
    assert "https://www.reddit.com/user/bob_smith" in html
    assert "title='en'>🇬🇧" in html
    assert "20.09.2026" in html and "2 дн." in html
    assert "Will be paying weekly" in html
    assert "RU: Платить будем еженедельно" in html
    assert "🟢 своя задача, оплата названа" in html
    assert "прямой заказ с оплатой" in html
    assert "предложить фикс-цену за воркфлоу" in html


def test_closed_status_rendered(store: Storage):
    html = render_report(
        [(post(), analysis(lead(), status="closed"), False)], store, CFG, NOW
    )
    assert '<span class="st closed">🟢 закрыта</span>' in html
    assert "🔴 открыта" not in html


def test_verified_threads_are_marked(store: Storage):
    html = render_report([(post(), analysis(lead()), True)], store, CFG, NOW)
    assert "✅ проверено" in html


def test_leads_below_min_score_are_hidden(store: Storage):
    html = render_report(
        [(post(), analysis(lead(score=82), lead("weak_user", 40)), False)],
        store, CFG, NOW,
    )
    assert "@bob_smith" in html
    assert "weak_user" not in html
    assert '<div class="kc"><b>1</b><span>лидов суммарно</span></div>' in html


def test_thread_with_no_visible_leads_is_dropped(store: Storage):
    html = render_report([(post(), analysis(lead(score=40)), False)], store, CFG, NOW)
    assert "Нужна синхронизация" not in html
    assert '<div class="kc"><b>0</b><span>тредов</span></div>' in html


def test_leads_sorted_by_score_desc(store: Storage):
    html = render_report(
        [(post(), analysis(lead("low_user", 60), lead("high_user", 88)), False)],
        store, CFG, NOW,
    )
    assert html.index("high_user") < html.index("low_user")


def test_bands_by_score(store: Storage):
    html = render_report(
        [(post(), analysis(lead("a", 88), lead("b", 72), lead("c", 58)), False)],
        store, CFG, NOW,
    )
    assert "<td>🔥</td>" in html and "<td>☀️</td>" in html and "<td>🌤</td>" in html


def test_quote_is_html_escaped(store: Storage):
    evil = lead(quote="<script>alert('x')</script>", quote_ru="<b>жирный</b>")
    html = render_report([(post(), analysis(evil), False)], store, CFG, NOW)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;жирный" in html


def test_thread_context_rendered_from_messages(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, [
        Message(thread_id=p.thread_id, msg_id="a1", parent_id=None, author="bob_smith",
                body="I run an agency", created_at=NOW - timedelta(days=2), depth=0, score=4),
        Message(thread_id=p.thread_id, msg_id="c1", parent_id="a1", author="ann_dev",
                body="ж" * 900, created_at=NOW - timedelta(days=1), depth=1, score=2),
    ])
    html = render_report([(p, analysis(lead()), False)], store, CFG, NOW)
    assert "🧵 Показать тред (2 сообщений)" in html
    assert "@bob_smith</span> I run an agency" in html
    assert "ж" * CFG.context_message_chars in html
    assert "ж" * (CFG.context_message_chars + 1) not in html


def test_lead_date_taken_from_own_message(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, [
        Message(thread_id=p.thread_id, msg_id="a1", parent_id=None, author="bob_smith",
                body="post", created_at=NOW - timedelta(days=2), depth=0, score=1),
        Message(thread_id=p.thread_id, msg_id="c1", parent_id="a1", author="ann_dev",
                body="reply", created_at=NOW - timedelta(hours=3), depth=1, score=1),
    ])
    html = render_report([(p, analysis(lead("ann_dev", 70)), False)], store, CFG, NOW)
    assert "22.09.2026" in html and "3 ч." in html


def test_experts_block_is_absent(store: Storage):
    html = render_report([(post(), analysis(lead()), False)], store, CFG, NOW)
    assert "Эксперты" not in html


def test_write_report_uses_sample_filename(tmp_path: Path):
    path = write_report("<html></html>", tmp_path, NOW)
    assert path.name == "threads_2026-09-22_120000.html"
    assert path.read_text(encoding="utf-8") == "<html></html>"
