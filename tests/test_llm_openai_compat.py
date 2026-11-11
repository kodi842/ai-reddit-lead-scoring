import json

import httpx
import pytest

from leadparser.config import BackendConfig, LLMConfig, StageConfig
from leadparser.llm.base import LLMError, QuotaExhausted
from leadparser.llm.factory import build_backends
from leadparser.llm.openai_compat import OpenAICompatBackend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def backend_with(handler) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        base_url="https://api.deepseek.com", api_key="k-1",
        transport=httpx.MockTransport(handler),
    )


def test_posts_chat_completion_with_json_mode():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}}]
        })

    result = backend_with(handler).complete_json(
        "SYS", "USER", SCHEMA, "deepseek-flash", "low"
    )
    assert result == {"ok": True}
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer k-1"
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in seen["body"]["messages"][0]["content"]
    assert '"required"' in seen["body"]["messages"][0]["content"]
    assert seen["body"]["messages"][0]["content"].startswith("SYS")
    assert seen["body"]["messages"][1] == {"role": "user", "content": "USER"}


def test_strips_markdown_fence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '```json\n{"ok": false}\n```'}}]
        })

    assert backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low") == {"ok": False}


def test_raises_on_invalid_json_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "nope"}}]})

    with pytest.raises(LLMError, match="не JSON"):
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")


def test_raises_on_empty_choices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LLMError, match="choices"):
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")


def test_429_is_quota_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    with pytest.raises(QuotaExhausted):
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")


def test_402_is_quota_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="insufficient balance")

    with pytest.raises(QuotaExhausted):
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")


def test_500_is_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with pytest.raises(LLMError, match="500"):
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")


def test_200_with_non_json_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(LLMError, match="не JSON") as exc_info:
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")
    assert not isinstance(exc_info.value, QuotaExhausted)


def test_200_with_json_list_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with pytest.raises(LLMError, match="не объект") as exc_info:
        backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low")
    assert not isinstance(exc_info.value, QuotaExhausted)


def test_strips_markdown_fence_single_line():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '```{"ok": true}```'}}]
        })

    assert backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low") == {"ok": True}


def test_strips_markdown_fence_with_language_tag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]
        })

    assert backend_with(handler).complete_json("s", "u", SCHEMA, "m", "low") == {"ok": True}


def test_build_backends_creates_only_used_ones():
    cfg = LLMConfig(
        backends={
            "claude_code": BackendConfig(type="claude_code"),
            "deepseek": BackendConfig(
                type="openai_compat", base_url="https://api.deepseek.com",
                api_key_env="DEEPSEEK_API_KEY",
            ),
        },
        stages={"prefilter": StageConfig(backend="claude_code", model="haiku")},
    )
    backends = build_backends(cfg)
    assert set(backends) == {"claude_code"}
    assert backends["claude_code"].name == "claude_code"


def test_build_backends_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = LLMConfig(
        backends={"deepseek": BackendConfig(
            type="openai_compat", base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        )},
        stages={"score": StageConfig(backend="deepseek", model="deepseek-v4-pro")},
    )
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        build_backends(cfg)


def test_build_backends_rejects_unknown_type():
    cfg = LLMConfig(
        backends={"weird": BackendConfig(type="telepathy")},
        stages={"score": StageConfig(backend="weird", model="x")},
    )
    with pytest.raises(LLMError, match="telepathy"):
        build_backends(cfg)
