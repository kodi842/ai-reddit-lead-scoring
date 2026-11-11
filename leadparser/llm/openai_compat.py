from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .base import LLMError, QuotaExhausted

SCHEMA_INSTRUCTION = (
    "\n\nReturn a single JSON object and nothing else. "
    "It must validate against this JSON Schema:\n{schema}"
)


class OpenAICompatBackend:
    """DeepSeek / Ollama / OpenRouter — резервный платный или локальный бэкенд."""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "timeout": timeout_s,
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        }
        if transport is not None:
            kwargs["transport"] = transport
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(**kwargs)

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, effort: str
    ) -> dict[str, Any]:
        system_full = system + SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema, ensure_ascii=False)
        )
        body = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_full},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = self._http.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"сетевая ошибка при вызове {self.base_url}: {exc}") from exc
        if resp.status_code in (402, 429):
            raise QuotaExhausted(
                f"{resp.status_code} от {self.base_url}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise LLMError(f"{resp.status_code} от {self.base_url}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"не JSON в ответе {resp.status_code}: {resp.text[:300]!r}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"ответ {resp.status_code} не объект: {resp.text[:300]!r}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("в ответе нет choices")
        content = (choices[0].get("message") or {}).get("content") or ""
        cleaned = content.strip()
        match = re.match(r'^```(?:\w+)?\n?(.*?)\n?```$', cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise LLMError(f"ответ модели не JSON: {content[:300]!r}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"ответ модели не объект: {content[:300]!r}")
        return parsed
