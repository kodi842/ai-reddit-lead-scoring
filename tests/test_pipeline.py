# tests/test_pipeline.py
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadparser.config import (
    BackendConfig,
    Config,
    HeuristicConfig,
    LLMConfig,
    RedditConfig,
    ReportConfig,
    StageConfig,
)
from leadparser.llm.base import QuotaExhausted
from leadparser.models import Message, Post
from leadparser.pipeline import Pipeline
from leadparser.sources.reddit import RedditSource
from leadparser.storage import Storage

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path) -> Config:
    return Config(
        lookback_days=7,
        db_path=tmp_path / "leads.sqlite",
        reports_dir=tmp_path / "reports",
        reddit=RedditConfig(subreddits=["n8n"]),
        heuristic=HeuristicConfig(stop_phrases=["[for hire]"], stop_authors=["AutoModerator"]),
        llm=LLMConfig(
            concurrency=1,
            backends={"fake": BackendConfig(type="claude_code")},
            stages={
                "prefilter": StageConfig(backend="fake", model="haiku", effort="low", batch_size=10),
                "score": StageConfig(backend="fake", model="sonnet", effort="medium"),
                "verify": StageConfig(backend="fake", model="opus", effort="high", threshold=70),
            },
        ),
        report=ReportConfig(min_score=55),
    )


def post(pid: str, author: str = "bob", stickied: bool = False) -> Post:
    return Post(
        thread_id=f"reddit:{pid}", source_key="reddit:n8n",
        url=f"https://www.reddit.com/r/n8n/comments/{pid}/", title=f"Need help {pid}",
        body="paying", author=author, author_url=f"https://www.reddit.com/user/{author}",
        created_at=NOW - timedelta(days=1), flair=None, num_comments=1,
        stickied=stickied, removed=False, raw_json={},
    )


class FakeSource:
    name = "reddit"

    def __init__(self, posts: list[Post]):
        self.posts = posts
        self.fetched: list[str] = []
        self.since: datetime | None = None

    def list_new_posts(self, since: datetime):
        self.since = since
        yield from self.posts

    def fetch_thread(self, thread_id: str) -> list[Message]:
        self.fetched.append(thread_id)
        return [Message(
            thread_id=thread_id, msg_id=thread_id.split(":")[1], parent_id=None,
            author="bob", body="paying", created_at=NOW - timedelta(days=1),
            depth=0, score=1,
        )]


class ScriptedBackend:
    name = "fake"

    def __init__(self):
        self.models: list[str] = []
        self.quota_on: str | None = None

    def complete_json(self, system, user, schema, model, effort):
        self.models.append(model)
        if self.quota_on == model:
            raise QuotaExhausted("лимит")
        if "items" in (schema.get("properties") or {}):
            ids = [
                line.split("id: ")[1].strip()
                for line in user.splitlines() if line.startswith("### id: ")
            ]
            return {"items": [
                {"id": i, "candidate": True, "reason": "wants to hire"} for i in ids
            ]}
        score = 82 if model == "sonnet" else 76
        return {
            "problem_title_ru": "Нужен разработчик автоматизации",
            "status": "open",
            "leads": [{
                "username": "bob", "score": score, "quote": "paying",
                "quote_ru": "платим", "lang": "en", "risk_level": "green",
                "risk_note": "своя задача", "why": "прямой заказ",
                "offer": "предложить воркфлоу",
            }],
        }


def build(tmp_path: Path, posts: list[Post]):
    cfg = make_config(tmp_path)
    storage = Storage(cfg.db_path)
    source = FakeSource(posts)
    backend = ScriptedBackend()
    pipeline = Pipeline(
        cfg, storage, {"reddit": source}, {"fake": backend}, now_factory=lambda: NOW
    )
    return cfg, storage, source, backend, pipeline


