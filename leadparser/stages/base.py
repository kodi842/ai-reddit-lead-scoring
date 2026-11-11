from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# Колбэк прогресса ступени: (сделано, всего). None вместо total — «пока не знаю».
Progress = Callable[[int, int | None], None]

_NO_MORE = object()


@dataclass
class StageCounters:
    ok: int = 0
    error: int = 0
    quota: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        return {"ok": self.ok, "error": self.error, "quota": self.quota, **self.extra}


def execute_units(
    units: list[Any],
    worker: Callable[[Any], Any],
    concurrency: int,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[tuple[Any, Any | None, Exception | None]]:
    """Запускает worker по единицам работы, отдавая результаты по мере готовности.

    Исключения не пробрасываются, а возвращаются третьим элементом кортежа —
    решение о том, прервать прогон или продолжить, принимает вызывающая ступень.

    В очередь пула уходит не больше ~`concurrency * 2` единиц, окно пополняется
    по мере готовности. Раньше сюда сразу отправлялись все единицы, а
    `ThreadPoolExecutor.__exit__` ждёт очередь, не отменяя её: Ctrl-C на скоринге
    висел, пока не отработают все оставшиеся вызовы `claude -p`, а упор в лимит
    на пятой единице из четырёхсот всё равно порождал ещё 395 процессов.
    `should_stop` (например «уже поймали QuotaExhausted») останавливает подачу
    новых единиц; те, что уже запущены, дорабатывают и отдают результат —
    терять оплаченную работу нельзя.
    """
    stop = should_stop or (lambda: False)
    if concurrency <= 1:
        for unit in units:
            if stop():
                return
            try:
                yield unit, worker(unit), None
            except Exception as exc:  # noqa: BLE001 — решает вызывающий код
                yield unit, None, exc
        return

    window = max(2, concurrency * 2)
    queue = iter(units)
    pending: dict[Future, Any] = {}
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        def top_up() -> None:
            while len(pending) < window and not stop():
                unit = next(queue, _NO_MORE)
                if unit is _NO_MORE:
                    return
                pending[pool.submit(worker, unit)] = unit

        top_up()
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in done:
                unit = pending.pop(future)
                try:
                    yield unit, future.result(), None
                except Exception as exc:  # noqa: BLE001
                    yield unit, None, exc
            top_up()
    finally:
        # wait=False: прерывание не должно ждать уже запущенные вызовы модели.
        pool.shutdown(wait=False, cancel_futures=True)
