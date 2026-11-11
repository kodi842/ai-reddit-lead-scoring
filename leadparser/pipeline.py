from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .llm.base import LLMBackend, LLMError, QuotaExhausted
from .models import Post
from .report.render import render_report, write_report
from .sources.base import Source
from .stages import prefilter, scorer, verifier
from .stages.heuristic import apply_heuristic
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    run_id: int
    status: str
    stats: dict[str, Any] = field(default_factory=dict)
    report_path: Path | None = None


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        storage: Storage,
        sources: dict[str, Source],
        backends: dict[str, LLMBackend],
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self.sources = sources
        self.backends = backends
        self._now = now_factory
        # Прогресс — необязательный колбэк (ступень, сделано, всего): пайплайн
        # не знает про rich и CLI, а тесты по умолчанию молчат.
        self._progress = progress

    def _tick(self, stage: str, done: int, total: int | None) -> None:
        if self._progress is not None:
            self._progress(stage, done, total)

    def _stage_progress(self, stage: str) -> Callable[[int, int | None], None] | None:
        if self._progress is None:
            return None
        return lambda done, total: self._tick(stage, done, total)

    def _backend_for(self, stage: str) -> LLMBackend:
        name = self.cfg.llm.stage(stage).backend
        if name not in self.backends:
            raise LLMError(f"бэкенд {name!r} не создан — проверьте llm.backends")
        return self.backends[name]

    def _source_for(self, thread_id: str) -> Source | None:
        return self.sources.get(thread_id.split(":", 1)[0])

    def _collect(self, since: datetime, max_posts: int | None) -> list[Post]:
        collected: list[Post] = []
        for source in self.sources.values():
            for post in source.list_new_posts(since):
                self.storage.upsert_post(post)
                collected.append(post)
                self._tick("collect", len(collected), max_posts)
                if max_posts is not None and len(collected) >= max_posts:
                    return collected
        return collected

    def _collection_health(self, collected: int) -> dict[str, Any]:
        """Сколько целей сбора настроено и сколько из них сорвалось.

        Нужно, чтобы отличить «сеть/прокси легли, собрать не удалось ничего»
        от честного «всё сработало, нового нет»: первое — повод для кода
        возврата 1, второе — обычный успешный прогон.

        Сорвавшиеся цели считаем и показываем всегда, но «упало всё» требует
        ещё и пустого сбора: сабреддит отдаёт до десяти страниц, и 429 на
        второй — это сбой при уже собранных постах, а не «собирать было
        нечего». Непустой сбор сам себе доказательство обратного.
        """
        targets = 0
        failures: list[str] = []
        for source in self.sources.values():
            count = getattr(source, "collection_targets", None)
            names = getattr(source, "collection_failures", None)
            targets += int(count()) if callable(count) else 0
            failures.extend(names() if callable(names) else [])
        return {
            "source_targets": targets,
            "source_failures": len(failures),
            "failed_sources": failures,
            "all_sources_failed": (
                bool(targets) and len(failures) >= targets and collected == 0
            ),
        }

    def _safe_pending_count(self) -> int | None:
        """Счётчик для статистики не имеет права утопить обработчик ошибки.

        Если в except-ветку прилетел сбой самой базы, то и этот запрос упадёт —
        и прогон снова останется в статусе running без отчёта.
        """
        try:
            return self.storage.pending_count()
        except Exception:  # noqa: BLE001 — статистика не стоит потери прогона
            log.warning("не смог прочитать счётчик ожидающих тредов", exc_info=True)
            return None

    def _fetch_comments(self, posts: list[Post]) -> tuple[int, list[Post]]:
        """Догружает комментарии и возвращает (сколько тредов, кого скорить).

        Тред, чьи комментарии не загрузились, скорить нельзя: модель получила
        бы один заголовок, а пустой результат осел бы в кэше как `ok` навсегда.
        Помечаем его `error` по ступени score — `posts_without_stage` вернёт
        его следующему прогону, а KPI «ждут моего разбора» покажет его сейчас.
        """
        fetched = 0
        scorable: list[Post] = []
        for index, post in enumerate(posts, start=1):
            self._tick("comments", index, len(posts))
            source = self._source_for(post.thread_id)
            if self.storage.get_messages(post.thread_id) or source is None:
                scorable.append(post)
                continue
            try:
                messages = source.fetch_thread(post.thread_id)
            except Exception as exc:  # noqa: BLE001 — один тред не ломает прогон
                log.warning("комментарии %s не загрузились: %s", post.thread_id, exc)
                self.storage.save_stage_result(
                    post.thread_id, scorer.STAGE, scorer.PROMPT_VERSION, "error",
                    error=f"комментарии не загрузились: {str(exc)[:400]}",
                )
                continue
            if messages:
                self.storage.save_messages(post.thread_id, messages)
                fetched += 1
            scorable.append(post)
        return fetched, scorable

    def run(self, max_posts: int | None = None, write_html: bool = True) -> RunResult:
        now = self._now()
        run_id = self.storage.start_run()
        since = now - timedelta(days=self.cfg.lookback_days)
        stats: dict[str, Any] = {}
        status = "ok"
        current_stage = "collect"

        try:
            # Сбор тоже внутри try: «между start_run и finish_run» — это и он.
            collected = self._collect(since, max_posts)
            stats["collected"] = len(collected)
            stats.update(self._collection_health(len(collected)))
            stats["skipped"] = apply_heuristic(
                self.storage, self.cfg.heuristic, collected
            )

            current_stage = "prefilter"
            pending = self.storage.posts_without_stage(
                prefilter.STAGE, prefilter.PROMPT_VERSION
            )
            stats["prefilter"] = prefilter.run_prefilter(
                self.storage, self._backend_for("prefilter"),
                self.cfg.llm.stage("prefilter"), pending, self.cfg.llm.concurrency,
                self._stage_progress("prefilter"),
            ).as_dict()

            candidates = self.storage.posts_without_stage(
                scorer.STAGE, scorer.PROMPT_VERSION, only_candidates=True,
                candidate_prompt_version=prefilter.PROMPT_VERSION,
            )
            stats["comments_fetched"], scorable = self._fetch_comments(candidates)
            current_stage = "score"
            stats["score"] = scorer.run_scorer(
                self.storage, self._backend_for("score"), self.cfg.llm.stage("score"),
                scorable, run_id, self.cfg.llm.concurrency,
                self._stage_progress("score"),
            ).as_dict()

            current_stage = "verify"
            stats["verify"] = verifier.run_verifier(
                self.storage, self._backend_for("verify"),
                self.cfg.llm.stage("verify"), run_id, self.cfg.llm.concurrency,
                self._stage_progress("verify"),
            ).as_dict()
        except QuotaExhausted as exc:
            log.warning("прогон приостановлен: %s", exc)
            status = "paused"
            stats["quota_message"] = str(exc)[:300]
            stats["paused_at"] = current_stage
            stats["pending"] = self._safe_pending_count()
        except Exception as exc:  # noqa: BLE001 — прогон обязан закрыться сам
            # Иначе строка прогона навсегда осталась бы в статусе running, а
            # отчёт по уже оплаченной работе не отрендерился бы вовсе.
            log.exception("ступень %s сорвалась", current_stage)
            status = "error"
            stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
            stats["failed_at"] = current_stage
            stats["pending"] = self._safe_pending_count()

        report_path: Path | None = None
        if write_html:
            rows = self.storage.threads_for_report(self.cfg.report.min_score, since)
            html = render_report(
                rows, self.storage, self.cfg.report, now, self.storage.pending_count()
            )
            report_path = write_report(html, self.cfg.reports_dir, now)

        self.storage.finish_run(run_id, status, stats)
        return RunResult(run_id=run_id, status=status, stats=stats, report_path=report_path)
