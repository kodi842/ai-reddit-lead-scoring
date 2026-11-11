# tests/test_storage.py
from datetime import datetime, timedelta, timezone

import pytest

from leadparser.models import Lead, Message, Post, ThreadAnalysis
from leadparser.storage import Storage

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_post(pid: str, sub: str = "n8n") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key=f"reddit:{sub}",
        url=f"https://www.reddit.com/r/{sub}/comments/{pid}/", title=f"title {pid}",
        body="body text", author="bob", author_url="https://www.reddit.com/user/bob",
        created_at=NOW, flair=None, num_comments=2, stickied=False, removed=False,
        raw_json={"id": pid},
    )


def make_analysis(score: int) -> ThreadAnalysis:
    return ThreadAnalysis(
        problem_title_ru="Нужен разработчик бота",
        status="open",
        leads=[Lead(
            username="bob", score=score, quote="need a bot", quote_ru="нужен бот",
            lang="en", risk_level="green", risk_note="своя задача",
            why="прямой заказ", offer="предложить разработку",
        )],
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


def test_upsert_post_is_idempotent(store: Storage):
    store.upsert_post(make_post("a1"))
    store.upsert_post(make_post("a1"))
    assert len(store.posts_without_stage("prefilter", "v1")) == 1
    got = store.get_post("reddit:a1")
    assert got is not None and got.title == "title a1"
    assert got.created_at == NOW


def test_posts_without_stage_excludes_done_and_skipped(store: Storage):
    store.upsert_post(make_post("a1"))
    store.upsert_post(make_post("a2"))
    store.upsert_post(make_post("a3"))
    store.set_skip_reason("reddit:a3", "stickied")
    store.save_stage_result("reddit:a1", "prefilter", "v1", "ok", {"candidate": True})
    pending = [p.thread_id for p in store.posts_without_stage("prefilter", "v1")]
    assert pending == ["reddit:a2"]


def test_stage_result_is_versioned(store: Storage):
    store.upsert_post(make_post("a1"))
    store.save_stage_result("reddit:a1", "prefilter", "v1", "ok", {"candidate": True})
    assert store.posts_without_stage("prefilter", "v1") == []
    assert [p.thread_id for p in store.posts_without_stage("prefilter", "v2")] == ["reddit:a1"]


def test_error_status_is_retried_next_run(store: Storage):
    store.upsert_post(make_post("a1"))
    store.save_stage_result("reddit:a1", "score", "v1", "error", error="bad json")
    assert [p.thread_id for p in store.posts_without_stage("score", "v1")] == ["reddit:a1"]
    assert store.pending_count() == 1


def test_only_candidates_filter(store: Storage):
    store.upsert_post(make_post("a1"))
    store.upsert_post(make_post("a2"))
    store.save_stage_result("reddit:a1", "prefilter", "v1", "ok", {"candidate": True})
    store.save_stage_result("reddit:a2", "prefilter", "v1", "ok", {"candidate": False})
    pending = store.posts_without_stage(
        "score", "v1", only_candidates=True, candidate_prompt_version="v1"
    )
    assert [p.thread_id for p in pending] == ["reddit:a1"]


def test_messages_roundtrip_sorted(store: Storage):
    store.upsert_post(make_post("a1"))
    store.save_messages("reddit:a1", [
        Message(thread_id="reddit:a1", msg_id="c2", parent_id="c1", author="ann",
                body="second", created_at=NOW + timedelta(minutes=5), depth=1, score=2),
        Message(thread_id="reddit:a1", msg_id="c1", parent_id=None, author="bob",
                body="first", created_at=NOW, depth=0, score=5),
    ])
    msgs = store.get_messages("reddit:a1")
    assert [m.msg_id for m in msgs] == ["c1", "c2"]
    assert msgs[1].parent_id == "c1"


def test_analysis_roundtrip_and_report_filter(store: Storage):
    store.upsert_post(make_post("a1"))
    store.upsert_post(make_post("a2"))
    run_id = store.start_run()
    store.save_analysis("reddit:a1", "v1", False, make_analysis(82), run_id)
    store.save_analysis("reddit:a2", "v1", True, make_analysis(40), run_id)
    loaded = store.get_analysis("reddit:a1")
    assert loaded is not None and loaded.leads[0].score == 82
    rows = store.threads_for_report(min_score=55)
    assert [post.thread_id for post, _, _ in rows] == ["reddit:a1"]
    assert rows[0][2] is False
    store.finish_run(run_id, "ok", {"threads": 2})


def test_threads_for_report_sorted_by_max_score(store: Storage):
    run_id = store.start_run()
    for pid, score in (("a1", 60), ("a2", 88), ("a3", 72)):
        store.upsert_post(make_post(pid))
        store.save_analysis(f"reddit:{pid}", "v1", False, make_analysis(score), run_id)
    rows = store.threads_for_report(min_score=55)
    assert [p.thread_id for p, _, _ in rows] == ["reddit:a2", "reddit:a3", "reddit:a1"]


def test_stage_counts(store: Storage):
    store.upsert_post(make_post("a1"))
    store.save_stage_result("reddit:a1", "score", "v1", "ok", {"x": 1})
    store.save_stage_result("reddit:a2", "score", "v1", "quota")
    counts = store.stage_counts()
    assert counts["score"]["ok"] == 1
    assert counts["score"]["quota"] == 1


def test_threads_for_report_scoped_by_since(store: Storage):
    """A3: отчёт режется по окну времени треда, а не по run_id.

    `analyses.run_id` перезаписывается на каждом save_analysis, поэтому фильтр
    по прогону врал: возобновлённый прогон терял лиды прошлого прогона.
    """
    run_id = store.start_run()
    fresh = make_post("new1")
    old = make_post("old1").model_copy(update={"created_at": NOW - timedelta(days=30)})
    for p in (fresh, old):
        store.upsert_post(p)
        store.save_analysis(p.thread_id, "v1", False, make_analysis(82), run_id)

    window = store.threads_for_report(min_score=55, since=NOW - timedelta(days=7))
    assert [p.thread_id for p, _, _ in window] == ["reddit:new1"]
    everything = store.threads_for_report(min_score=55)
    assert {p.thread_id for p, _, _ in everything} == {"reddit:new1", "reddit:old1"}


def test_threads_for_report_ignores_which_run_analysed_the_thread(store: Storage):
    """A3: анализ, сделанный в прошлом прогоне, остаётся в отчёте следующего."""
    first_run = store.start_run()
    store.upsert_post(make_post("a1"))
    store.save_analysis("reddit:a1", "v1", False, make_analysis(82), first_run)
    second_run = store.start_run()
    store.upsert_post(make_post("a2"))
    store.save_analysis("reddit:a2", "v1", False, make_analysis(82), second_run)

    rows = store.threads_for_report(min_score=55, since=NOW - timedelta(days=1))
    assert {p.thread_id for p, _, _ in rows} == {"reddit:a1", "reddit:a2"}


def test_only_candidates_respects_prefilter_prompt_version(store: Storage):
    """B3: вердикт старой версии промпта не должен пускать тред в скоринг.

    Документированный рабочий приём («правишь промпт — бампни версию») иначе
    молча не срабатывает: `prefilter_v2` ещё не считал тред кандидатом, а
    ступень score уже берёт его в работу по вердикту `prefilter_v1`.
    """
    store.upsert_post(make_post("a1"))
    store.save_stage_result(
        "reddit:a1", "prefilter", "prefilter_v1", "ok", {"candidate": True}
    )
    stale = store.posts_without_stage(
        "score", "scorer_v1", only_candidates=True,
        candidate_prompt_version="prefilter_v2",
    )
    assert stale == []

    fresh = store.posts_without_stage(
        "score", "scorer_v1", only_candidates=True,
        candidate_prompt_version="prefilter_v1",
    )
    assert [p.thread_id for p in fresh] == ["reddit:a1"]