def test_full_run_produces_report(tmp_path: Path):
    cfg, storage, source, backend, pipeline = build(tmp_path, [post("a1"), post("a2")])
    result = pipeline.run()
    assert result.status == "ok"
    assert result.stats["collected"] == 2
    assert result.stats["prefilter"]["ok"] == 2
    assert result.stats["score"]["ok"] == 2
    assert result.stats["verify"]["ok"] == 2
    assert source.fetched == ["reddit:a1", "reddit:a2"]
    assert backend.models[:1] == ["haiku"]
    assert "sonnet" in backend.models and "opus" in backend.models
    assert result.report_path is not None and result.report_path.exists()
    html = result.report_path.read_text(encoding="utf-8")
    assert "✅ проверено" in html and "<td class='sc'>76</td>" in html
    storage.close()


def test_since_uses_lookback_days(tmp_path: Path):
    cfg, storage, source, _b, pipeline = build(tmp_path, [post("a1")])
    pipeline.run(write_html=False)
    assert source.since == NOW - timedelta(days=7)
    storage.close()


def test_heuristic_skipped_posts_never_reach_llm(tmp_path: Path):
    cfg, storage, source, backend, pipeline = build(
        tmp_path, [post("a1"), post("bad", author="AutoModerator", stickied=True)]
    )
    result = pipeline.run(write_html=False)
    assert result.stats["skipped"] == 1
    assert result.stats["prefilter"]["ok"] == 1
    assert source.fetched == ["reddit:a1"]
    storage.close()


def test_max_posts_limits_collection(tmp_path: Path):
    cfg, storage, _s, _b, pipeline = build(
        tmp_path, [post("a1"), post("a2"), post("a3")]
    )
    result = pipeline.run(max_posts=2, write_html=False)
    assert result.stats["collected"] == 2
    storage.close()


def test_second_run_reuses_stage_results(tmp_path: Path):
    cfg, storage, _s, backend, pipeline = build(tmp_path, [post("a1")])
    pipeline.run(write_html=False)
    first_calls = len(backend.models)
    result = pipeline.run(write_html=False)
    assert len(backend.models) == first_calls
    assert result.stats["prefilter"]["ok"] == 0
    assert result.stats["score"]["ok"] == 0
    storage.close()


def test_comments_are_fetched_once(tmp_path: Path):
    cfg, storage, source, _b, pipeline = build(tmp_path, [post("a1")])
    pipeline.run(write_html=False)
    pipeline.run(write_html=False)
    assert source.fetched == ["reddit:a1"]
    storage.close()


def test_quota_pauses_run_and_keeps_progress(tmp_path: Path):
    cfg, storage, _s, backend, pipeline = build(tmp_path, [post("a1")])
    backend.quota_on = "sonnet"
    result = pipeline.run(write_html=False)
    assert result.status == "paused"
    assert result.stats["paused_at"] == "score"
    # The score stage raised before returning its own counters, so the pipeline
    # cannot honestly report a count for it — it must report nothing, not a guess.
    assert "score" not in result.stats
    assert storage.get_stage_result("reddit:a1", "prefilter", "prefilter_v1")["status"] == "ok"
    assert storage.get_stage_result("reddit:a1", "score", "scorer_v1")["status"] == "quota"
    backend.quota_on = None
    again = pipeline.run(write_html=False)
    assert again.status == "ok"
    assert again.stats["score"]["ok"] == 1
    storage.close()


def test_run_without_sources_is_not_an_error(tmp_path: Path):
    cfg, storage, _s, _b, pipeline = build(tmp_path, [])
    result = pipeline.run(write_html=False)
    assert result.status == "ok"
    assert result.stats["collected"] == 0
    storage.close()


def test_paused_score_stats_do_not_leak_other_runs_successes(tmp_path: Path):
    """A pause during score must report only this run's work, not sitewide history.

    Regression for: the old QuotaExhausted handler backfilled missing stage counts
    from storage.stage_counts(), which has no run_id column — it counts every thread
    ever put into that status, across all runs. A run that scored zero posts this
    time could still report an "ok" success that actually belongs to an earlier run.
    """
    cfg, storage, source, backend, pipeline = build(tmp_path, [post("a1")])
    first = pipeline.run(write_html=False)
    assert first.status == "ok"
    assert first.stats["score"]["ok"] == 1

    source.posts.append(post("a2"))
    backend.quota_on = "sonnet"
    result = pipeline.run(write_html=False)
    assert result.status == "paused"
    assert result.stats["paused_at"] == "score"
    assert result.stats.get("score", {}).get("ok", 0) == 0
    storage.close()


