from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["green", "yellow", "red"]
ThreadStatus = Literal["open", "closed"]

BAND_HOT = 80
BAND_WARM = 70
MIN_LEAD_SCORE = 55


def band(score: int) -> str:
    """Эмодзи-бэнд балла, как в образцах отчётов."""
    if score >= BAND_HOT:
        return "🔥"
    if score >= BAND_WARM:
        return "☀️"
    return "🌤"


class Post(BaseModel):
    thread_id: str
    source_key: str
    url: str
    title: str
    body: str
    author: str
    author_url: str
    created_at: datetime
    flair: str | None = None
    num_comments: int = 0
    stickied: bool = False
    removed: bool = False
    raw_json: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    thread_id: str
    msg_id: str
    parent_id: str | None = None
    author: str
    body: str
    created_at: datetime
    depth: int = 0
    score: int = 0


class PrefilterItem(BaseModel):
    id: str
    candidate: bool
    reason: str = ""


class PrefilterBatch(BaseModel):
    items: list[PrefilterItem] = Field(default_factory=list)


class Lead(BaseModel):
    username: str
    score: int = Field(ge=0, le=100)
    quote: str
    quote_ru: str
    lang: str = "en"
    risk_level: RiskLevel
    risk_note: str = ""
    why: str = ""
    offer: str = ""


class ThreadAnalysis(BaseModel):
    problem_title_ru: str
    status: ThreadStatus = "open"
    leads: list[Lead] = Field(default_factory=list)

    @property
    def max_score(self) -> int:
        return max((lead.score for lead in self.leads), default=0)


PREFILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "candidate", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "candidate": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["problem_title_ru", "status", "leads"],
    "properties": {
        "problem_title_ru": {"type": "string"},
        "status": {"type": "string", "enum": ["open", "closed"]},
        "leads": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "username", "score", "quote", "quote_ru", "lang",
                    "risk_level", "risk_note", "why", "offer",
                ],
                "properties": {
                    "username": {"type": "string"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "quote": {"type": "string"},
                    "quote_ru": {"type": "string"},
                    "lang": {"type": "string"},
                    "risk_level": {"type": "string", "enum": ["green", "yellow", "red"]},
                    "risk_note": {"type": "string"},
                    "why": {"type": "string"},
                    "offer": {"type": "string"},
                },
            },
        },
    },
}
