from __future__ import annotations

import logging

from ..config import StageConfig
from ..llm.base import LLMBackend, QuotaExhausted, call_with_retries
from ..models import ANALYSIS_SCHEMA, Message, Post, ThreadAnalysis
from ..prompts import load_prompt
from ..storage import Storage
from .base import Progress, StageCounters, execute_units

log = logging.getLogger(__name__)

STAGE = "score"
PROMPT_VERSION = "scorer_v1"
MAX_TOTAL_CHARS = 12000
MAX_MESSAGE_CHARS = 1200
SEPARATOR = "\n--- thread ---\n"
TRUNCATION_MARKER = "… (остальные комментарии отброшены по длине)"
JOIN_LEN = 2  # длина "\n\n", которым "\n\n".join(...) склеивает строки треда


def _post_as_message(post: Post) -> Message:
    """Сам пост в виде сообщения — ровно так его отдаёт и parse_rss_thread."""
    return Message(
        thread_id=post.thread_id, msg_id=post.thread_id, parent_id=None,
        author=post.author,
        body="\n\n".join(x for x in (post.title, post.body.strip()) if x),
        created_at=post.created_at, depth=0, score=0,
    )


def render_thread(post: Post, messages: list[Message]) -> str:
    header = (
        f"source: {post.source_key.replace('reddit:', 'r/')}\n"
        f"post title: {post.title}\n"
        f"original poster (OP): u/{post.author}\n"
    )
    # Без комментариев тред не бывает пустым: в нём как минимум сам пост.
    # Раньше промпт обрывался на заголовке, и модель оценивала тред вслепую.
    messages = messages or [_post_as_message(post)]
    lines: list[str] = []
    # Бюджет должен считать ВСЁ, что попадёт в итоговую строку — не только сами
    # строки, но и SEPARATOR перед ними, "\n\n" между ними и TRUNCATION_MARKER,
    # которым мы обрываем список. Раньше "used" не знал про эти накладные
    # расходы, и на треде с множеством коротких сообщений итог заметно превышал
    # MAX_TOTAL_CHARS. Резервируем JOIN_LEN + len(TRUNCATION_MARKER) на каждом
    # шаге — тогда добавление маркера при обрыве никогда не пробьёт лимит.
    used = len(header) + len(SEPARATOR)
    for msg in messages:
        body = msg.body.strip()[:MAX_MESSAGE_CHARS]
        op_marker = " (OP)" if msg.author == post.author else ""
        line = f"[{msg.created_at:%Y-%m-%d %H:%M}] u/{msg.author}{op_marker}: {body}"
        cost = len(line) + (JOIN_LEN if lines else 0)
        if used + cost + JOIN_LEN + len(TRUNCATION_MARKER) > MAX_TOTAL_CHARS:
            lines.append(TRUNCATION_MARKER)
            break
        lines.append(line)
        used += cost
    return header + SEPARATOR + "\n\n".join(lines)


def run_scorer(
    storage: Storage,
    backend: LLMBackend,
    stage_cfg: StageConfig,
    posts: list[Post],
    run_id: int,
    concurrency: int = 1,
    on_progress: Progress | None = None,
) -> StageCounters:
    counters = StageCounters()
    system = load_prompt(PROMPT_VERSION)

    # Storage открывает sqlite3 с check_same_thread=True (по умолчанию), а
    # execute_units при concurrency > 1 запускает worker в потоках пула — любое
    # обращение к storage оттуда валит sqlite3.ProgrammingError. Параллелизм
    # нужен только ради задержки самой модели, поэтому все чтения из storage
    # делаем здесь заранее, в основном потоке; worker получает уже готовый
    # текст треда и трогает только backend.
    units = [
        (post, render_thread(post, storage.get_messages(post.thread_id)))
        for post in posts
    ]

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
            log.warning("скоринг %s не удался: %s", post.thread_id, exc)
            storage.save_stage_result(
                post.thread_id, STAGE, PROMPT_VERSION, "error", error=str(exc)[:500]
            )
            counters.error += 1
            done.add(post.thread_id)
        else:
            assert analysis is not None
            storage.save_analysis(post.thread_id, PROMPT_VERSION, False, analysis, run_id)
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
