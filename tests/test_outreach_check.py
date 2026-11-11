# tests/test_outreach_check.py — автопроверка `out check`: без сети, на
# поддельном клиенте и сохранённой XML-фикстуре (RSS плоский — см. докстринг
# leadparser/outreach_check.py про честность формулировок).
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadparser.config import RedditConfig
from leadparser.models import Message
from leadparser.net.blanktrail import BlankTrailError
from leadparser.outreach import OutreachStore
from leadparser.outreach_check import DISCLAIMER, run_check
from leadparser.outreach_dashboard import movement_rows
from leadparser.sources.reddit import RedditSource
from leadparser.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
THREAD_XML = (FIXTURES / "reddit_rss_thread.xml").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
USERNAME = "Slow-Plate4355"
COMMENT_URL = "https://www.reddit.com/r/AI_Agents/comments/1wowt2l/ai_agent_developer_needed/"
DM_URL = COMMENT_URL


def make_messages(post_author="OP", comments=()) -> list[Message]:
    """`comments`: список (author, minutes_after_base)."""
    base = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    msgs = [Message(
        thread_id="reddit:1wowt2l", msg_id="post", parent_id=None, author=post_author,
        body="post", created_at=base, depth=0,
    )]
    for i, (author, minutes) in enumerate(comments):
        msgs.append(Message(
            thread_id="reddit:1wowt2l", msg_id=f"c{i}", parent_id="post", author=author,
            body="comment", created_at=base + timedelta(minutes=minutes), depth=1,
        ))
    return msgs


class StubSource:
    def __init__(self, by_thread: dict[str, object]):
        self.by_thread = by_thread
        self.calls: list[str] = []

    def fetch_thread(self, thread_id: str) -> list[Message]:
        self.calls.append(thread_id)
        result = self.by_thread[thread_id]
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def out(store: Storage) -> OutreachStore:
    return OutreachStore(store)


def seed(out: OutreachStore, *, sent_at=None, last_seen_entries=None, comment_visible=None,
          last_checked_at=None, status="sent", url=COMMENT_URL, channel="comment") -> int:
    row_id = out.add(url=url, channel=channel)
    if status != "sent":
        out.set_status(row_id, status)
    out.conn.execute(
        """UPDATE outreach SET sent_at = COALESCE(?, sent_at), last_seen_entries = ?,
               comment_visible = ?, last_checked_at = ? WHERE id = ?""",
        (sent_at, last_seen_entries, comment_visible, last_checked_at, row_id),
    )
    out.conn.commit()
    return row_id


# --- видимость комментария ---

def test_first_check_marks_visible_when_username_found(out: OutreachStore):
    row_id = seed(out)
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 5)])})
    result = run_check(out, source, USERNAME, NOW)
    row = result.rows[0]
    assert row.row_id == row_id
    assert row.visible is True
    assert row.newly_hidden is False
    assert out.get(row_id)["comment_visible"] == 1


def test_first_check_flags_newly_hidden_loudly_when_username_missing(out: OutreachStore):
    """Первая проверка после отправки ничего не нашла — это и есть теневой бан,
    брифу важно не потерять этот сигнал."""
    row_id = seed(out)
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[("someone_else", 5)])})
    result = run_check(out, source, USERNAME, NOW)
    row = result.rows[0]
    assert row.visible is False
    assert row.newly_hidden is True
    assert out.get(row_id)["comment_visible"] == 0


def test_transition_from_visible_to_missing_is_newly_hidden(out: OutreachStore):
    seed(out, last_seen_entries=2, comment_visible=1, last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[("someone_else", 5)])})
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].newly_hidden is True


def test_still_hidden_on_repeat_check_is_not_a_fresh_alarm(out: OutreachStore):
    """Уже было известно, что комментарий скрыт — второй раз громко не кричим."""
    seed(out, last_seen_entries=2, comment_visible=0, last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[("someone_else", 5)])})
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].visible is False
    assert result.rows[0].newly_hidden is False


def test_comment_reappearing_is_reported_visible_again(out: OutreachStore):
    seed(out, last_seen_entries=2, comment_visible=0, last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 5)])})
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].visible is True
    assert result.rows[0].newly_hidden is False


# --- движение в треде / автор треда ---

def test_entry_counts_before_and_after_are_reported(out: OutreachStore):
    seed(out, last_seen_entries=1, comment_visible=1, last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({
        "reddit:1wowt2l": make_messages(comments=[(USERNAME, 5), ("other", 10)])
    })
    result = run_check(out, source, USERNAME, NOW)
    row = result.rows[0]
    assert row.entries_before == 1
    assert row.entries_now == 3  # пост + 2 комментария
    assert out.get(row.row_id)["last_seen_entries"] == 3


def test_run_check_flags_movement_for_the_dashboard_when_entries_grew(out: OutreachStore):
    """Дашборд (leadparser/outreach_dashboard.py) не бегает в Reddit сам — он
    читает то, что здесь персистентно записал run_check."""
    row_id = seed(out, last_seen_entries=1, comment_visible=1,
                  last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({
        "reddit:1wowt2l": make_messages(comments=[(USERNAME, 5), ("other", 10)])
    })
    run_check(out, source, USERNAME, NOW)
    assert [r["id"] for r in movement_rows(out)] == [row_id]


def test_run_check_does_not_flag_movement_when_entry_count_is_unchanged(out: OutreachStore):
    seed(out, last_seen_entries=3, comment_visible=1,
         last_checked_at="2026-09-23T00:00:00+00:00")
    source = StubSource({
        "reddit:1wowt2l": make_messages(comments=[(USERNAME, 5), ("other", 10)])
    })
    run_check(out, source, USERNAME, NOW)
    assert movement_rows(out) == []


def test_run_check_does_not_flag_movement_on_a_first_ever_check(out: OutreachStore):
    """entries_before is None на первой проверке — рост не определён, а не True."""
    seed(out)
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 5)])})
    run_check(out, source, USERNAME, NOW)
    assert movement_rows(out) == []


