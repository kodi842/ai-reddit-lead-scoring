from pathlib import Path

import pytest

from leadparser.llm import base as llm_base


@pytest.fixture(scope="session")
def config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture(autouse=True)
def disable_retry_backoff_sleep(monkeypatch):
    """call_with_retries' backoff must not cost wall-clock time in tests.

    call_with_retries binds its `sleep` parameter to `time.sleep` as a default
    argument value, and Python evaluates default argument values exactly once, at
    function-definition time. So patching `time.sleep` itself after the module is
    already imported has no effect — the function already holds a direct reference
    to the real sleep. We replace the bound default itself instead. This only
    touches call_with_retries: ThrottledClient (leadparser.net.blanktrail) takes its
    own `sleep` as an explicit constructor argument in every caller, so it is
    unaffected.
    """
    retries_default = llm_base.call_with_retries.__defaults__[0]
    monkeypatch.setattr(
        llm_base.call_with_retries,
        "__defaults__",
        (retries_default, lambda _seconds: None),
    )


@pytest.fixture(autouse=True)
def isolate_blanktrail_ca_cert_env(request, monkeypatch):
    """`load_config`/`load_dotenv()` reads this machine's real `.env`, which may
    set `BLANKTRAIL_CA_CERT` to a real path (needed for `run`/the one integration
    test that talks to BlankTrail for real). `os.environ` is process-global and
    `load_dotenv()` mutates it with no per-test cleanup, so the first test that
    calls `load_config` leaks that value into every test that runs after it —
    tests would then pass or fail depending on what happens to be on the
    developer's machine, not on the code. An empty string (not a deletion) is
    required: `load_dotenv()`'s default `override=False` only skips a key that
    is already *present* in `os.environ`, so unsetting it here would just let
    the next `load_dotenv()` call refill it from the real file. Tests that need
    a real value set it themselves via `monkeypatch.setenv`, which layers over
    this default within that one test.

    Integration-marked tests are exempt: `pytest -m 'not integration'` (our
    default run) never collects them, so deselection alone would make this
    moot — but `pytest -m integration` does collect and run them, and this
    fixture is autouse, so it would still fire for them. Autouse fixtures
    aren't gated by markers. test_reddit_listing_through_blanktrail needs this
    machine's real BLANKTRAIL_CA_CERT to build a `verify=` that BlankTrail's
    own MITM certificate actually satisfies; blanking it here would silently
    fall back to `verify=True` and fail TLS verification against BlankTrail
    before the first request — a spurious failure in the one test that proves
    the whole Reddit-over-RSS-through-BlankTrail path this project exists to
    deliver actually works. Before the path moved from config.yaml into the
    environment, it was hardcoded and this test was immune; the isolation has
    to respect that boundary now that the value lives here too.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    monkeypatch.setenv("BLANKTRAIL_CA_CERT", "")
