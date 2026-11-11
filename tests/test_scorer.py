# tests/test_scorer.py
from datetime import datetime, timedelta, timezone

import pytest

from leadparser.config import StageConfig
from leadparser.llm.base import LLMError, QuotaExhausted
from leadparser.models import Message, Post
from leadparser.prompts import load_prompt
from leadparser.stages.scorer import (
    MAX_MESSAGE_CHARS,
    MAX_TOTAL_CHARS,
    PROMPT_VERSION,
    render_thread,
    run_scorer,
)
from leadparser.storage import Storage

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
STAGE = StageConfig(backend="claude_code", model="sonnet", effort="medium")


def post(pid: str = "a1") -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key="reddit:n8n",
        url=f"https://www.reddit.com/r/n8n/comments/{pid}/",
        title="Need HubSpot to Sheets sync", body="I run an agency, paying $300",
        author="bob", author_url="https://www.reddit.com/user/bob", created_at=NOW,
        flair="Help", num_comments=2, stickied=False, removed=False, raw_json={},
    )


def messages(pid: str = "a1") -> list[Message]:
    return [
        Message(thread_id=f"reddit:{pid}", msg_id=pid, parent_id=None, author="bob",
                body="I run an agency, paying $300", created_at=NOW, depth=0, score=5),
        Message(thread_id=f"reddit:{pid}", msg_id="c1", parent_id=pid, author="ann",
                body="I can help, DM me", created_at=NOW + timedelta(minutes=5),
                depth=1, score=2),
    ]


def analysis_payload(score: int = 82) -> dict:
    return {
        "problem_title_ru": "Нужна синхронизация HubSpot и Google Sheets",
        "status": "open",
        "leads": [{
            "username": "bob", "score": score, "quote": "paying $300",
            "quote_ru": "платим 300 долларов", "lang": "en", "risk_level": "green",
            "risk_note": "своя задача, оплата названа", "why": "прямой заказ с оплатой",
            "offer": "предложить готовый воркфлоу за фикс",
        }],
    }


@pytest.fixture()
def store():
    s = Storage(":memory:")
    yield s
    s.close()


def test_prompt_mentions_score_bands():
    text = load_prompt(PROMPT_VERSION)
    for token in ("80", "70", "55", "quote_ru", "risk_level"):
        assert token in text


def test_render_thread_includes_post_and_comments():
    rendered = render_thread(post(), messages())
    assert "r/n8n" in rendered
    assert "Need HubSpot to Sheets sync" in rendered
    assert "u/bob" in rendered and "u/ann" in rendered
    assert "I can help, DM me" in rendered
    assert "OP" in rendered


def test_render_thread_truncates_long_messages():
    msgs = messages()
    # наполнитель кириллический: латинские буквы встречаются в самом тексте треда
    msgs[1] = msgs[1].model_copy(update={"body": "ж" * 5000})
    rendered = render_thread(post(), msgs)
    assert rendered.count("ж") == MAX_MESSAGE_CHARS


def test_render_thread_caps_total_size():
    msgs = [messages()[0]]
    for i in range(200):
        msgs.append(Message(
            thread_id="reddit:a1", msg_id=f"c{i}", parent_id="a1", author=f"u{i}",
            body="ю" * 1000, created_at=NOW + timedelta(minutes=i), depth=1, score=1,
        ))
    rendered = render_thread(post(), msgs)
    assert len(rendered) <= MAX_TOTAL_CHARS
    assert "I run an agency" in rendered


def test_render_thread_caps_total_size_with_many_short_messages():
    # Форма, на которой обнаружился баг: раньше "used" не считал ни SEPARATOR,
    # ни "\n\n" между строками, ни сам маркер обрезки. На треде с одним длинным
    # сообщением эти накладные расходы тонут в шуме, а на треде из множества
    # коротких реплик набегают в разы больше и итог заметно превышал лимит.
    msgs = [messages()[0]]
    for i in range(500):
        msgs.append(Message(
            thread_id="reddit:a1", msg_id=f"c{i}", parent_id="a1", author=f"u{i}",
            body="ok", created_at=NOW + timedelta(minutes=i), depth=1, score=1,
        ))
    rendered = render_thread(post(), msgs)
    assert len(rendered) <= MAX_TOTAL_CHARS


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, system, user, schema, model, effort):
        self.calls.append(model)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_run_scorer_saves_analysis(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, messages())
    run_id = store.start_run()
    backend = FakeBackend([analysis_payload()])
    counters = run_scorer(store, backend, STAGE, [p], run_id, concurrency=1)
    assert counters.ok == 1
    saved = store.get_analysis(p.thread_id)
    assert saved is not None and saved.leads[0].score == 82
    assert saved.problem_title_ru.startswith("Нужна")
    assert store.get_stage_result(p.thread_id, "score", PROMPT_VERSION)["status"] == "ok"
    rows = store.threads_for_report(min_score=55)
    assert rows[0][2] is False
    assert backend.calls == ["sonnet"]


