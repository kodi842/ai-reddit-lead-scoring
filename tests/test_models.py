from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from leadparser.models import (
    ANALYSIS_SCHEMA,
    PREFILTER_SCHEMA,
    Lead,
    Message,
    Post,
    PrefilterBatch,
    ThreadAnalysis,
    band,
)


def test_band_thresholds():
    assert band(88) == "🔥"
    assert band(80) == "🔥"
    assert band(79) == "☀️"
    assert band(70) == "☀️"
    assert band(69) == "🌤"
    assert band(55) == "🌤"


def test_lead_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        Lead(
            username="x", score=101, quote="q", quote_ru="ц", lang="en",
            risk_level="green", risk_note="n", why="w", offer="o",
        )


def test_lead_rejects_unknown_risk_level():
    with pytest.raises(ValidationError):
        Lead(
            username="x", score=80, quote="q", quote_ru="ц", lang="en",
            risk_level="purple", risk_note="n", why="w", offer="o",
        )


def test_analysis_max_score():
    def lead(score: int) -> Lead:
        return Lead(
            username="u", score=score, quote="q", quote_ru="ц", lang="en",
            risk_level="green", risk_note="n", why="w", offer="o",
        )

    analysis = ThreadAnalysis(
        problem_title_ru="Нужен бот", status="open", leads=[lead(60), lead(82)]
    )
    assert analysis.max_score == 82
    assert ThreadAnalysis(problem_title_ru="t", status="open", leads=[]).max_score == 0


def test_prefilter_batch_parses():
    batch = PrefilterBatch.model_validate(
        {"items": [{"id": "reddit:a1", "candidate": True, "reason": "wants to hire"}]}
    )
    assert batch.items[0].candidate is True


def test_post_and_message_construct():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    post = Post(
        thread_id="reddit:a1", source_key="reddit:n8n", url="https://reddit.com/x",
        title="t", body="b", author="bob", author_url="https://reddit.com/user/bob",
        created_at=now, flair=None, num_comments=3, stickied=False, removed=False,
        raw_json={},
    )
    assert post.thread_id == "reddit:a1"
    msg = Message(
        thread_id="reddit:a1", msg_id="c1", parent_id=None, author="bob",
        body="hi", created_at=now, depth=0, score=1,
    )
    assert msg.depth == 0


def test_schemas_are_valid_json_schema():
    assert PREFILTER_SCHEMA["type"] == "object"
    assert PREFILTER_SCHEMA["required"] == ["items"]
    assert ANALYSIS_SCHEMA["properties"]["leads"]["type"] == "array"
    lead_props = ANALYSIS_SCHEMA["properties"]["leads"]["items"]["properties"]
    assert set(lead_props) == {
        "username", "score", "quote", "quote_ru", "lang",
        "risk_level", "risk_note", "why", "offer",
    }
