# tests/test_outreach_dashboard.py — запросы для `leadparser dashboard`, которых
# нет ни в OutreachStore (leadparser/outreach.py), ни в Storage: что зависло,
# что скрыто, что уже отработано, запас лидов (leadparser/outreach_dashboard.py).
from datetime import datetime, timedelta, timezone

import pytest

from leadparser.models import Lead, Post, ThreadAnalysis
from leadparser.outreach import OutreachStore
from leadparser.outreach_dashboard import (
    GIG_SUBREDDITS, hidden_comment_rows, leads_inventory, movement_rows,
    paid_count, record_movement, stale_rows, worked_thread_ids,
)
from leadparser.storage import Storage

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
THREAD_URL = "https://www.reddit.com/r/AI_Agents/comments/1wowt2l/ai_agent_developer_needed_asap/"


def post(pid: str = "p1", sub: str = "AI_Agents") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key=f"reddit:{sub}",
        url=f"https://www.reddit.com/r/{sub}/comments/{pid}/x/",
        title=f"title {pid}", body="body", author="op",
        author_url="https://www.reddit.com/user/op",
        created_at=NOW, flair=None, num_comments=1, stickied=False, removed=False,
        raw_json={},
    )


def analysis(score: int) -> ThreadAnalysis:
    return ThreadAnalysis(
        problem_title_ru="Нужен разработчик",
        status="open",
        leads=[Lead(
            username="lead", score=score, quote="q", quote_ru="ц",
            lang="en", risk_level="green", risk_note="", why="", offer="",
        )],
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def out(store: Storage) -> OutreachStore:
    return OutreachStore(store)


# --- stale_rows: без ответа дольше N дней ---

def test_stale_rows_finds_sent_rows_past_the_threshold(out: OutreachStore):
    old = out.add(url=THREAD_URL, channel="comment")
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=8)).isoformat(), old),
    )
    fresh = out.add(url=THREAD_URL, channel="comment")
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=1)).isoformat(), fresh),
    )
    out.conn.commit()
    assert [r["id"] for r in stale_rows(out, NOW, days=3)] == [old]