def test_run_scorer_accepts_thread_without_leads(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, messages())
    run_id = store.start_run()
    backend = FakeBackend([{"problem_title_ru": "Болтовня", "status": "open", "leads": []}])
    counters = run_scorer(store, backend, STAGE, [p], run_id, concurrency=1)
    assert counters.ok == 1
    assert store.threads_for_report(min_score=55) == []


def test_run_scorer_records_error(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, messages())
    run_id = store.start_run()
    backend = FakeBackend([LLMError("bad json")])
    counters = run_scorer(store, backend, STAGE, [p], run_id, concurrency=1)
    assert counters.error == 1
    assert store.get_stage_result(p.thread_id, "score", PROMPT_VERSION)["status"] == "error"
    assert store.get_analysis(p.thread_id) is None


def test_run_scorer_records_error_on_schema_violation(store: Storage):
    p = post()
    store.upsert_post(p)
    store.save_messages(p.thread_id, messages())
    run_id = store.start_run()
    bad = analysis_payload()
    bad["leads"][0]["risk_level"] = "chartreuse"
    backend = FakeBackend([bad])
    counters = run_scorer(store, backend, STAGE, [p], run_id, concurrency=1)
    assert counters.error == 1


def test_run_scorer_aborts_on_quota(store: Storage):
    posts = [post("a1"), post("a2")]
    for p in posts:
        store.upsert_post(p)
        store.save_messages(p.thread_id, messages(p.thread_id.split(":")[1]))
    run_id = store.start_run()
    backend = FakeBackend([QuotaExhausted("limit"), analysis_payload()])
    with pytest.raises(QuotaExhausted):
        run_scorer(store, backend, STAGE, posts, run_id, concurrency=1)
    statuses = {
        p.thread_id: (store.get_stage_result(p.thread_id, "score", PROMPT_VERSION) or {}).get("status")
        for p in posts
    }
    assert "quota" in statuses.values()


def test_run_scorer_with_concurrency_keeps_storage_off_worker_threads(store: Storage):
    # Storage открывает sqlite3.connect(...) с default check_same_thread=True,
    # так что любое обращение к storage из потока пула ThreadPoolExecutor валит
    # sqlite3.ProgrammingError. render_thread(post, storage.get_messages(...))
    # обязан выполняться в основном потоке ещё до диспетчеризации по worker'ам.
    # До фикса этот тест падает: все посты получают status="error" вместо "ok".
    posts = [post("a1"), post("a2"), post("a3")]
    for p in posts:
        store.upsert_post(p)
        store.save_messages(p.thread_id, messages(p.thread_id.split(":")[1]))
    run_id = store.start_run()
    backend = FakeBackend([analysis_payload() for _ in posts])
    counters = run_scorer(store, backend, STAGE, posts, run_id, concurrency=3)
    assert counters.ok == len(posts)
    assert counters.error == 0
    for p in posts:
        assert store.get_stage_result(p.thread_id, "score", PROMPT_VERSION)["status"] == "ok"
        assert store.get_analysis(p.thread_id) is not None


def test_render_thread_falls_back_to_post_body_without_comments():
    """A2: без комментариев модель обязана увидеть хотя бы сам пост.

    Раньше промпт состоял из одного заголовка и пустого тела треда — модель
    оценивала тред вслепую, а результат кэшировался как ok навсегда.
    """
    rendered = render_thread(post(), [])
    assert "I run an agency, paying $300" in rendered
    assert "Need HubSpot to Sheets sync" in rendered
    assert "u/bob" in rendered


def test_run_scorer_without_messages_sends_post_body_to_model(store: Storage):
    """A2: тот же путь целиком — от run_scorer до промпта бэкенда."""
    p = post()
    store.upsert_post(p)
    run_id = store.start_run()
    prompts: list[str] = []

    class CapturingBackend(FakeBackend):
        def complete_json(self, system, user, schema, model, effort):
            prompts.append(user)
            return super().complete_json(system, user, schema, model, effort)

    counters = run_scorer(
        store, CapturingBackend([analysis_payload()]), STAGE, [p], run_id, concurrency=1
    )
    assert counters.ok == 1
    assert "I run an agency, paying $300" in prompts[0]
