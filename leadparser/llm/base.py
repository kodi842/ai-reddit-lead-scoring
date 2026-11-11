from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Вызов модели не дал валидного результата."""


class QuotaExhausted(LLMError):
    """Исчерпан лимит подписки или тарифа — продолжать прогон бессмысленно."""


class LLMBackend(Protocol):
    name: str

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, effort: str
    ) -> dict:
        ...


def call_with_retries(
    backend: LLMBackend,
    system: str,
    user: str,
    schema: dict,
    model: str,
    effort: str,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    last: LLMError | None = None
    for attempt in range(retries + 1):
        try:
            return backend.complete_json(system, user, schema, model, effort)
        except QuotaExhausted:
            raise
        except LLMError as exc:
            last = exc
            log.warning("попытка %s/%s не удалась: %s", attempt + 1, retries + 1, exc)
            if attempt < retries:
                sleep(2 * (attempt + 1))
    assert last is not None
    raise last
