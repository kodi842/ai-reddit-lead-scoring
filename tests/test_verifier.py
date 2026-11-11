# tests/test_verifier.py
from datetime import datetime, timezone

import pytest

from leadparser.config import StageConfig
from leadparser.llm.base import LLMError, QuotaExhausted
from leadparser.models import Lead, Message, Post, ThreadAnalysis
from leadparser.prompts import load_prompt
from leadparser.stages.verifier import PROMPT_VERSION, hot_threads, run_verifier
from leadparser.storage import Storage

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
STAGE = StageConfig(backend="claude_code", model="opus", effort="high", threshold=70)


def post(pid: str) -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key="reddit:n8n",
        url=f"https://www.reddit.com/r/n8n/comments/{pid}/", title="Need a bot",
        body="paying", author="bob", author_url="https://www.reddit.com/user/bob",
        created_at=NOW, flair=None, num_comments=1, stickied=False, removed=False,
        raw_json={},
    )


def analysis(score: int) -> ThreadAnalysis:
    return ThreadAnalysis(
        problem_title_ru="Нужен бот", status="open",
        leads=[Lead(
            username="bob", score=score, quote="paying", quote_ru="платим", lang="en",
            risk_level="green", risk_note="своя задача", why="есть оплата",
            offer="предложить бота",
        )],
    )


def payload(score: int) -> dict:
    return analysis(score).model_dump()


def seed(store: Storage, pid: str, score: int, run_id: int) -> Post:
    p = post(pid)
    store.upsert_post(p)
    store.save_messages(p.thread_id, [Message(
        thread_id=p.thread_id, msg_id=pid, parent_id=None, author="bob",
        body="paying", created_at=NOW, depth=0, score=1,
    )])
    store.save_analysis(p.thread_id, "scorer_v1", False, analysis(score), run_id)
    return p


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


def test_prompt_asks_for_stricter_review():
    text = load_prompt(PROMPT_VERSION)
    assert "strict" in text.lower() or "overrate" in text.lower()


def test_hot_threads_filters_by_threshold(store: Storage):
    run_id = store.start_run()
    seed(store, "a1", 88, run_id)
    seed(store, "a2", 65, run_id)
    assert [p.thread_id for p in hot_threads(store, 70)] == ["reddit:a1"]


def test_hot_threads_skips_already_verified(store: Storage):
    run_id = store.start_run()
    p = seed(store, "a1", 88, run_id)
    store.save_stage_result(p.thread_id, "verify", PROMPT_VERSION, "ok", {"max_score": 88})
    assert hot_threads(store, 70) == []


def test_hot_threads_retries_errored(store: Storage):
    run_id = store.start_run()
    p = seed(store, "a1", 88, run_id)
    store.save_stage_result(p.thread_id, "verify", PROMPT_VERSION, "error", error="x")
    assert [x.thread_id for x in hot_threads(store, 70)] == ["reddit:a1"]


def test_hot_threads_picks_up_threads_from_earlier_runs(store: Storage):
    """Тред, чья верификация упала по лимиту, должен доверифицироваться позже."""
    old_run = store.start_run()
    p = seed(store, "a1", 88, old_run)
    store.save_stage_result(p.thread_id, "verify", PROMPT_VERSION, "quota", error="лимит")
    store.start_run()
    assert [x.thread_id for x in hot_threads(store, 70)] == ["reddit:a1"]


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete_json(self, system, user, schema, model, effort):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_run_verifier_overwrites_analysis_and_marks_verified(store: Storage):
    run_id = store.start_run()
    p = seed(store, "a1", 88, run_id)
    backend = FakeBackend([payload(74)])
    counters = run_verifier(store, backend, STAGE, run_id, concurrency=1)
    assert counters.ok == 1
    saved = store.get_analysis(p.thread_id)
    assert saved is not None and saved.leads[0].score == 74
    rows = store.threads_for_report(min_score=55)
    assert rows[0][2] is True
    assert "previous analysis" in backend.prompts[0].lower()
    assert '"score": 88' in backend.prompts[0] or '"score":88' in backend.prompts[0]


def test_run_verifier_keeps_previous_analysis_on_error(store: Storage):
    run_id = store.start_run()
    p = seed(store, "a1", 88, run_id)
    backend = FakeBackend([LLMError("bad json")])
    counters = run_verifier(store, backend, STAGE, run_id, concurrency=1)
    assert counters.error == 1
    saved = store.get_analysis(p.thread_id)
    assert saved is not None and saved.leads[0].score == 88
    rows = store.threads_for_report(min_score=55)
    assert rows[0][2] is False


def test_run_verifier_aborts_on_quota(store: Storage):
    run_id = store.start_run()
    seed(store, "a1", 88, run_id)
    backend = FakeBackend([QuotaExhausted("limit")])
    with pytest.raises(QuotaExhausted):
        run_verifier(store, backend, STAGE, run_id, concurrency=1)
    assert store.get_stage_result("reddit:a1", "verify", PROMPT_VERSION)["status"] == "quota"


def test_run_verifier_no_hot_threads_is_noop(store: Storage):
    run_id = store.start_run()
    seed(store, "a1", 60, run_id)
    backend = FakeBackend([])
    counters = run_verifier(store, backend, STAGE, run_id, concurrency=1)
    assert counters.as_dict()["ok"] == 0


def test_run_verifier_with_concurrency_keeps_storage_off_worker_threads(store: Storage):
    # Storage открывает sqlite3.connect(...) с default check_same_thread=True, так
    # что любое обращение к storage из потока пула ThreadPoolExecutor валит
    # sqlite3.ProgrammingError. get_analysis/get_messages обязаны читаться в
    # основном потоке ещё до диспетчеризации по worker'ам — как в run_scorer.
    # До фикса этот тест падает: все треды получают status="error" вместо "ok"
    # (worker брифа читал storage прямо внутри себя, уже в потоке пула).
    run_id = store.start_run()
    posts = [seed(store, f"a{i}", 88, run_id) for i in range(1, 4)]
    backend = FakeBackend([payload(74) for _ in posts])
    counters = run_verifier(store, backend, STAGE, run_id, concurrency=3)
    assert counters.ok == len(posts)
    assert counters.error == 0
    rows = {row[0].thread_id: row for row in store.threads_for_report(min_score=55)}
    for p in posts:
        assert store.get_stage_result(p.thread_id, "verify", PROMPT_VERSION)["status"] == "ok"
        _saved_post, saved_analysis, verified = rows[p.thread_id]
        assert verified is True
        assert saved_analysis.leads[0].score == 74


def test_run_verifier_sends_post_body_when_comments_are_missing(store: Storage):
    """A2: путь верификатора тоже не должен слать модели пустой тред.

    Проверяем именно ту часть промпта, где лежит тред: заголовок и цитаты из
    прошлого анализа попадают туда и так, а тело поста раньше терялось.
    """
    run_id = store.start_run()
    p = post("a1").model_copy(update={"body": "бюджет 500 долларов, нужен парсер"})
    store.upsert_post(p)
    store.save_analysis(p.thread_id, "scorer_v1", False, analysis(88), run_id)
    backend = FakeBackend([payload(74)])
    run_verifier(store, backend, STAGE, run_id, concurrency=1)
    thread_part = backend.prompts[0].split("--- previous analysis ---")[0]
    assert "бюджет 500 долларов" in thread_part