def test_paused_prefilter_reports_no_score_entry_from_earlier_run(tmp_path: Path):
    """A pause during prefilter must not fabricate a score entry from a stale row.

    Regression for: the old handler backfilled *every* stage name, including ones
    never touched this run — a run that paused in prefilter and never called the
    scorer could still report a "quota" count for score, purely because an earlier
    run had left that status in the database.
    """
    cfg, storage, source, backend, pipeline = build(tmp_path, [post("a1")])
    backend.quota_on = "sonnet"
    first = pipeline.run(write_html=False)
    assert first.status == "paused"

    source.posts.append(post("a2"))
    backend.quota_on = "haiku"
    result = pipeline.run(write_html=False)
    assert result.status == "paused"
    assert result.stats["paused_at"] == "prefilter"
    assert "score" not in result.stats
    storage.close()


class CountingSource(FakeSource):
    """Источник, умеющий отчитаться о сорванных целях сбора (как RedditSource)."""

    def __init__(self, posts: list[Post], targets: int, failures: list[str]):
        super().__init__(posts)
        self._targets = targets
        self._failures = failures

    def collection_targets(self) -> int:
        return self._targets

    def collection_failures(self) -> list[str]:
        return list(self._failures)


def build_with_source(tmp_path: Path, source):
    cfg = make_config(tmp_path)
    storage = Storage(cfg.db_path)
    backend = ScriptedBackend()
    pipeline = Pipeline(
        cfg, storage, {"reddit": source}, {"fake": backend}, now_factory=lambda: NOW
    )
    return cfg, storage, backend, pipeline


def test_total_collection_failure_is_surfaced_in_stats(tmp_path: Path):
    """A1: все сабреддиты упали — прогон не имеет права выглядеть успешным."""
    source = CountingSource(
        [], targets=3, failures=["r/webdev: 403", "r/n8n: 403", "r/hiring: 403"]
    )
    cfg, storage, _b, pipeline = build_with_source(tmp_path, source)
    result = pipeline.run(write_html=False)
    assert result.stats["collected"] == 0
    assert result.stats["source_targets"] == 3
    assert result.stats["source_failures"] == 3
    assert result.stats["all_sources_failed"] is True
    assert "r/webdev: 403" in result.stats["failed_sources"]
    storage.close()


def test_empty_but_healthy_collection_is_not_a_failure(tmp_path: Path):
    """A1: «всё сработало, нового нет» остаётся успехом."""
    source = CountingSource([], targets=3, failures=[])
    cfg, storage, _b, pipeline = build_with_source(tmp_path, source)
    result = pipeline.run(write_html=False)
    assert result.stats["source_failures"] == 0
    assert result.stats["all_sources_failed"] is False
    storage.close()


class BrokenCommentsSource(FakeSource):
    def fetch_thread(self, thread_id: str):
        self.fetched.append(thread_id)
        raise RuntimeError("403 на комментариях")


def test_failed_comment_fetch_is_retried_not_scored_blind(tmp_path: Path):
    """A2: тред без комментариев нельзя скорить вслепую и кэшировать как ok."""
    source = BrokenCommentsSource([post("a1")])
    cfg, storage, backend, pipeline = build_with_source(tmp_path, source)
    result = pipeline.run(write_html=False)

    assert "sonnet" not in backend.models
    assert result.stats["score"]["ok"] == 0
    row = storage.get_stage_result("reddit:a1", "score", "scorer_v1")
    assert row is not None and row["status"] == "error"
    pending = storage.posts_without_stage(
        "score", "scorer_v1", only_candidates=True,
        candidate_prompt_version="prefilter_v1",
    )
    assert [p.thread_id for p in pending] == ["reddit:a1"]
    storage.close()


def test_report_covers_lookback_window_not_just_this_run(tmp_path: Path):
    """A3: возобновлённый прогон не имеет права терять лиды прошлого прогона."""
    cfg, storage, source, backend, pipeline = build(tmp_path, [post("a1")])
    first = pipeline.run()
    assert first.report_path is not None
    assert "comments/a1/" in first.report_path.read_text(encoding="utf-8")

    source.posts.append(post("a2"))
    second = pipeline.run()
    assert second.report_path is not None
    html = second.report_path.read_text(encoding="utf-8")
    assert "comments/a2/" in html
    assert "comments/a1/" in html
    storage.close()


