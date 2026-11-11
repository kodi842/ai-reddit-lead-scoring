"""Данные и рендер `leadparser dashboard` — по образцу leadparser/report/render.py:
функция подготовки данных, Jinja2-шаблон с автоэкранированием, `write_*` хелпер.

Инлайновые SVG-графики (funnel_svg/karma_line_svg, см. charts.py) в шаблон НЕ
передаются через `{{ }}` и нигде не помечаются `|safe`: автоэкранирование
Jinja2 превратило бы их теги в текст. Вместо этого шаблон содержит буквенные
токены-плейсхолдеры (см. FUNNEL_TOKEN/KARMA_TOKEN) как обычный статический
текст, а не выражение — Jinja их не трогает вовсе, — и уже готовый HTML
подставляется в них строковой заменой после рендера. Токен оформлен как
HTML-комментарий с угловыми скобками: если бы такая строка встретилась в
заголовке треда или заметке, автоэкранирование превратило бы её `<`/`>` в
сущности до этой замены, так что подменить чужим текстом плейсхолдер нельзя.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import OutreachConfig
from ..outreach import FunnelStats, OutreachStore
from ..outreach_dashboard import action_items, leads_inventory, paid_count
from ..outreach_render import payment_flag, visible_flag
from ..storage import Storage
from .charts import funnel_svg, karma_line_svg

TEMPLATES_DIR = Path(__file__).parent / "templates"
FUNNEL_TOKEN = "<!--DASHBOARD_FUNNEL_SVG-->"
KARMA_TOKEN = "<!--DASHBOARD_KARMA_SVG-->"
STAGE_LABELS = ("Отправлено", "Ответили", "Переговоры", "Заказов", "Оплачено")
_RICH_TAG_RE = re.compile(r"\[/?[a-z]*\]")


def _plain(text: str) -> str:
    """payment_flag/visible_flag пишут Rich-разметку `[color]…[/]` для
    консоли (см. outreach_render.py) — те же слова, но HTML эти квадратные
    скобки не поймёт как цвет и покажет буквально, поэтому снимаем их здесь.
    Закрывающий тег у Rich — голый `[/]` без имени (не `[/color]`), так что
    имя тега тут необязательное (`*`, а не `+`) — иначе `[/]` не совпал бы
    вовсе и утёк бы на страницу текстом."""
    return _RICH_TAG_RE.sub("", text)


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def hero_view(stats: FunnelStats, goal_rub: float) -> dict[str, Any]:
    """Прогресс к цели. `pct`/`reached` — только про оплаченное (earned_rub):
    unpaid_rub и unrated считаются и показываются отдельно, а не в этом числе
    (бриф: "эти два числа нельзя складывать и нельзя показывать одной полосой").

    `unrated` — построчно, а не по валюте: ревью нашло, что оплаченная и
    просто обещанная сделки в одной валюте без курса схлопывались в одну
    сумму на страницу, и по ней нельзя было понять, что из этого уже пришло.
    Строк для одной валюты будет две, если у неё есть и то, и другое.
    """
    unrated_lines = []
    for code, bucket in sorted(stats.unrated.items()):
        if bucket.paid:
            unrated_lines.append({"code": code, "amount": bucket.paid, "paid": True})
        if bucket.unpaid:
            unrated_lines.append({"code": code, "amount": bucket.unpaid, "paid": False})
    return {
        "earned_rub": stats.earned_rub,
        "goal_rub": goal_rub,
        "pct": (stats.earned_rub / goal_rub * 100) if goal_rub else None,
        "reached": bool(goal_rub) and stats.earned_rub >= goal_rub,
        "unpaid_rub": stats.unpaid_rub,
        "unrated": unrated_lines,
    }


def funnel_view(stats: FunnelStats, paid: int) -> dict[str, Any]:
    values = (stats.sent, stats.replied, stats.negotiating, stats.won, paid)
    stages = list(zip(STAGE_LABELS, values))
    return {
        "stages": stages,
        "conversion_pct": (stats.won / stats.sent * 100) if stats.sent else None,
        "svg": funnel_svg(stages),
    }


def karma_view(history: list[sqlite3.Row], now: datetime) -> dict[str, Any]:
    if len(history) < 2:
        return {"has_chart": False}
    points = [(date.fromisoformat(r["recorded_on"]), r["karma"]) for r in history]
    return {
        "has_chart": True,
        "svg": karma_line_svg(points, today=now.date()),
        "latest": points[-1][1],
        "since": history[0]["recorded_on"],
    }


def outreach_table(rows: list[sqlite3.Row], now: datetime) -> list[dict[str, Any]]:
    """Надмножество `out list` (outreach_render.print_rows_table), плюс
    кликабельная ссылка на тред — буквально то, что просит бриф. Те же
    колонки в том же порядке (включая "дней назад" — брифу важно, чтобы
    страница не расходилась с тем, что владелец уже видит в терминале)."""
    table = []
    for row in rows:
        amount = f"{row['amount']:g} {row['currency']}" if row["amount"] is not None else "—"
        days_ago = (now - datetime.fromisoformat(row["sent_at"])).days
        table.append({
            "id": row["id"], "subreddit": row["subreddit"] or "—", "channel": row["channel"],
            "days_ago": days_ago, "url": row["url"], "status": row["status"], "amount": amount,
            "payment": _plain(payment_flag(row["status"], row["paid_at"])),
            "due_date": row["due_date"] or "—",
            "comment_flag": _plain(visible_flag(row["channel"], row["comment_visible"])),
        })
    return table


def render_dashboard(
    storage: Storage, store: OutreachStore, outreach_cfg: OutreachConfig,
    min_score: int, now: datetime,
) -> str:
    stats = store.funnel(outreach_cfg.rates_to_rub)
    funnel = funnel_view(stats, paid_count(store))
    karma = karma_view(store.karma_history(), now)
    context = {
        "hero": hero_view(stats, outreach_cfg.goal_rub),
        "actions": action_items(storage, store, now),
        "funnel": funnel,
        "karma": karma,
        "leads": leads_inventory(storage, store, min_score),
        "rows": outreach_table(store.list_rows(), now),
        "generated_at": f"{now:%d.%m.%Y %H:%M} UTC",
    }
    html = _environment().get_template("dashboard.html.j2").render(**context)
    html = html.replace(FUNNEL_TOKEN, funnel["svg"], 1)
    if karma["has_chart"]:
        html = html.replace(KARMA_TOKEN, karma["svg"], 1)
    return html


def write_dashboard(html: str, reports_dir: Path) -> Path:
    """Один файл по стабильному пути — без метки времени, каждый раз поверх
    себя же: владелец открывает его один раз и дальше просто обновляет страницу."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "dashboard.html"
    path.write_text(html, encoding="utf-8")
    return path
