"""Запросы для `leadparser dashboard`, которых нет ни в OutreachStore
(leadparser/outreach.py), ни в Storage (leadparser/storage.py).

Отдельный модуль, а не новые методы прямо в OutreachStore: тот и так у своего
предела в ~250 строк (см. его собственный докстринг). Работает поверх
store.conn — так же, как это уже делают leadparser/outreach_check.py и тесты
(см. tests/test_outreach_check.py:seed()).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .outreach import ACTIVE_CHECK_STATUSES, OutreachStore
from .storage import Storage

# Не импортируем leadparser.outreach_render здесь: он сам импортирует
# outreach_check, а тот — этот модуль (record_movement), так что обратный
# импорт замкнул бы цикл. Форматирование "оплачено/не оплачено" и
# "виден/скрыт" для таблицы откликов поэтому живёт в dashboard/render.py —
# единственном месте, которому по дереву импортов можно смотреть в обе стороны.

# Гиг-сабы недоступны, пока не набрана карма (см. бриф дашборда) — их лидов
# нельзя смешивать с "доступными" в запасе лидов. Сравнение регистронезависимое:
# то, как саб записан в config.yaml/URL, может не совпасть по регистру с этим
# списком буква в букву.
GIG_SUBREDDITS = frozenset({
    "slavelabour", "forhire", "donedirtcheap", "jobbit",
    "hireaprogrammer", "programmingrequests",
})


def record_movement(
    conn: sqlite3.Connection, outreach_id: int, checked_at: str, grew: bool,
    entries_before: int | None, entries_now: int,
) -> None:
    """Персистентный флаг "стало ли записей в треде больше на последней
    проверке" — см. комментарий к outreach_movement в storage_schema.py про то,
    почему это отдельная таблица, а не колонка outreach."""
    conn.execute(
        """INSERT INTO outreach_movement
               (outreach_id, grew, entries_before, entries_now, checked_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(outreach_id) DO UPDATE SET
               grew = excluded.grew, entries_before = excluded.entries_before,
               entries_now = excluded.entries_now, checked_at = excluded.checked_at""",
        (outreach_id, int(grew), entries_before, entries_now, checked_at),
    )
    conn.commit()


def movement_rows(store: OutreachStore) -> list[sqlite3.Row]:
    """Отклики, где на последней автопроверке записей в треде стало больше —
    "grew" отражает именно последнюю проверку, а не "выросло когда-либо".
    Только активные (см. ACTIVE_CHECK_STATUSES): won/lost/ghosted уже решены,
    напоминать о движении в их треде в "что делать сейчас" незачем."""
    placeholders = ", ".join("?" for _ in ACTIVE_CHECK_STATUSES)
    return store.conn.execute(
        f"""SELECT o.*, m.entries_before AS m_entries_before,
                   m.entries_now AS m_entries_now, m.checked_at AS m_checked_at
            FROM outreach o JOIN outreach_movement m ON m.outreach_id = o.id
            WHERE m.grew = 1 AND o.status IN ({placeholders})
            ORDER BY o.sent_at, o.id""",
        ACTIVE_CHECK_STATUSES,
    ).fetchall()


def stale_rows(store: OutreachStore, now: datetime, days: int = 3) -> list[sqlite3.Row]:
    """Отправлено, ответа ещё нет (status='sent') и это было `days`+ дней назад."""
    cutoff = (now - timedelta(days=days)).isoformat()
    return store.conn.execute(
        "SELECT * FROM outreach WHERE status = 'sent' AND sent_at <= ? ORDER BY sent_at",
        (cutoff,),
    ).fetchall()


def hidden_comment_rows(store: OutreachStore) -> list[sqlite3.Row]:
    """comment_visible = 0 — комментарий пропал из публичной ленты Reddit.
    Только активные (см. ACTIVE_CHECK_STATUSES): сделка уже закрыта — won,
    lost или ghosted — действовать по видимости комментария больше не по чему."""
    placeholders = ", ".join("?" for _ in ACTIVE_CHECK_STATUSES)
    return store.conn.execute(
        f"SELECT * FROM outreach WHERE comment_visible = 0 "
        f"AND status IN ({placeholders}) ORDER BY sent_at",
        ACTIVE_CHECK_STATUSES,
    ).fetchall()


def paid_count(store: OutreachStore) -> int:
    """Заказы, по которым деньги уже пришли — счётчик для воронки дашборда
    (5-й столбец "оплачено"), не сумма: суммы и курсы — забота FunnelStats."""
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM outreach WHERE status = 'won' AND paid_at IS NOT NULL"
    ).fetchone()
    return int(row["n"])


def worked_thread_ids(store: OutreachStore) -> set[str]:
    """Треды, по которым уже был хоть один отклик — граница "лид отработан"."""
    rows = store.conn.execute(
        "SELECT DISTINCT thread_id FROM outreach WHERE thread_id IS NOT NULL"
    ).fetchall()
    return {r["thread_id"] for r in rows}


def _thread_title(storage: Storage, thread_id: str | None) -> str | None:
    """Заголовок треда, если он вообще есть в базе — не для каждого отклика
    тред обязательно собран основным пайплайном (см. OutreachStore.add)."""
    if not thread_id:
        return None
    found = storage.get_post(thread_id)
    return found.title if found else None


def _row_view(storage: Storage, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "subreddit": row["subreddit"] or "—", "url": row["url"],
        "thread_title": _thread_title(storage, row["thread_id"]),
        "status": row["status"], "note": row["note"],
    }


def _fmt_dt(iso: str) -> str:
    """Человеку, не сырым ISO-таймстампом — как generated_at в отчёте
    (см. leadparser/report/render.py)."""
    return f"{datetime.fromisoformat(iso):%d.%m.%Y %H:%M}"


def action_items(storage: Storage, store: OutreachStore, now: datetime) -> dict:
    """Три списка блока "что делать сейчас" — см. бриф дашборда, п.2."""
    stale = [
        {**_row_view(storage, r), "days_ago": (now - datetime.fromisoformat(r["sent_at"])).days}
        for r in stale_rows(store, now)
    ]
    movement = [
        {**_row_view(storage, r), "entries_before": r["m_entries_before"],
         "entries_now": r["m_entries_now"]}
        for r in movement_rows(store)
    ]
    hidden = [
        {**_row_view(storage, r), "checked_at": _fmt_dt(r["last_checked_at"])}
        for r in hidden_comment_rows(store)
    ]
    return {"stale": stale, "movement": movement, "hidden": hidden}


@dataclass
class LeadsInventory:
    available: int
    gig_locked: int
    by_subreddit: list[tuple[str, int, bool]]  # (сабреддит, лидов, это гиг-саб?)


def leads_inventory(storage: Storage, store: OutreachStore, min_score: int) -> LeadsInventory:
    """Неотработанные лиды min_score+, разбивка по сабреддитам, гиг-сабы отдельно.

    "Неотработанный" = тред, по которому ни разу не было отклика (см.
    worked_thread_ids) — outreach привязан к треду, а не к отдельному лиду
    внутри него, так что это и есть доступная нам граница.
    """
    worked = worked_thread_ids(store)
    counts: dict[str, int] = defaultdict(int)
    for post, analysis, _verified in storage.threads_for_report(min_score):
        if post.thread_id in worked:
            continue
        parts = post.source_key.split(":", 1)
        sub = parts[1] if len(parts) > 1 else parts[0]
        n = sum(1 for lead in analysis.leads if lead.score >= min_score)
        if n:
            counts[sub] += n
    by_sub = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    gig_total = sum(n for sub, n in by_sub if sub.lower() in GIG_SUBREDDITS)
    grand_total = sum(n for _, n in by_sub)
    return LeadsInventory(
        available=grand_total - gig_total,
        gig_locked=gig_total,
        by_subreddit=[(sub, n, sub.lower() in GIG_SUBREDDITS) for sub, n in by_sub],
    )
