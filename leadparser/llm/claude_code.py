from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

from .base import LLMError, QuotaExhausted

QUOTA_MARKERS = ("usage limit", "rate limit", "quota", "limit reached")
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def _looks_like_quota(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in QUOTA_MARKERS)


def parse_cli_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"вывод claude -p не JSON: {stdout[:300]!r}") from exc

    text = " ".join(
        str(payload.get(key) or "") for key in ("result", "subtype", "terminal_reason")
    )
    if payload.get("api_error_status") == 429 or (
        payload.get("is_error") and _looks_like_quota(text)
    ):
        raise QuotaExhausted(f"лимит подписки Claude: {text[:200]}")
    if payload.get("is_error"):
        raise LLMError(f"claude -p вернул ошибку: {text[:300]}")
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        raise LLMError(f"в ответе нет structured_output: {text[:300]}")
    return structured


class ClaudeCodeBackend:
    """LLM через headless Claude Code — расходует лимит подписки, не платные токены."""

    name = "claude_code"

    def __init__(
        self,
        binary: str = "claude",
        timeout_s: float = 300.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self._runner = runner

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, effort: str
    ) -> dict[str, Any]:
        cmd = [
            self.binary, "-p",
            "--model", model,
            "--effort", effort,
            "--tools", "",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--system-prompt", system,
            user,
        ]
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        try:
            proc = self._runner(
                cmd, capture_output=True, text=True, timeout=self.timeout_s, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"таймаут {self.timeout_s:.0f}с при вызове claude -p") from exc
        except FileNotFoundError as exc:
            raise LLMError(
                f"не найден исполняемый файл {self.binary!r} — установите Claude Code"
            ) from exc
        if proc.returncode != 0 and not proc.stdout.strip():
            text = f"{proc.stderr} {proc.stdout}"
            if _looks_like_quota(text):
                raise QuotaExhausted(f"лимит подписки Claude: {text.strip()[:200]}")
            raise LLMError(
                f"claude -p завершился с кодом {proc.returncode}: {proc.stderr[:300]}"
            )
        return parse_cli_output(proc.stdout)
