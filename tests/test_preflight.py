# Находка 4: предполётная проверка лицензии BlankTrail — диагностика, а не
# условие запуска. Печатает план, предупреждает про ограниченные домены и про
# js_solver без выделенных процессов; сама неудача запроса — тоже предупреждение.
from __future__ import annotations

import io

from rich.console import Console

from leadparser.net.blanktrail import BlankTrailError
from leadparser.preflight import check_license


class StubBlankTrail:
    def __init__(self, status: dict | None = None, error: Exception | None = None) -> None:
        self._status = status
        self._error = error

    def license_status(self) -> dict:
        if self._error is not None:
            raise self._error
        assert self._status is not None
        return self._status


def run_check(blanktrail, profile: dict) -> str:
    buf = io.StringIO()
    console = Console(file=buf, no_color=True)
    check_license(blanktrail, profile, console)
    return buf.getvalue()


def test_prints_plan_line_without_warnings_when_everything_is_fine():
    status = {"plan": "enterprise", "allowed_domains": [], "js_solver_max_procs": 4}
    output = run_check(StubBlankTrail(status=status), {"js_solver": True})
    assert "enterprise" in output
    assert "reddit.com" not in output
    assert "Challenge Breaker" not in output


def test_warns_when_reddit_is_not_in_allowed_domains():
    status = {"plan": "starter", "allowed_domains": ["example.com"], "js_solver_max_procs": 0}
    output = run_check(StubBlankTrail(status=status), {})
    assert "reddit.com" in output
    assert "403" in output


def test_no_warning_when_reddit_domain_is_allowed():
    status = {"plan": "starter", "allowed_domains": ["reddit.com"], "js_solver_max_procs": 0}
    output = run_check(StubBlankTrail(status=status), {})
    assert "не входит" not in output


def test_no_warning_when_allowed_domains_is_empty():
    """Пустой список — «без ограничений», а не «ничего не разрешено»."""
    status = {"plan": "enterprise", "allowed_domains": [], "js_solver_max_procs": 0}
    output = run_check(StubBlankTrail(status=status), {"js_solver": False})
    assert "не входит" not in output


def test_warns_when_js_solver_enabled_but_no_procs_available():
    status = {"plan": "starter", "allowed_domains": [], "js_solver_max_procs": 0}
    output = run_check(StubBlankTrail(status=status), {"js_solver": True})
    assert "Challenge Breaker" in output


def test_no_warning_when_js_solver_disabled_in_profile():
    status = {"plan": "starter", "allowed_domains": [], "js_solver_max_procs": 0}
    output = run_check(StubBlankTrail(status=status), {"js_solver": False})
    assert "Challenge Breaker" not in output


def test_request_failure_is_only_a_warning_and_does_not_raise():
    output = run_check(
        StubBlankTrail(error=BlankTrailError("BlankTrail недоступен")), {"js_solver": True}
    )
    assert "BlankTrail недоступен" in output
