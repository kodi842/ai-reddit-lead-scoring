# tests/test_prefilter.py
import threading
from datetime import datetime, timezone

import pytest

from leadparser.config import StageConfig
from leadparser.llm.base import LLMError, QuotaExhausted
from leadparser.models import Post
from leadparser.prompts import load_prompt
from leadparser.stages.prefilter import (
    PROMPT_VERSION,
    build_batches,
    render_batch,
    run_prefilter,
)
from leadparser.storage import Storage

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
STAGE = StageConfig(backend="claude_code", model="haiku", effort="low", batch_size=2)


def post(pid: str, title: str = "Need a scraper", body: str = "paying $200") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key="reddit:n8n",
        url=f"https://www.reddit.com/r/n8n/comments/{pid}/", title=title, body=body,
        author="bob", author_url="https://www.reddit.com/user/bob", created_at=NOW,
        flair=None, num_comments=1, stickied=False, removed=False, raw_json={},
    )


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


def test_prompt_file_loads_and_mentions_criteria():
    text = load_prompt(PROMPT_VERSION)
    assert "candidate" in text
    assert "freelance studio" in text.lower()
    assert "paying client" in text.lower()


def test_build_batches_splits_by_size():
    posts = [post(f"a{i}") for i in range(5)]
    batches = build_batches(posts, 2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_build_batches_handles_zero_size():
    posts = [post("a1"), post("a2")]
    assert [len(b) for b in build_batches(posts, 0)] == [1, 1]


def test_render_batch_contains_ids_and_truncates_body():
    long_post = post("a1", body="x" * 5000)
    rendered = render_batch([long_post, post("a2")])
    assert "reddit:a1" in rendered and "reddit:a2" in rendered
    assert "r/n8n" in rendered
    assert rendered.count("x") <= 1500


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, system, user, schema, model, effort):
        self.calls.append({"system": system, "user": user, "model": model})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_run_prefilter_stores_verdicts(store: Storage):
    posts = [post("a1"), post("a2")]
    for p in posts:
        store.upsert_post(p)
    backend = FakeBackend([{
        "items": [
            {"id": "reddit:a1", "candidate": True, "reason": "wants to hire"},
            {"id": "reddit:a2", "candidate": False, "reason": "just showing off"},
        ]
    }])
    counters = run_prefilter(store, backend, STAGE, posts)
    assert counters.ok == 2 and counters.error == 0
    assert store.get_stage_result("reddit:a1", "prefilter", PROMPT_VERSION)["payload"] == {
        "candidate": True, "reason": "wants to hire",
    }
    assert backend.calls[0]["model"] == "haiku"
    assert [p.thread_id for p in store.posts_without_stage(
        "score", "scorer_v1", only_candidates=True,
        candidate_prompt_version=PROMPT_VERSION,
    )] == ["reddit:a1"]


def test_run_prefilter_marks_missing_ids_as_error(store: Storage):
    posts = [post("a1"), post("a2")]
    for p in posts:
        store.upsert_post(p)
    backend = FakeBackend([{
        "items": [{"id": "reddit:a1", "candidate": True, "reason": "ok"}]
    }])
    counters = run_prefilter(store, backend, STAGE, posts)
    assert counters.ok == 1 and counters.error == 1
    assert store.get_stage_result("reddit:a2", "prefilter", PROMPT_VERSION)["status"] == "error"


def test_run_prefilter_records_error_for_failed_batch(store: Storage):
    posts = [post("a1"), post("a2")]
    for p in posts:
        store.upsert_post(p)
    backend = FakeBackend([LLMError("bad json")])
    counters = run_prefilter(store, backend, STAGE, posts)
    assert counters.error == 2
    assert store.get_stage_result("reddit:a1", "prefilter", PROMPT_VERSION)["status"] == "error"


def test_run_prefilter_aborts_on_quota(store: Storage):
    posts = [post("a1"), post("a2"), post("a3"), post("a4")]
    for p in posts:
        store.upsert_post(p)
    backend = FakeBackend([QuotaExhausted("limit")])
    with pytest.raises(QuotaExhausted):
        run_prefilter(store, backend, StageConfig(
            backend="claude_code", model="haiku", effort="low", batch_size=2
        ), posts)
    assert store.get_stage_result("reddit:a1", "prefilter", PROMPT_VERSION)["status"] == "quota"
    assert store.pending_count() >= 1


