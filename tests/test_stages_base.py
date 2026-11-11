from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from leadparser.stages import base
from leadparser.stages.base import StageCounters, execute_units


def test_execute_units_sequential_preserves_order_and_surfaces_exception():
    def worker(unit: int) -> int:
        if unit == 2:
            raise ValueError("boom")
        return unit * 10

    results = list(execute_units([1, 2, 3], worker, concurrency=1))

    assert [r[0] for r in results] == [1, 2, 3]
    assert results[0] == (1, 10, None)
    assert results[2] == (3, 30, None)
    unit, result, exc = results[1]
    assert unit == 2 and result is None
    assert isinstance(exc, ValueError) and str(exc) == "boom"


def test_execute_units_threaded_pairs_each_unit_with_its_own_result():
    def worker(unit: int) -> int:
        if unit == 2:
            raise ValueError(f"boom-{unit}")
        return unit * 100

    results = list(execute_units([1, 2, 3], worker, concurrency=3))

    by_unit = {r[0]: r for r in results}
    assert set(by_unit) == {1, 2, 3}
    assert by_unit[1] == (1, 100, None)
    assert by_unit[3] == (3, 300, None)
    unit, result, exc = by_unit[2]
    assert result is None
    assert isinstance(exc, ValueError) and str(exc) == "boom-2"


def test_execute_units_threaded_yields_fast_units_before_slow_one():
    """Pins the as_completed bug: the first-submitted unit is also the slowest.

    A buggy implementation that iterates the futures dict in submission order
    blocks on that slow unit's `.result()` first, so it would come back before
    the fast units even though they finish first — exactly the property later
    stages rely on to fail fast on QuotaExhausted from whichever unit hits it,
    not just the first one submitted. Uses a short sleep and checks arrival
    order (not elapsed wall-clock time), so it doesn't flake on a loaded box.
    """
    def worker(unit: int) -> int:
        if unit == 1:
            time.sleep(0.05)
        return unit

    results = list(execute_units([1, 2, 3], worker, concurrency=3))
    yielded_order = [r[0] for r in results]

    assert yielded_order.index(1) > yielded_order.index(2)
    assert yielded_order.index(1) > yielded_order.index(3)


def test_stage_counters_as_dict_merges_extra():
    empty = StageCounters()
    assert empty.as_dict() == {"ok": 0, "error": 0, "quota": 0}

    counters = StageCounters(ok=3, error=1, quota=2, extra={"skipped": 5, "retried": 1})
    assert counters.as_dict() == {
        "ok": 3, "error": 1, "quota": 2, "skipped": 5, "retried": 1,
    }


def test_execute_units_submits_in_a_bounded_window(monkeypatch):
    """C2: не ставить в очередь 400 вызовов `claude -p` разом.

    ThreadPoolExecutor.__exit__ ждёт очередь и не отменяет её, поэтому Ctrl-C
    (и упор в лимит на пятой единице из четырёхсот) раньше означал «дождись,
    пока отработают все остальные». Считаем именно submit'ы: воркеров всё
    равно работает не больше concurrency, а проблема была в размере очереди.
    """
    submitted: list[object] = []

    class CountingPool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            submitted.append(args[0] if args else None)
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(base, "ThreadPoolExecutor", CountingPool)

    def worker(unit: int) -> int:
        time.sleep(0.01)
        return unit

    gen = base.execute_units(list(range(60)), worker, concurrency=2)
    try:
        next(gen)
    finally:
        gen.close()
    assert len(submitted) <= 8, f"в очередь ушло {len(submitted)} единиц из 60"


def test_execute_units_stops_submitting_once_stop_condition_is_set():
    """C2: упёрлись в лимит — новые единицы в очередь не уходят."""
    seen: list[int] = []
    lock = threading.Lock()
    stop = {"hit": False}

    def worker(unit: int) -> int:
        with lock:
            seen.append(unit)
        return unit

    consumed = []
    for unit, _res, _exc in base.execute_units(
        list(range(40)), worker, concurrency=3, should_stop=lambda: stop["hit"]
    ):
        consumed.append(unit)
        stop["hit"] = True

    assert len(seen) <= 8, f"после стопа отработало {len(seen)} единиц"
    assert len(consumed) == len(seen), "уже оплаченные результаты нельзя терять"


def test_execute_units_sequential_honours_stop_condition():
    seen: list[int] = []
    stop = {"hit": False}

    def worker(unit: int) -> int:
        seen.append(unit)
        return unit

    for _unit, _res, _exc in base.execute_units(
        list(range(10)), worker, concurrency=1, should_stop=lambda: stop["hit"]
    ):
        stop["hit"] = True

    assert seen == [0]
