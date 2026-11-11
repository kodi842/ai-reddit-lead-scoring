from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import ReportConfig
from ..models import Post, ThreadAnalysis, band
from ..storage import Storage

TEMPLATES_DIR = Path(__file__).parent / "templates"

LANG_FLAGS = {
    "en": "🇬🇧", "ru": "🇷🇺", "es": "🇪🇸", "pt": "🇵🇹", "de": "🇩🇪", "fr": "🇫🇷",
    "it": "🇮🇹", "nl": "🇳🇱", "pl": "🇵🇱", "tr": "🇹🇷", "uk": "🇺🇦", "ar": "🇸🇦",
    "hi": "🇮🇳", "id": "🇮🇩", "tl": "🇵🇭", "vi": "🇻🇳", "th": "🇹🇭", "ja": "🇯🇵",
    "ko": "🇰🇷", "zh": "🇨🇳", "sv": "🇸🇪", "da": "🇩🇰", "no": "🇳🇴", "fi": "🇫🇮",
    "cs": "🇨🇿", "ro": "🇷🇴", "hu": "🇭🇺", "el": "🇬🇷", "he": "🇮🇱",
}
RISK_ICONS = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def flag(lang: str) -> str:
    return LANG_FLAGS.get((lang or "").lower()[:2], "🏳")


def humanize_age(created_at: datetime, now: datetime) -> str:
    delta = now - created_at
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes >= 1440:
        return f"{total_minutes // 1440} дн."
    if total_minutes >= 60:
        return f"{total_minutes // 60} ч."
    return f"{total_minutes} мин."


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_report(
    rows: list[tuple[Post, ThreadAnalysis, bool]],
    storage: Storage,
    cfg: ReportConfig,
    now: datetime,
    pending: int = 0,
) -> str:
    threads: list[dict[str, Any]] = []
    total_leads = 0
    open_threads = 0
    sources: set[str] = set()

    for post, analysis, verified in rows:
        leads = sorted(
            (lead for lead in analysis.leads if lead.score >= cfg.min_score),
            key=lambda lead: lead.score,
            reverse=True,
        )
        if not leads:
            continue
        messages = storage.get_messages(post.thread_id)
        first_message_at = {}
        for msg in messages:
            first_message_at.setdefault(msg.author, msg.created_at)

        lead_views = []
        for lead in leads:
            created_at = first_message_at.get(lead.username, post.created_at)
            lead_views.append(
                {
                    "score": lead.score,
                    "band": band(lead.score),
                    "username": lead.username,
                    "profile_url": f"https://www.reddit.com/user/{lead.username}",
                    "lang": lead.lang,
                    "flag": flag(lead.lang),
                    "date": f"{created_at:%d.%m.%Y}",
                    "age": humanize_age(created_at, now),
                    "quote": lead.quote,
                    "quote_ru": lead.quote_ru,
                    "risk_icon": RISK_ICONS.get(lead.risk_level, "⚪"),
                    "risk_note": lead.risk_note,
                    "why": lead.why,
                    "offer": lead.offer,
                }
            )

        context = [
            {"author": msg.author, "body": msg.body[: cfg.context_message_chars]}
            for msg in messages[: cfg.max_context_messages]
        ]
        threads.append(
            {
                "title": analysis.problem_title_ru,
                "status": analysis.status,
                "verified": verified,
                "source_key": post.source_key,
                "url": post.url,
                "leads": lead_views,
                "context": context,
            }
        )
        total_leads += len(lead_views)
        open_threads += 1 if analysis.status == "open" else 0
        sources.add(post.source_key.split(":", 1)[0])

    template = _environment().get_template("report.html.j2")
    return template.render(
        kpi={
            "threads": len(threads),
            "leads": total_leads,
            "open": open_threads,
            "pending": pending,
        },
        threads=threads,
        min_score=cfg.min_score,
        sources=sorted(sources),
        generated_at=f"{now:%d.%m.%Y %H:%M} UTC",
    )


def write_report(html: str, reports_dir: Path, now: datetime) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"threads_{now:%Y-%m-%d_%H%M%S}.html"
    path.write_text(html, encoding="utf-8")
    return path
