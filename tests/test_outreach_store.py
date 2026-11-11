# tests/test_outreach_store.py — учёт откликов и кармы (leadparser/outreach.py).
from datetime import date, datetime, timedelta, timezone

import pytest

from leadparser.models import Post
from leadparser.outreach import (
    OutreachError, OutreachStore, UnratedAmount, mentions_reddit, parse_reddit_url,
)
from leadparser.storage import Storage

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
THREAD_URL = "https://www.reddit.com/r/AI_Agents/comments/1wowt2l/ai_agent_developer_needed_asap/"


def make_thread_post(thread_id: str = "reddit:1wowt2l", sub: str = "AI_Agents") -> Post:
    return Post(
        thread_id=thread_id, source_key=f"reddit:{sub}",
        url=f"https://www.reddit.com/r/{sub}/comments/{thread_id.split(':')[1]}/x/",
        title="AI agent developer needed ASAP", body="body", author="Cool-Background-8360",
        author_url="https://www.reddit.com/user/Cool-Background-8360",
        created_at=NOW, flair=None, num_comments=1, stickied=False, removed=False, raw_json={},
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def out(store: Storage) -> OutreachStore:
    return OutreachStore(store)


# --- parse_reddit_url ---

def test_parse_reddit_url_extracts_subreddit_and_thread_id():
    sub, thread_id = parse_reddit_url(THREAD_URL)
    assert sub == "AI_Agents"
    assert thread_id == "reddit:1wowt2l"


def test_parse_reddit_url_handles_old_reddit_and_trailing_slug_variants():
    sub, thread_id = parse_reddit_url(
        "https://old.reddit.com/r/n8n/comments/1wobb41/form_submission_tab/"
    )
    assert sub == "n8n"
    assert thread_id == "reddit:1wobb41"


def test_parse_reddit_url_returns_none_for_non_reddit_url():
    assert parse_reddit_url("https://example.com/not-reddit") == (None, None)


def test_parse_reddit_url_handles_short_redd_it_share_links():
    """Ревью: redd.it — то, что даёт кнопка «Поделиться» в мобильном
    приложении Reddit. Сабреддита в этой форме нет вовсе, id поста есть."""
    assert parse_reddit_url("https://redd.it/1wowt2l") == (None, "reddit:1wowt2l")


def test_parse_reddit_url_handles_subredditless_comments_links():
    assert parse_reddit_url("https://www.reddit.com/comments/1wowt2l/") == (
        None, "reddit:1wowt2l",
    )


def test_mentions_reddit_true_for_both_domains():
    assert mentions_reddit("https://reddit.com/gallery/1wowt2l") is True
    assert mentions_reddit("https://redd.it/1wowt2l") is True


def test_mentions_reddit_false_for_unrelated_url():
    assert mentions_reddit("https://example.com/not-reddit") is False


# --- add ---

def test_add_sets_thread_id_when_thread_already_known(store: Storage, out: OutreachStore):
    store.upsert_post(make_thread_post())
    row_id = out.add(url=THREAD_URL, channel="comment", message="hi", note=None)
    row = out.get(row_id)
    assert row["thread_id"] == "reddit:1wowt2l"
    assert row["subreddit"] == "AI_Agents"
    assert row["status"] == "sent"
    assert row["channel"] == "comment"
    assert row["message"] == "hi"
    assert row["url"] == THREAD_URL


def test_add_leaves_thread_id_null_when_thread_unknown(out: OutreachStore):
    """B: тред ещё не собран основным пайплайном — отклик всё равно заносится."""
    row_id = out.add(url=THREAD_URL, channel="dm", message=None, note="через ЛС")
    row = out.get(row_id)
    assert row["thread_id"] is None
    assert row["subreddit"] == "AI_Agents"
    assert row["channel"] == "dm"
    assert row["note"] == "через ЛС"


def test_add_works_for_two_channels_on_the_same_thread(out: OutreachStore):
    """Ровно сценарий из брифа: комментарий и личка одному автору по одному треду."""
    comment_id = out.add(url=THREAD_URL, channel="comment", message="комментарий")
    dm_id = out.add(url=THREAD_URL, channel="dm", message="личка")
    assert comment_id != dm_id
    rows = out.list_rows(show_all=True)
    assert {r["channel"] for r in rows} == {"comment", "dm"}
    assert all(r["subreddit"] == "AI_Agents" for r in rows)


def test_add_rejects_unknown_channel(out: OutreachStore):
    with pytest.raises(OutreachError, match="comment.*dm|dm.*comment"):
        out.add(url=THREAD_URL, channel="carrier-pigeon")


def test_add_stores_a_non_reddit_url_with_null_subreddit(out: OutreachStore):
    """Ссылку разобрать не удалось — отклик не теряем, просто без разбивки."""
    row_id = out.add(url="https://example.com/thread", channel="comment")
    row = out.get(row_id)
    assert row["subreddit"] is None
    assert row["thread_id"] is None
    assert row["url"] == "https://example.com/thread"


# --- set_status ---

def test_set_status_updates_status_and_note(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.set_status(row_id, "replied", note="ответил сам автор")
    row = out.get(row_id)
    assert row["status"] == "replied"
    assert row["note"] == "ответил сам автор"


def test_set_status_without_note_keeps_existing_note(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment", note="исходная заметка")
    out.set_status(row_id, "negotiating")
    assert out.get(row_id)["note"] == "исходная заметка"


def test_set_status_rejects_unknown_status(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    with pytest.raises(OutreachError, match="статус"):
        out.set_status(row_id, "friendship")


def test_set_status_rejects_unknown_id(out: OutreachStore):
    with pytest.raises(OutreachError, match="404|не найден"):
        out.set_status(404, "replied")


# --- won / paid ---

def test_mark_won_sets_status_amount_currency_and_due(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(row_id, amount=200, currency="aud", due="2026-10-01")
    row = out.get(row_id)
    assert row["status"] == "won"
    assert row["amount"] == 200
    assert row["currency"] == "AUD"
    assert row["due_date"] == "2026-10-01"


def test_mark_won_without_due_leaves_it_null(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(row_id, amount=50, currency="USD", due=None)
    assert out.get(row_id)["due_date"] is None


def test_mark_won_rejects_unknown_id(out: OutreachStore):
    with pytest.raises(OutreachError):
        out.mark_won(404, amount=1, currency="USD", due=None)


def test_mark_paid_sets_paid_at(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(row_id, amount=50, currency="USD", due=None)
    out.mark_paid(row_id)
    row = out.get(row_id)
    assert row["paid_at"] is not None


def test_mark_paid_rejects_unknown_id(out: OutreachStore):
    with pytest.raises(OutreachError):
        out.mark_paid(404)


# --- list_rows ---

def test_list_rows_hides_lost_and_ghosted_by_default(out: OutreachStore):
    keep = out.add(url=THREAD_URL, channel="comment")
    lost = out.add(url=THREAD_URL, channel="comment")
    ghosted = out.add(url=THREAD_URL, channel="comment")
    out.set_status(lost, "lost")
    out.set_status(ghosted, "ghosted")
    ids = [r["id"] for r in out.list_rows()]
    assert ids == [keep]


def test_list_rows_all_shows_everything(out: OutreachStore):
    a = out.add(url=THREAD_URL, channel="comment")
    b = out.add(url=THREAD_URL, channel="comment")
    out.set_status(b, "lost")
    ids = {r["id"] for r in out.list_rows(show_all=True)}
    assert ids == {a, b}


def test_list_rows_filters_by_explicit_status(out: OutreachStore):
    a = out.add(url=THREAD_URL, channel="comment")
    b = out.add(url=THREAD_URL, channel="comment")
    out.set_status(b, "negotiating")
    ids = [r["id"] for r in out.list_rows(status="negotiating")]
    assert ids == [b]
    assert a not in ids


# --- funnel / currency ---

def test_funnel_counts_are_cumulative_by_stage(out: OutreachStore):
    sent_only = out.add(url=THREAD_URL, channel="comment")
    replied = out.add(url=THREAD_URL, channel="comment")
    out.set_status(replied, "replied")
    negotiating = out.add(url=THREAD_URL, channel="comment")
    out.set_status(negotiating, "negotiating")
    won = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(won, amount=100, currency="USD", due=None)
    out.mark_paid(won)  # "заработано" считает только оплаченное — см. ниже
    stats = out.funnel(rates={"USD": 85})
    assert stats.sent == 4
    assert stats.replied == 3   # replied + negotiating + won
    assert stats.negotiating == 2  # negotiating + won
    assert stats.won == 1
    assert stats.earned_rub == pytest.approx(8500)
    assert sent_only  # используется только для читаемости сценария


def test_funnel_counts_only_paid_deals_as_earned(out: OutreachStore):
    """Ревью: won без paid_at — это ещё не деньги в кармане. Раньше earned_rub
    считал сумму сразу по mark_won, что рисовало прогресс к недельной цели по
    сделкам, которые ещё никто не оплатил."""
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(row_id, amount=50, currency="USD", due=None)
    stats = out.funnel(rates={"USD": 85})
    assert stats.earned_rub == 0
    assert stats.unpaid_rub == pytest.approx(50 * 85)

    out.mark_paid(row_id)
    stats = out.funnel(rates={"USD": 85})
    assert stats.earned_rub == pytest.approx(50 * 85)
    assert stats.unpaid_rub == 0


def test_funnel_converts_known_currency_to_rub_using_configured_rate(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(row_id, amount=200, currency="USD", due=None)
    out.mark_paid(row_id)
    stats = out.funnel(rates={"USD": 85})
    assert stats.earned_rub == pytest.approx(200 * 85)
    assert stats.unrated == {}


def test_funnel_keeps_currency_without_configured_rate_separate_and_out_of_total(
    out: OutreachStore,
):
    """Ключевое требование брифа: не выдумывать курс для GBP/AUD без записи в
    rates_to_rub — показываем отдельно и не тащим в рублёвый итог. Это не
    зависит от того, оплачена сделка или нет — курса всё равно нет."""
    usd_row = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(usd_row, amount=100, currency="USD", due=None)
    out.mark_paid(usd_row)
    gbp_row = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(gbp_row, amount=150, currency="GBP", due=None)
    stats = out.funnel(rates={"USD": 85})
    assert stats.earned_rub == pytest.approx(100 * 85)
    assert stats.unrated == {"GBP": UnratedAmount(paid=0, unpaid=150)}


def test_funnel_splits_unrated_currency_by_paid_status(out: OutreachStore):
    """Ревью: платёж и договорённость в одной валюте без курса раньше молча
    складывались в одно число — по нему нельзя было понять, что из этого уже
    реально пришло, а что только обещано. Дашборд показывает это отдельными
    строками именно потому, что здесь они уже раздельные поля."""
    paid_gbp = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(paid_gbp, amount=100, currency="GBP", due=None)
    out.mark_paid(paid_gbp)
    unpaid_gbp = out.add(url=THREAD_URL, channel="comment")
    out.mark_won(unpaid_gbp, amount=50, currency="GBP", due=None)
    stats = out.funnel(rates={})
    assert stats.unrated == {"GBP": UnratedAmount(paid=100, unpaid=50)}


def test_funnel_ignores_non_won_rows_for_earnings(out: OutreachStore):
    row_id = out.add(url=THREAD_URL, channel="comment")
    out.set_status(row_id, "negotiating")
    stats = out.funnel(rates={"USD": 85})
    assert stats.earned_rub == 0
    assert stats.unpaid_rub == 0
    assert stats.unrated == {}


# --- инварианты статуса: won только через mark_won, paid только после won ---

def test_set_status_rejects_direct_transition_to_won(out: OutreachStore):
    """Ревью: `out set <id> won` в обход mark_won оставлял amount/currency
    пустыми, а строка всё равно попадала в "заказов"."""
    row_id = out.add(url=THREAD_URL, channel="comment")
    with pytest.raises(OutreachError, match="out won"):
        out.set_status(row_id, "won")
    assert out.get(row_id)["status"] == "sent"


def test_mark_paid_rejects_when_row_is_not_won(out: OutreachStore):
    """Ревью: paid_at по смыслу принадлежит выигранной сделке (см. комментарий
    к схеме) — нельзя проставить его раньше, чем `out won`."""
    row_id = out.add(url=THREAD_URL, channel="comment")
    with pytest.raises(OutreachError, match="won"):
        out.mark_paid(row_id)
    assert out.get(row_id)["paid_at"] is None


# --- subreddit breakdown ---

def test_subreddit_breakdown_counts_outreach_replies_and_deals_per_sub(out: OutreachStore):
    a1 = out.add(url=THREAD_URL, channel="comment")  # AI_Agents
    out.mark_won(a1, amount=1, currency="USD", due=None)
    out.add(url=THREAD_URL, channel="dm")  # AI_Agents, ещё не ответили
    n8n_url = "https://www.reddit.com/r/n8n/comments/1wobb41/form_submission_tab/"
    n8n_row = out.add(url=n8n_url, channel="comment")
    out.set_status(n8n_row, "replied")

    breakdown = {sub: (sent, replied, won) for sub, sent, replied, won in out.subreddit_breakdown()}
    assert breakdown["AI_Agents"] == (2, 1, 1)
    assert breakdown["n8n"] == (1, 1, 0)


# --- karma ---

def test_record_karma_upserts_for_the_same_day(out: OutreachStore):
    out.record_karma(120, note="старт")
    out.record_karma(125, note="исправил опечатку")
    history = out.karma_history()
    assert len(history) == 1
    assert history[0]["karma"] == 125
    assert history[0]["note"] == "исправил опечатку"
    assert history[0]["recorded_on"] == date.today().isoformat()


def test_karma_growth_7d_is_none_without_a_week_of_history(out: OutreachStore):
    out.record_karma(100)
    assert out.karma_growth_7d() is None


def test_karma_growth_7d_is_none_when_two_points_are_still_closer_than_a_week(
    out: OutreachStore,
):
    """Две записи не значит неделя данных — честность важнее видимости числа."""
    three_days_ago = (date.today() - timedelta(days=3)).isoformat()
    out.conn.execute(
        "INSERT INTO karma (recorded_on, karma, note) VALUES (?, 100, NULL)", (three_days_ago,)
    )
    out.conn.commit()
    out.record_karma(110)
    assert out.karma_growth_7d() is None


def test_karma_growth_7d_computes_delta_against_the_oldest_point_at_or_before_cutoff(
    out: OutreachStore,
):
    today = date.today()
    eight_days_ago = (today - timedelta(days=8)).isoformat()
    two_days_ago = (today - timedelta(days=2)).isoformat()
    out.conn.execute(
        "INSERT INTO karma (recorded_on, karma, note) VALUES (?, 90, NULL)", (eight_days_ago,)
    )
    out.conn.execute(
        "INSERT INTO karma (recorded_on, karma, note) VALUES (?, 110, NULL)", (two_days_ago,)
    )
    out.conn.commit()
    out.record_karma(130)
    assert out.karma_growth_7d() == 130 - 90
