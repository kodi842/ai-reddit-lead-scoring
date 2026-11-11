from __future__ import annotations

import logging

from ..config import StageConfig
from ..llm.base import LLMBackend, QuotaExhausted, call_with_retries
from ..models import PREFILTER_SCHEMA, Post, PrefilterBatch
from ..prompts import load_prompt
from ..storage import Storage
from .base import Progress, StageCounters, execute_units

log = logging.getLogger(__name__)

STAGE = "prefilter"
PROMPT_VERSION = "prefilter_v1"
BODY_CHARS = 1500


def build_batches(posts: list[Post], size: int) -> list[list[Post]]:
    step = max(1, size)
    return [posts[i : i + step] for i in range(0, len(posts), step)]


def render_batch(batch: list[Post]) -> str:
    blocks = []
    for post in batch:
        body = post.body.strip()[:BODY_CHARS]
        blocks.append(
            f"### id: {post.thread_id}\n"
            f"source: {post.source_key.replace('reddit:', 'r/')}\n"
            f"title: {post.title}\n"
            f"body: {body or '(no body)'}"
        )
    return "\n\n".join(blocks)


def _save_verdicts(
    storage: Storage, batch: list[Post], result: PrefilterBatch,
    counters: StageCounters, done: set[str],
) -> None:
    verdicts = {item.id: item for item in result.items}
    for post in batch:
        # Модель иногда возвращает голый id вместо «reddit:<id>». Раньше на этом
        # весь батч из 25 постов уходил в error и ретраился каждым прогоном
        # вечно — сигналом был только растущий счётчик «ждут моего разбора».
        verdict = verdicts.get(post.thread_id) or verdicts.get(
            post.thread_id.split(":", 1)[-1]
        )
        done.add(post.thread_id)
        if verdict is None:
            storage.save_stage_result(
                post.thread_id, STAGE, PROMPT_VERSION, "error",
                error="модель не вернула вердикт для этого id",
            )
            counters.error += 1
            continue
        storage.save_stage_result(
            post.thread_id, STAGE, PROMPT_VERSION, "ok",
            payload={"candidate": verdict.candidate, "reason": verdict.reason},
        )
        counters.ok += 1


def run_prefilter(
    storage: Storage,
    backend: LLMBackend,
    stage_cfg: StageConfig,
    posts: list[Post],
    concurrency: int = 1,
    on_progress: Progress | None = None,
) -> StageCounters:
    counters = StageCounters()
    system = load_prompt(PROMPT_VERSION)

    # Предфильтр — самая многовызовная ступень: окно в 7 дней по 37 сабреддитам
    # это сотни батчей примерно по 14 с каждый, последовательно — часы. Схема
    # параллелизма ровно та же, что в run_scorer: storage читаем и пишем только
    # в основном потоке, worker получает готовый текст батча и трогает backend.
    units = [
        (batch, render_batch(batch))
        for batch in build_batches(posts, stage_cfg.batch_size)
    ]

    def worker(unit: tuple[list[Post], str]) -> PrefilterBatch:
        _, user = unit
        raw = call_with_retries(
            backend, system, user, PREFILTER_SCHEMA, stage_cfg.model, stage_cfg.effort
        )
        return PrefilterBatch.model_validate(raw)

    quota_hit: QuotaExhausted | None = None
    done: set[str] = set()
    finished = 0
    for unit, result, exc in execute_units(
        units, worker, concurrency, lambda: quota_hit is not None
    ):
        batch, _ = unit
        if isinstance(exc, QuotaExhausted):
            quota_hit = exc
        elif exc is not None:
            log.warning("предфильтр: батч из %s постов не удался: %s", len(batch), exc)
            for post in batch:
                storage.save_stage_result(
                    post.thread_id, STAGE, PROMPT_VERSION, "error", error=str(exc)[:500]
                )
                counters.error += 1
                done.add(post.thread_id)
        else:
            assert result is not None
            _save_verdicts(storage, batch, result, counters, done)
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
