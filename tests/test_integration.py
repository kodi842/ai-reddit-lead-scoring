"""Тесты, которым нужны сеть, BlankTrail или реальный вызов LLM.

Запуск: .venv/bin/python -m pytest -m integration -v
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest

from leadparser.config import RedditConfig, load_config
from leadparser.llm.claude_code import ClaudeCodeBackend
from leadparser.net.blanktrail import BlankTrail, BlankTrailError, ThrottledClient
from leadparser.sources.reddit import RedditSource

pytestmark = pytest.mark.integration

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_lead", "reason"],
    "properties": {
        "is_lead": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}


def test_claude_code_backend_returns_structured_output():
    """Проверяет, что headless claude -p работает под текущей подпиской."""
    backend = ClaudeCodeBackend()
    result = backend.complete_json(
        system="You are a lead qualifier. Answer strictly via the JSON schema.",
        user=(
            "Reddit post from r/n8n: 'Need someone to build an n8n workflow that "
            "syncs HubSpot with Google Sheets, paying $300, DM me'. Is the author a "
            "potential client for a freelance automation developer?"
        ),
        schema=SCHEMA,
        model="haiku",
        effort="low",
    )
    assert result["is_lead"] is True
    assert isinstance(result["reason"], str) and result["reason"]


def test_reddit_listing_through_blanktrail():
    """Проверяет, что Reddit отдаёт RSS (Atom) через порт BlankTrail.

    `.json` Reddit закрыл анонимному доступу (403 с трёх IP и через
    BlankTrail с безупречным отпечатком — это политика API, не детект бота),
    `.rss` остался открыт — источник теперь целиком на нём.
    """
    cfg = load_config(Path("config.yaml"))
    if not cfg.blanktrail.api_key:
        pytest.skip("нет BLANKTRAIL_API_KEY в .env")
    blanktrail = BlankTrail(cfg.blanktrail)
    try:
        blanktrail.ensure_port()
    except BlankTrailError as exc:
        pytest.skip(f"BlankTrail недоступен: {exc}")
    client = ThrottledClient(
        proxy_url=blanktrail.proxy_url,
        interval_s=cfg.reddit.request_interval_s,
        verify=str(cfg.blanktrail.ca_cert) if cfg.blanktrail.ca_cert else True,
    )
    try:
        source = RedditSource(client, RedditConfig(subreddits=["n8n"]))
        since = datetime.now(tz=timezone.utc) - timedelta(days=30)
        posts = list(source.list_new_posts(since))
        assert posts, "листинг r/n8n пуст — проверьте прокси и троттлинг"
        first = posts[0]
        assert first.thread_id.startswith("reddit:")
        assert first.source_key == "reddit:n8n"
        parsed_url = urlparse(first.url)
        assert parsed_url.scheme == "https" and parsed_url.netloc
        messages = source.fetch_thread(first.thread_id)
        assert messages and messages[0].depth == 0
    finally:
        client.close()