def test_author_activity_after_send_is_flagged_with_a_careful_wording(out: OutreachStore):
    sent_at = "2026-09-24T07:00:00+00:00"  # раньше base=08:00 в make_messages
    seed(out, sent_at=sent_at)
    source = StubSource({
        "reddit:1wowt2l": make_messages(post_author="OP", comments=[("OP", 30), (USERNAME, 5)])
    })
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].author_posted_after_send is True


def test_author_activity_before_send_is_not_flagged(out: OutreachStore):
    sent_at = "2026-09-25T00:00:00+00:00"  # позже всех сообщений фикстуры
    seed(out, sent_at=sent_at)
    source = StubSource({
        "reddit:1wowt2l": make_messages(post_author="OP", comments=[("OP", 30), (USERNAME, 5)])
    })
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].author_posted_after_send is False


# --- каналы и устойчивость ---

def test_dm_rows_are_skipped_and_counted_not_fetched(out: OutreachStore):
    seed(out, channel="dm", url=DM_URL)
    comment_id = seed(out, channel="comment")
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 1)])})
    result = run_check(out, source, USERNAME, NOW)
    assert result.skipped_dm == 1
    assert [r.row_id for r in result.rows] == [comment_id]
    assert source.calls == ["reddit:1wowt2l"]


def test_unparseable_url_is_skipped_without_calling_the_client(out: OutreachStore):
    seed(out, url="https://example.com/not-a-reddit-thread")
    source = StubSource({})
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows[0].unparseable is True
    assert source.calls == []


def test_short_redd_it_link_is_checkable_despite_missing_subreddit(out: OutreachStore):
    """Ревью: до фикса regex'а redd.it/reddit.com/comments/... схлопывались в
    (None, None) точно как чужой URL — эта строка не проверялась никогда, и
    как раз для неё теневой бан было бы невозможно поймать."""
    row_id = seed(out, url="https://redd.it/1wowt2l")
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 1)])})
    result = run_check(out, source, USERNAME, NOW)
    assert source.calls == ["reddit:1wowt2l"]
    row = result.rows[0]
    assert row.row_id == row_id
    assert row.unparseable is False
    assert row.visible is True


def test_one_failing_row_does_not_abort_the_rest(out: OutreachStore):
    """Один сорвавшийся тред не должен ронять всю проверку — тот же принцип,
    что и у сбора по сабреддитам (RedditSource.list_new_posts)."""
    broken_url = "https://www.reddit.com/r/n8n/comments/deadbee/broken/"
    broken_id = seed(out, url=broken_url)
    ok_id = seed(out)
    source = StubSource({
        "reddit:deadbee": BlankTrailError("429 после ретраев"),
        "reddit:1wowt2l": make_messages(comments=[(USERNAME, 1)]),
    })
    result = run_check(out, source, USERNAME, NOW)
    by_id = {r.row_id: r for r in result.rows}
    assert by_id[broken_id].error is not None
    assert by_id[ok_id].visible is True
    assert sorted(source.calls) == ["reddit:1wowt2l", "reddit:deadbee"]


def test_won_lost_ghosted_rows_are_not_candidates(out: OutreachStore):
    for status in ("won", "lost", "ghosted"):
        row_id = seed(out, status="sent")
        if status == "won":
            out.mark_won(row_id, amount=1, currency="USD", due=None)
        else:
            out.set_status(row_id, status)
    source = StubSource({})
    result = run_check(out, source, USERNAME, NOW)
    assert result.rows == []
    assert result.skipped_dm == 0
    assert source.calls == []


def test_run_check_persists_last_checked_at(out: OutreachStore):
    row_id = seed(out)
    source = StubSource({"reddit:1wowt2l": make_messages(comments=[(USERNAME, 1)])})
    run_check(out, source, USERNAME, NOW)
    assert out.get(row_id)["last_checked_at"] == NOW.isoformat()


# --- честность формулировок ---

def test_disclaimer_names_both_limitations():
    assert "лич" in DISCLAIMER.lower()  # личные сообщения нельзя проверить
    assert "плоск" in DISCLAIMER.lower() or "родител" in DISCLAIMER.lower()


# --- сквозной прогон на настоящей фикстуре через RedditSource ---

class FakeHttpClient:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        for fragment, payload in self.responses.items():
            if fragment in url:
                return payload
        raise AssertionError(f"неожиданный URL: {url}")


def test_end_to_end_through_real_reddit_source_and_fixture(out: OutreachStore):
    """Не выдуманные Message — настоящий RedditSource.fetch_thread на
    настоящей сохранённой XML (см. tests/fixtures/reddit_rss_thread.xml)."""
    url = "https://www.reddit.com/r/n8n/comments/1wobb41/form_submission_tab_does_not_pop_up/"
    row_id = out.add(url=url, channel="comment")
    client = FakeHttpClient({"/comments/1wobb41/.rss": THREAD_XML})
    source = RedditSource(client, RedditConfig())
    result = run_check(out, source, "redditor_4", NOW)
    assert client.urls == ["https://www.reddit.com/comments/1wobb41/.rss?limit=100"]
    row = result.rows[0]
    assert row.row_id == row_id
    assert row.visible is True
    assert row.entries_now == 5  # пост + 4 комментария (AutoModerator отфильтрован)
