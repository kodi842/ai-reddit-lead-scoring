# tests/test_dashboard_render.py — leadparser/dashboard/render.py: подготовка
# данных, шаблон dashboard.html.j2, запись на стабильный путь.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadparser.config import OutreachConfig
from leadparser.dashboard.render import render_dashboard, write_dashboard
from leadparser.models import Lead, Post, ThreadAnalysis
from leadparser.outreach import OutreachStore
from leadparser.outreach_dashboard import record_movement
from leadparser.storage import Storage

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
CFG = OutreachConfig(reddit_username="Slow-Plate4355", goal_rub=15000, rates_to_rub={"USD": 85})
URL = "https://www.reddit.com/r/AI_Agents/comments/1wowt2l/ai_agent_developer_needed/"


def post(pid: str = "p1", sub: str = "n8n", title: str = "Need a bot") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key=f"reddit:{sub}",
        url=f"https://www.reddit.com/r/{sub}/comments/{pid}/x/",
        title=title, body="body", author="op", author_url="https://www.reddit.com/user/op",
        created_at=NOW, flair=None, num_comments=1, stickied=False, removed=False, raw_json={},
    )


def analysis(score: int = 80) -> ThreadAnalysis:
    return ThreadAnalysis(
        problem_title_ru="Нужен разработчик", status="open",
        leads=[Lead(username="lead", score=score, quote="q", quote_ru="ц", lang="en",
                    risk_level="green", risk_note="", why="", offer="")],
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def out(store: Storage) -> OutreachStore:
    # Тот же store, что и в параметре теста — pytest резолвит фикстуру `store`
    # один раз на тест, так что это гарантированно одна и та же база.
    return OutreachStore(store)


def render(storage: Storage, store_: OutreachStore, cfg=CFG, min_score=55, now=NOW) -> str:
    return render_dashboard(storage, store_, cfg, min_score, now)


# --- каркас страницы ---

def test_page_has_head_and_is_russian(store: Storage, out: OutreachStore):
    html = render(store, out)
    assert html.startswith("<!doctype html>")
    assert '<html lang="ru">' in html


def test_palette_and_both_dark_mode_selectors_are_present(store: Storage, out: OutreachStore):
    html = render(store, out)
    for hex_code in ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#fcfcfb", "#0b0b0b", "#52514e"):
        assert hex_code in html
    for hex_code in ("#3987e5", "#d95926", "#199e70", "#c98500", "#1a1a19", "#ffffff", "#c3c2b7"):
        assert hex_code in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert 'data-theme="dark"' in html


# --- герой: деньги никогда не смешиваются ---

def test_hero_shows_earned_goal_and_percent(store: Storage, out: OutreachStore):
    row_id = out.add(url=URL, channel="comment")
    out.mark_won(row_id, amount=100, currency="USD", due=None)
    out.mark_paid(row_id)
    html = render(store, out)
    clean = html.replace(",", "").replace(".0", "")
    assert "8500" in clean
    assert "15000" in clean
    assert "57" in html  # 8500 / 15000 = 56.7%, округление где-то рядом


def test_unpaid_and_unrated_never_get_added_into_the_earned_number(
    store: Storage, out: OutreachStore
):
    """Ключевое требование брифа: оплачено (8500) и договорено-не-оплачено
    (4250) не должны нигде складываться в одно число (12750)."""
    paid_id = out.add(url=URL, channel="comment")
    out.mark_won(paid_id, amount=100, currency="USD", due=None)
    out.mark_paid(paid_id)
    unpaid_id = out.add(url=URL, channel="comment")
    out.mark_won(unpaid_id, amount=50, currency="USD", due=None)  # не оплачено
    html = render(store, out)
    clean = html.replace(",", "").replace(".0", "")
    assert "8500" in clean
    assert "4250" in clean
    assert "12750" not in clean
    assert "договорено" in html.lower()
    assert "не оплачен" in html.lower()


def test_unrated_currency_is_shown_as_is_with_a_note(store: Storage, out: OutreachStore):
    row_id = out.add(url=URL, channel="comment")
    out.mark_won(row_id, amount=150, currency="GBP", due=None)
    html = render(store, out)
    assert "150" in html and "GBP" in html
    assert "курс" in html.lower() and "не задан" in html.lower()


def test_unrated_currency_with_both_paid_and_unpaid_shows_two_separate_lines(
    store: Storage, out: OutreachStore
):
    """Ревью: платёж и договорённость в одной валюте без курса раньше молча
    складывались в одну сумму (150) — по ней нельзя понять, что из этого уже
    пришло. Теперь это две строки: 100 (оплачено) и 50 (не оплачено)."""
    paid_id = out.add(url=URL, channel="comment")
    out.mark_won(paid_id, amount=100, currency="GBP", due=None)
    out.mark_paid(paid_id)
    unpaid_id = out.add(url=URL, channel="comment")
    out.mark_won(unpaid_id, amount=50, currency="GBP", due=None)
    html = render(store, out)
    assert "150 GBP" not in html  # старое поведение — объединённая сумма
    assert "100 GBP" in html and "50 GBP" in html
    assert "оплачено" in html.lower() and "не оплачено" in html.lower()


# --- "что делать сейчас" ---

def test_action_lists_say_so_when_empty_instead_of_hiding_the_block(
    store: Storage, out: OutreachStore
):
    html = render(store, out)
    assert "пуст" in html.lower()


def test_stale_outreach_is_listed_after_three_days(store: Storage, out: OutreachStore):
    row_id = out.add(url=URL, channel="comment")
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=8)).isoformat(), row_id),
    )
    out.conn.commit()
    html = render(store, out)
    assert "AI_Agents" in html
    assert URL in html