def test_report_excludes_threads_older_than_the_window(tmp_path: Path):
    """A3: окно отчёта — это окно прогона, а не вся история базы."""
    old = post("old1").model_copy(update={"created_at": NOW - timedelta(days=30)})
    cfg, storage, source, backend, pipeline = build(tmp_path, [old, post("a1")])
    result = pipeline.run()
    assert result.report_path is not None
    html = result.report_path.read_text(encoding="utf-8")
    assert "comments/a1/" in html
    assert "comments/old1/" not in html
    storage.close()


def test_stage_failure_still_renders_report_and_finishes_run(tmp_path: Path):
    """B2: ошибка между start_run и finish_run не должна бросать прогон.

    Раньше любое исключение не-QuotaExhausted пролетало сквозь run(): строка
    прогона навсегда оставалась в статусе running, отчёт не рендерился, а
    оплаченная работа ступеней нигде не показывалась.
    """
    cfg = make_config(tmp_path)
    cfg.llm.stages["verify"] = StageConfig(
        backend="его-нет", model="opus", effort="high", threshold=70
    )
    storage = Storage(cfg.db_path)
    pipeline = Pipeline(
        cfg, storage, {"reddit": FakeSource([post("a1")])},
        {"fake": ScriptedBackend()}, now_factory=lambda: NOW,
    )
    result = pipeline.run()

    assert result.status == "error"
    assert result.stats["failed_at"] == "verify"
    assert result.stats["score"]["ok"] == 1
    assert result.report_path is not None and result.report_path.exists()
    row = storage.conn.execute(
        "SELECT status FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row["status"] == "error"
    storage.close()


def test_progress_callback_reports_every_stage(tmp_path: Path):
    """C4: за часы прогона владелец должен видеть, что идёт и сколько осталось.

    Пайплайн не знает про CLI: он зовёт переданный callback, по умолчанию —
    ничего (поэтому тесты молчат).
    """
    events: list[tuple[str, int, int | None]] = []
    cfg = make_config(tmp_path)
    storage = Storage(cfg.db_path)
    pipeline = Pipeline(
        cfg, storage, {"reddit": FakeSource([post("a1"), post("a2")])},
        {"fake": ScriptedBackend()}, now_factory=lambda: NOW,
        progress=lambda stage, done, total: events.append((stage, done, total)),
    )
    pipeline.run(write_html=False)

    assert {"collect", "prefilter", "score", "verify"} <= {e[0] for e in events}
    for stage in ("prefilter", "score", "verify"):
        stage_events = [e for e in events if e[0] == stage]
        assert stage_events, f"нет прогресса по ступени {stage}"
        done, total = stage_events[-1][1], stage_events[-1][2]
        assert total and done == total, f"{stage}: {done}/{total}"
        assert [e[1] for e in stage_events] == sorted(e[1] for e in stage_events)
    storage.close()


def test_pipeline_without_progress_callback_stays_silent(tmp_path: Path):
    cfg, storage, _s, _b, pipeline = build(tmp_path, [post("a1")])
    assert pipeline.run(write_html=False).status == "ok"
    storage.close()


def test_collection_failure_still_finishes_the_run(tmp_path: Path):
    """B2: сбор — тоже «между start_run и finish_run».

    Источник, упавший целиком (не один сабреддит, а, скажем, ещё не
    реализованный Facebook или сбой записи в базу), не должен оставлять
    строку прогона в статусе running.
    """

    class ExplodingSource(FakeSource):
        def list_new_posts(self, since: datetime):
            raise RuntimeError("источник не поднялся")
            yield  # pragma: no cover — делает метод генератором

    cfg = make_config(tmp_path)
    storage = Storage(cfg.db_path)
    pipeline = Pipeline(
        cfg, storage, {"reddit": ExplodingSource([])}, {"fake": ScriptedBackend()},
        now_factory=lambda: NOW,
    )
    result = pipeline.run(write_html=False)

    assert result.status == "error"
    assert result.stats["failed_at"] == "collect"
    row = storage.conn.execute(
        "SELECT status FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row["status"] == "error"
    storage.close()


class PartlyFailingRssClient:
    """Один сабреддит отвечает Atom-лентой, другой срывается целиком.

    В RSS нет пагинации (один запрос — вся лента до `listing_limit`), поэтому
    у нынешнего эквивалента старого «страница 1 ок, страница 2 — 429» сбой
    случается на уровне целого сабреддита, а не половины его листинга.
    """

    _ENTRY = (
        '<entry><author><name>/u/bob</name></author>'
        '<content type="html">paying</content><id>t3_a1</id>'
        '<link href="https://www.reddit.com/r/n8n/comments/a1/need_help/" />'
        '<updated>{updated}</updated><title>Need help a1</title></entry>'
    )

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        if "/r/broken/" in url:
            raise RuntimeError("429 у r/broken")
        entry = self._ENTRY.format(updated=(NOW - timedelta(days=1)).isoformat())
        return f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'


def test_partial_collection_is_not_a_total_failure(tmp_path: Path):
    """Сабреддит, отдавший ленту, и сабреддит, сорвавшийся целиком, — это не
    «собирать было нечего».

    Регрессия волны фиксов: сбой засчитывался целиком, `all_sources_failed`
    становился True при непустом сборе, и CLI печатал «ни один источник не
    ответил» с кодом 1 — при том, что посты собраны, разобраны и в отчёте.
    """
    cfg = make_config(tmp_path)
    cfg.reddit = RedditConfig(subreddits=["n8n", "broken"])
    storage = Storage(cfg.db_path)
    source = RedditSource(PartlyFailingRssClient(), cfg.reddit)
    pipeline = Pipeline(
        cfg, storage, {"reddit": source}, {"fake": ScriptedBackend()},
        now_factory=lambda: NOW,
    )
    result = pipeline.run(write_html=False)

    assert result.status == "ok"
    assert result.stats["collected"] == 1
    assert result.stats["score"]["ok"] == 1
    # Сбой всё равно виден — его печатают красным...
    assert result.stats["source_failures"] == 1
    assert "429" in result.stats["failed_sources"][0]
    # ...но это не «собирать было нечего», и это не код возврата 1.
    assert result.stats["all_sources_failed"] is False
    storage.close()


def test_total_failure_still_detected_when_nothing_was_collected(tmp_path: Path):
    """Обратная сторона: пустой сбор при всех сорванных целях — по-прежнему провал."""
    source = CountingSource([], targets=2, failures=["r/a: 403", "r/b: 403"])
    cfg, storage, _b, pipeline = build_with_source(tmp_path, source)
    result = pipeline.run(write_html=False)
    assert result.stats["all_sources_failed"] is True
    storage.close()


def _explode() -> int:
    raise sqlite3.OperationalError("database is locked")


def test_stage_failure_survives_a_broken_pending_count(tmp_path: Path):
    """Счётчик для статистики не имеет права утопить обработчик ошибки.

    Иначе сбой самой базы снова оставляет строку прогона в статусе running —
    ровно тот симптом, который закрывал B2.
    """
    cfg = make_config(tmp_path)
    cfg.llm.stages["verify"] = StageConfig(
        backend="его-нет", model="opus", effort="high", threshold=70
    )
    storage = Storage(cfg.db_path)
    storage.pending_count = _explode
    pipeline = Pipeline(
        cfg, storage, {"reddit": FakeSource([post("a1")])},
        {"fake": ScriptedBackend()}, now_factory=lambda: NOW,
    )
    result = pipeline.run(write_html=False)

    assert result.status == "error"
    row = storage.conn.execute(
        "SELECT status FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row["status"] == "error"
    storage.close()


def test_quota_pause_survives_a_broken_pending_count(tmp_path: Path):
    cfg, storage, _s, backend, pipeline = build(tmp_path, [post("a1")])
    backend.quota_on = "sonnet"
    storage.pending_count = _explode
    result = pipeline.run(write_html=False)

    assert result.status == "paused"
    row = storage.conn.execute(
        "SELECT status FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row["status"] == "paused"
    storage.close()
