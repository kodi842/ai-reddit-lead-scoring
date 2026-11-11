# tests/test_llm_claude_code.py
import json
import subprocess

import pytest

from leadparser.llm.base import LLMError, QuotaExhausted, call_with_retries
from leadparser.llm.claude_code import ClaudeCodeBackend, parse_cli_output

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def cli_json(**overrides) -> str:
    payload = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": '{"ok":true}', "structured_output": {"ok": True},
        "api_error_status": None, "total_cost_usd": 0.0075,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_cli_output_returns_structured_output():
    assert parse_cli_output(cli_json()) == {"ok": True}


def test_parse_cli_output_detects_quota_from_status():
    with pytest.raises(QuotaExhausted):
        parse_cli_output(cli_json(is_error=True, api_error_status=429, result="rate limited"))


def test_parse_cli_output_detects_quota_from_text():
    with pytest.raises(QuotaExhausted):
        parse_cli_output(cli_json(
            is_error=True, subtype="error",
            result="Claude usage limit reached. Your limit will reset at 3pm.",
            structured_output=None,
        ))


def test_parse_cli_output_raises_on_error_flag():
    with pytest.raises(LLMError, match="claude -p"):
        parse_cli_output(cli_json(is_error=True, result="tool failure", structured_output=None))


def test_parse_cli_output_raises_on_missing_structured_output():
    with pytest.raises(LLMError, match="structured_output"):
        parse_cli_output(cli_json(structured_output=None))


def test_parse_cli_output_raises_on_garbage():
    with pytest.raises(LLMError, match="не JSON"):
        parse_cli_output("claude: command not found")


def test_backend_builds_expected_command():
    captured = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(cmd, 0, stdout=cli_json(), stderr="")

    backend = ClaudeCodeBackend(runner=runner)
    assert backend.complete_json("SYS", "USER", SCHEMA, "haiku", "low") == {"ok": True}
    cmd = captured["cmd"]
    assert cmd[0:2] == ["claude", "-p"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "haiku"
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[-1] == "USER"
    assert "--bare" not in cmd
    assert "CLAUDECODE" not in captured["env"]
    assert "CLAUDE_CODE_ENTRYPOINT" not in captured["env"]
    assert captured["timeout"] == 300.0


def test_backend_raises_on_timeout():
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 300)

    with pytest.raises(LLMError, match="таймаут"):
        ClaudeCodeBackend(runner=runner).complete_json("s", "u", SCHEMA, "haiku", "low")


def test_backend_raises_when_binary_missing():
    def runner(cmd, **kwargs):
        raise FileNotFoundError("claude")

    with pytest.raises(LLMError, match="claude"):
        ClaudeCodeBackend(runner=runner).complete_json("s", "u", SCHEMA, "haiku", "low")


def test_backend_includes_stderr_on_nonzero_exit():
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(LLMError, match="boom"):
        ClaudeCodeBackend(runner=runner).complete_json("s", "u", SCHEMA, "haiku", "low")


def test_backend_raises_quota_exhausted_on_nonzero_exit_with_quota_stderr():
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="",
            stderr="Claude usage limit reached. Your limit will reset at 3pm.",
        )

    with pytest.raises(QuotaExhausted):
        ClaudeCodeBackend(runner=runner).complete_json("s", "u", SCHEMA, "haiku", "low")


def test_backend_raises_llm_error_not_quota_on_unrelated_nonzero_exit():
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown flag --nonsense")

    with pytest.raises(LLMError) as exc_info:
        ClaudeCodeBackend(runner=runner).complete_json("s", "u", SCHEMA, "haiku", "low")
    assert not isinstance(exc_info.value, QuotaExhausted)


def test_call_with_retries_recovers_after_llm_error():
    attempts = {"n": 0}

    class Flaky:
        def complete_json(self, system, user, schema, model, effort):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise LLMError("bad json")
            return {"ok": True}

    result = call_with_retries(
        Flaky(), "s", "u", SCHEMA, "haiku", "low", retries=2, sleep=lambda s: None
    )
    assert result == {"ok": True}
    assert attempts["n"] == 3


def test_call_with_retries_gives_up_after_budget():
    class Broken:
        def complete_json(self, *a, **kw):
            raise LLMError("bad json")

    with pytest.raises(LLMError):
        call_with_retries(
            Broken(), "s", "u", SCHEMA, "haiku", "low", retries=2, sleep=lambda s: None
        )


def test_call_with_retries_does_not_retry_quota():
    attempts = {"n": 0}

    class OutOfQuota:
        def complete_json(self, *a, **kw):
            attempts["n"] += 1
            raise QuotaExhausted("limit")

    with pytest.raises(QuotaExhausted):
        call_with_retries(
            OutOfQuota(), "s", "u", SCHEMA, "haiku", "low", retries=2, sleep=lambda s: None
        )
    assert attempts["n"] == 1