def test_stale_rows_ignores_rows_that_already_got_a_reply(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.conn.execute(
        "UPDATE outreach SET sent_at = ? WHERE id = ?",
        ((NOW - timedelta(days=8)).isoformat(), row_id),
    )
    out.conn.commit()
    out.set_status(row_id, "replied")
    assert stale_rows(out, NOW, days=3) == []


# --- hidden_comment_rows: comment_visible = 0 ---

def test_hidden_comment_rows_finds_only_invisible_comments(out: OutreachStore):
    hidden = out.add(url=THREAD_URL, channel="comment")
    out.record_check(hidden, checked_at=NOW.isoformat(), entries=1, visible=False)
    visible = out.add(url=THREAD_URL, channel="comment")
    out.record_check(visible, checked_at=NOW.isoformat(), entries=1, visible=True)
    assert [r["id"] for r in hidden_comment_rows(out)] == [hidden]


def test_hidden_comment_rows_excludes_rows_already_resolved(out: OutreachStore):
    """Список — "что делать сейчас": сделка уже закрыта (выиграна, слита или
    автор пропал) — действовать больше не по чему, показывать её тут — шум."""
    active_hidden = out.add(url=THREAD_URL, channel="comment")
    out.record_check(active_hidden, checked_at=NOW.isoformat(), entries=1, visible=False)
    won_hidden = out.add(url=THREAD_URL, channel="comment")
    out.record_check(won_hidden, checked_at=NOW.isoformat(), entries=1, visible=False)
    out.mark_won(won_hidden, amount=10, currency="USD", due=None)
    lost_hidden = out.add(url=THREAD_URL, channel="comment")
    out.record_check(lost_hidden, checked_at=NOW.isoformat(), entries=1, visible=False)
    out.set_status(lost_hidden, "lost")
    assert [r["id"] for r in hidden_comment_rows(out)] == [active_hidden]


# --- paid_count: заказы, по которым деньги уже пришли ---

def test_paid_count_counts_only_won_and_paid(out: OutreachStore):
    paid = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(paid, amount=10, currency="USD", due=None)
    out.mark_paid(paid)
    unpaid = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(unpaid, amount=10, currency="USD", due=None)
    assert paid_count(out) == 1


# --- worked_thread_ids: только отклики со связанным тредом ---

def test_worked_thread_ids_only_counts_rows_with_a_known_thread(
    store: Storage, out: OutreachStore
):
    store.upsert_post(post("p1"))
    out.add(url="https://www.reddit.com/r/AI_Agents/comments/p1/x/", channel="comment")
    out.add(url="https://example.com/unrelated", channel="dm")  # без thread_id
    assert worked_thread_ids(out) == {"reddit:p1"}


# --- record_movement / movement_rows ---

def test_movement_rows_returns_only_rows_flagged_as_grown(out: OutreachStore):
    grew_id = out.add(url=THREAD_URL, channel="comment")
    record_movement(
        out.conn, grew_id, checked_at=NOW.isoformat(), grew=True,
        entries_before=1, entries_now=3,
    )
    flat_id = out.add(url=THREAD_URL, channel="comment")
    record_movement(
        out.conn, flat_id, checked_at=NOW.isoformat(), grew=False,
        entries_before=2, entries_now=2,
    )
    assert [r["id"] for r in movement_rows(out)] == [grew_id]


def test_movement_rows_excludes_rows_already_resolved(out: OutreachStore):
    """Тред вырос, но сделка уже won/lost/ghosted — followup по ответу больше
    не нужен, в "что делать сейчас" ей не место."""
    active_id = out.add(url=THREAD_URL, channel="comment")
    record_movement(out.conn, active_id, checked_at=NOW.isoformat(), grew=True,
                     entries_before=1, entries_now=3)
    won_id = out.add(url=THREAD_URL, channel="comment")
    record_movement(out.conn, won_id, checked_at=NOW.isoformat(), grew=True,
                     entries_before=1, entries_now=3)
    out.mark_won(won_id, amount=10, currency="USD", due=None)
    assert [r["id"] for r in movement_rows(out)] == [active_id]


def test_record_movement_upserts_so_a_later_check_can_clear_the_flag(out: OutreachStore):
    """Второй чек без роста должен убрать строку из списка — это состояние на
    момент последней проверки, а не журнал "когда-либо росло"."""
    row_id = out.add(url=THREAD_URL, channel="comment")
    record_movement(
        out.conn, row_id, checked_at="2026-09-20T00:00:00+00:00", grew=True,
        entries_before=1, entries_now=2,
    )
    record_movement(
        out.conn, row_id, checked_at="2026-09-24T00:00:00+00:00", grew=False,
        entries_before=2, entries_now=2,
    )
    assert movement_rows(out) == []


# --- GIG_SUBREDDITS ---

def test_gig_subreddits_matches_the_brief_list_case_insensitively():
    assert GIG_SUBREDDITS == {
        "slavelabour", "forhire", "donedirtcheap", "jobbit",
        "hireaprogrammer", "programmingrequests",
    }


# --- leads_inventory: неотработанные лиды 55+, по сабреддитам, гиг-сабы отдельно ---

def test_leads_inventory_excludes_worked_threads_and_splits_gig_subs(
    store: Storage, out: OutreachStore,
):
    store.upsert_post(post("p1", sub="n8n"))       # свободный, останется
    store.upsert_post(post("p2", sub="slavelabour"))  # гиг-саб
    store.upsert_post(post("p3", sub="n8n"))       # уже в работе — исключить
    run_id = store.start_run()
    store.save_analysis("reddit:p1", "v1", False, analysis(80), run_id)
    store.save_analysis("reddit:p2", "v1", False, analysis(70), run_id)
    store.save_analysis("reddit:p3", "v1", False, analysis(60), run_id)
    out.add(url="https://www.reddit.com/r/n8n/comments/p3/x/", channel="comment")

    inv = leads_inventory(store, out, min_score=55)
    assert inv.gig_locked == 1
    assert inv.available == 1
    assert ("n8n", 1, False) in inv.by_subreddit
    assert ("slavelabour", 1, True) in inv.by_subreddit
    assert sum(n for _, n, _ in inv.by_subreddit) == 2  # p3 не считается вовсе


def test_leads_inventory_ignores_leads_below_the_threshold(
    store: Storage, out: OutreachStore,
):
    store.upsert_post(post("p1", sub="n8n"))
    run_id = store.start_run()
    store.save_analysis("reddit:p1", "v1", False, analysis(40), run_id)
    inv = leads_inventory(store, out, min_score=55)
    assert inv.available == 0
    assert inv.by_subreddit == []