def test_hidden_comment_row_is_the_most_prominent_alert(store: Storage, out: OutreachStore):
    """Владелец не увидит пропавший комментарий со своего экрана — бриф просит
    выделить это заметнее всего остального в блоке."""
    row_id = out.add(url=URL, channel="comment")
    out.record_check(row_id, checked_at=NOW.isoformat(), entries=1, visible=False)
    html = render(store, out)
    assert "dash-alert-critical" in html  # выделенный класс — не просто текст в общем списке
    assert "публичной ленты" in html or "пропал" in html.lower()
    assert NOW.isoformat() not in html  # дата человеку, не сырой ISO-таймстамп
    assert "24.09.2026" in html


def test_movement_row_is_listed(store: Storage, out: OutreachStore):
    row_id = out.add(url=URL, channel="comment")
    record_movement(out.conn, row_id, checked_at=NOW.isoformat(), grew=True,
                     entries_before=1, entries_now=4)
    html = render(store, out)
    assert "AI_Agents" in html
    assert "1" in html and "4" in html


def test_thread_titles_and_notes_in_action_lists_are_escaped(store: Storage, out: OutreachStore):
    store.upsert_post(post("p9", sub="n8n", title="<script>alert(1)</script>"))
    row_id = out.add(
        url="https://www.reddit.com/r/n8n/comments/p9/x/", channel="comment",
        note="<b>заметка</b>",
    )
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=8)).isoformat(), row_id),
    )
    out.conn.commit()
    html = render(store, out)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>заметка</b>" not in html


# --- воронка ---

def test_funnel_svg_is_embedded_unescaped_with_all_five_stages(store: Storage, out: OutreachStore):
    row_id = out.add(url=URL, channel="comment")
    out.mark_won(row_id, amount=10, currency="USD", due=None)
    out.mark_paid(row_id)
    html = render(store, out)
    assert "<svg" in html
    assert "&lt;svg" not in html
    for label in ("Отправлено", "Ответили", "Переговоры", "Заказов", "Оплачено"):
        assert label in html


# --- карма ---

def test_karma_with_fewer_than_two_points_shows_the_honest_fallback(
    store: Storage, out: OutreachStore
):
    html = render(store, out)
    assert "истории мало" in html.lower()


def test_karma_with_two_or_more_points_renders_a_chart(store: Storage, out: OutreachStore):
    out.record_karma(100)
    out.conn.execute(
        "INSERT INTO karma (recorded_on, karma, note) VALUES (?, 90, NULL)",
        ((NOW - timedelta(days=5)).date().isoformat(),),
    )
    out.conn.commit()
    html = render(store, out)
    assert html.count("<svg") == 2  # воронка + карма
    assert "истории мало" not in html.lower()


# --- запас лидов ---

def test_leads_inventory_separates_gig_locked_from_available(store: Storage, out: OutreachStore):
    store.upsert_post(post("p1", sub="n8n"))
    store.upsert_post(post("p2", sub="slavelabour"))
    run_id = store.start_run()
    store.save_analysis("reddit:p1", "v1", False, analysis(80), run_id)
    store.save_analysis("reddit:p2", "v1", False, analysis(70), run_id)
    html = render(store, out, min_score=55)
    assert "n8n" in html and "slavelabour" in html
    assert "гиг" in html.lower()


# --- таблица откликов ---

def test_outreach_table_has_a_clickable_thread_link_and_matches_out_list_wording(
    store: Storage, out: OutreachStore
):
    row_id = out.add(url=URL, channel="comment")
    out.mark_won(row_id, amount=100, currency="USD", due=None)
    html = render(store, out)
    assert f'href="{URL}"' in html
    assert "не оплачен" in html.lower()


def test_outreach_table_includes_days_ago_like_out_list(store: Storage, out: OutreachStore):
    """Бриф: таблица — надмножество `out list` ("всё, что в out list, плюс
    ссылка"). print_rows_table (outreach_render.py) показывает "дней назад" —
    для холодных откликов это половина того, что подсказывает, пора ли
    напомнить о себе, даже если отклик ещё не попал в список "застряло"."""
    row_id = out.add(url=URL, channel="comment")
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=5)).isoformat(), row_id),
    )
    out.conn.commit()
    out.set_status(row_id, "replied")  # не застрял, не скрыт — иначе бы всё равно попал на страницу
    html = render(store, out)
    assert "дней назад" in html.lower() or "дн. назад" in html.lower()
    # "5" должно стоять рядом с этой самой строкой таблицы, не только где-то на странице
    table_section = html.split('id="rows"')[1]
    assert ">5<" in table_section


def test_outreach_table_strips_rich_console_markup_including_the_bare_closing_tag(
    store: Storage, out: OutreachStore
):
    """payment_flag/visible_flag (outreach_render.py) пишут Rich-разметку для
    консоли, включая голый `[/]` без имени тега — он не должен утечь на
    страницу буквально, как в `не оплачено[/]`."""
    won_unpaid = out.add(url=URL, channel="comment")
    out.mark_won(won_unpaid, amount=10, currency="USD", due=None)
    hidden = out.add(url=URL, channel="comment")
    out.record_check(hidden, checked_at=NOW.isoformat(), entries=1, visible=False)
    html = render(store, out)
    assert "[/]" not in html
    assert "[red]" not in html
    assert "[yellow]" not in html


# --- write_dashboard: стабильный путь, перезапись ---

def test_write_dashboard_uses_a_stable_filename_and_overwrites(tmp_path: Path):
    p1 = write_dashboard("<html>v1</html>", tmp_path)
    assert p1 == tmp_path / "dashboard.html"
    p2 = write_dashboard("<html>v2</html>", tmp_path)
    assert p2 == p1
    assert p1.read_text(encoding="utf-8") == "<html>v2</html>"
