from __future__ import annotations

import json
import logging

from ..config import StageConfig
from ..llm.base import LLMBackend, QuotaExhausted, call_with_retries
from ..models import ANALYSIS_SCHEMA, Post, ThreadAnalysis
from ..prompts import load_prompt
from ..storage import Storage
from .base import Progress, StageCounters, execute_units
from .scorer import render_thread

log = logging.getLogger(__name__)

STAGE = "verify"
PROMPT_VERSION = "verifier_v1"


def hot_threads(storage: Storage, threshold: int) -> list[Post]:
    """Горячие треды без успешной верификации — по всем прогонам.

    Ограничиться текущим прогоном нельзя: тред, чья верификация упала по лимиту
    модели, остался бы непроверенным навсегда.
    """
    rows = storage.threads_for_report(min_score=threshold)
    out: list[Post] = []
    for post, _analysis, _verified in rows:
        existing = storage.get_stage_result(post.thread_id, STAGE, PROMPT_VERSION)
        if existing is None or existing["status"] != "ok":
            out.append(post)
    return out


def run_verifier(
    storage: Storage,
    backend: LLMBackend,
    stage_cfg: StageConfig,
    run_id: int,
    concurrency: int = 1,
    on_progress: Progress | None = None,
) -> StageCounters:
    counters = StageCounters()
    system = load_prompt(PROMPT_VERSION)
    posts = hot_threads(storage, stage_cfg.threshold)
    if not posts:
        return counters

    # Storage открывает sqlite3 с check_same_thread=True (по умолчанию), а
    # execute_units при concurrency > 1 запускает worker в потоках пула — любое
    # обращение к storage оттуда валит sqlite3.ProgrammingError. Параллелизм
    # нужен только ради задержки самой модели, поэтому все чтения из storage
    # (сам тред и предыдущий анализ) делаем здесь заранее, в основном потоке;
    # worker получает уже готовый текст промпта и трогает только backend.
    units: list[tuple[Post, str]] = []
    for post in posts:
        previous = storage.get_analysis(post.thread_id)
        thread_text = render_thread(post, storage.get_messages(post.thread_id))
        previous_json = json.dumps(
            previous.model_dump() if previous else {}, ensure_ascii=False, indent=2
        )
        user = f"{thread_text}\n\n--- previous analysis ---\n{previous_json}"
        units.append((post, user))

    def worker(unit: tuple[Post, str]) -> ThreadAnalysis:
        _, user = unit
        raw = call_with_retries(
            backend, system, user, ANALYSIS_SCHEMA, stage_cfg.model, stage_cfg.effort
        )
        return ThreadAnalysis.model_validate(raw)

    quota_hit: QuotaExhausted | None = None
    done: set[str] = set()
    finished = 0
    for unit, analysis, exc in execute_units(
        units, worker, concurrency, lambda: quota_hit is not None
    ):
        post, _ = unit
        if isinstance(exc, QuotaExhausted):
            quota_hit = exc
        elif exc is not None:
            log.warning("верификация %s не удалась: %s", post.thread_id, exc)
            storage.save_stage_result(
                post.thread_id, STAGE, PROMPT_VERSION, "error", error=str(exc)[:500]
            )
            counters.error += 1
            done.add(post.thread_id)
        else:
            assert analysis is not None
            storage.save_analysis(post.thread_id, PROMPT_VERSION, True, analysis, run_id)
            storage.save_stage_result(
                post.thread_id, STAGE, PROMPT_VERSION, "ok",
                payload={"max_score": analysis.max_score, "leads": len(analysis.leads)},
            )
            counters.ok += 1
            done.add(post.thread_id)
        finished += 1
        if on_progress is not None:
            on_progress(finished, len(units))

    if quota_hit is not None:
        for post in posts:
            if post.thread_id not in done:
                storage.save_stage_result(
                    post.thread_id, STAGE, PROMPT_VERSION, "quota", error="лимит модели"
                )
                counters.quota += 1
        raise quota_hit
    return counters