def test_run_prefilter_ignores_unknown_ids_from_model(store: Storage):
    posts = [post("a1")]
    store.upsert_post(posts[0])
    backend = FakeBackend([{
        "items": [
            {"id": "reddit:a1", "candidate": True, "reason": "ok"},
            {"id": "reddit:hallucinated", "candidate": True, "reason": "ghost"},
        ]
    }])
    counters = run_prefilter(store, backend, STAGE, posts)
    assert counters.ok == 1
    assert store.get_stage_result("reddit:hallucinated", "prefilter", PROMPT_VERSION) is None


def test_run_prefilter_accepts_bare_ids_from_model(store: Storage):
    """B4: голый id вместо `reddit:<id>` не должен ронять весь батч в error.

    Такой батч ретраился бы каждым прогоном вечно — сигналом был бы только
    растущий счётчик «ждут моего разбора».
    """
    posts = [post("a1"), post("a2")]
    for p in posts:
        store.upsert_post(p)
    backend = FakeBackend([{
        "items": [
            {"id": "a1", "candidate": True, "reason": "wants to hire"},
            {"id": "a2", "candidate": False, "reason": "just showing off"},
        ]
    }])
    counters = run_prefilter(store, backend, STAGE, posts)
    assert counters.ok == 2 and counters.error == 0
    assert store.get_stage_result("reddit:a1", "prefilter", PROMPT_VERSION)["payload"] == {
        "candidate": True, "reason": "wants to hire",
    }


def test_run_prefilter_prefers_full_id_over_bare_id(store: Storage):
    """Полный id остаётся главным: голый — только запасной ключ."""
    posts = [post("a1")]
    store.upsert_post(posts[0])
    backend = FakeBackend([{
        "items": [
            {"id": "a1", "candidate": False, "reason": "запасной ключ"},
            {"id": "reddit:a1", "candidate": True, "reason": "точный ключ"},
        ]
    }])
    run_prefilter(store, backend, STAGE, posts)
    payload = store.get_stage_result("reddit:a1", "prefilter", PROMPT_VERSION)["payload"]
    assert payload == {"candidate": True, "reason": "точный ключ"}


class ThreadSafeBackend:
    """Фейковый бэкенд для многопоточных проверок: `list.pop(0)` тут не годится."""

    def __init__(self, quota_after: int | None = None):
        self.quota_after = quota_after
        self.calls = 0
        self._lock = threading.Lock()

    def complete_json(self, system, user, schema, model, effort):
        with self._lock:
            self.calls += 1
            number = self.calls
        if self.quota_after is not None and number > self.quota_after:
            raise QuotaExhausted("limit")
        ids = [
            line.split("id: ")[1].strip()
            for line in user.splitlines() if line.startswith("### id: ")
        ]
        return {"items": [{"id": i, "candidate": True, "reason": "ok"} for i in ids]}


def test_run_prefilter_with_concurrency_keeps_storage_off_worker_threads(store: Storage):
    """C1: предфильтр — самая многовызовная ступень, ей нужен параллелизм.

    Storage открывает sqlite3 с check_same_thread=True, поэтому worker обязан
    получить готовый текст батча и не трогать базу — как в run_scorer.
    """
    posts = [post(f"a{i}") for i in range(6)]
    for p in posts:
        store.upsert_post(p)
    backend = ThreadSafeBackend()
    counters = run_prefilter(store, backend, STAGE, posts, concurrency=3)
    assert counters.ok == 6 and counters.error == 0
    for p in posts:
        assert store.get_stage_result(
            p.thread_id, "prefilter", PROMPT_VERSION
        )["status"] == "ok"


def test_run_prefilter_quota_bookkeeping_under_concurrency(store: Storage):
    """C1: при лимите ни один пост не остаётся без строки, статусы честные."""
    posts = [post(f"a{i}") for i in range(6)]
    for p in posts:
        store.upsert_post(p)
    backend = ThreadSafeBackend(quota_after=1)
    with pytest.raises(QuotaExhausted):
        run_prefilter(
            store,
            backend,
            StageConfig(backend="claude_code", model="haiku", effort="low", batch_size=1),
            posts,
            concurrency=3,
        )
    statuses = [
        (store.get_stage_result(p.thread_id, "prefilter", PROMPT_VERSION) or {}).get("status")
        for p in posts
    ]
    assert None not in statuses, "пост без строки ступени потерялся бы навсегда"
    assert set(statuses) <= {"ok", "quota"}
    assert "quota" in statuses
