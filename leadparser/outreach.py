"""Учёт откликов ("куда писал") и снимков кармы аккаунта.

Отдельный модуль, а не разбухший storage.py: тот уже был близко к пределу.
Таблицы (`outreach`, `karma`) — в storage_schema.py, рядом с остальной DDL;
здесь — запросы к ним (`OutreachStore`) и конвертация валюты (`funnel`).
Разбор ссылки на тред — в outreach_url.py (тоже ради размера файла); отсюда
`parse_reddit_url`/`mentions_reddit` только реэкспортируются, чтобы CLI,
автопроверка и тесты не знали, что он переехал.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .outreach_url import mentions_reddit, parse_reddit_url  # реэкспорт, см. outreach_url.py
from .storage import Storage

__all__ = [
    "ACTIVE_CHECK_STATUSES", "CHANNELS", "FunnelStats", "HIDDEN_BY_DEFAULT",
    "NEGOTIATING_STATUSES", "OutreachError", "OutreachStore", "REPLIED_STATUSES", "STATUSES",
    "UnratedAmount", "mentions_reddit", "parse_reddit_url",
]

CHANNELS = ("comment", "dm")
STATUSES = ("sent", "replied", "negotiating", "won", "lost", "ghosted")
# Статусы, которые подразумевают, что ответ уже был: нет отдельного флага
# "был ли ответ" — переговоры и заказ логически невозможны без него.
REPLIED_STATUSES = ("replied", "negotiating", "won")
NEGOTIATING_STATUSES = ("negotiating", "won")
HIDDEN_BY_DEFAULT = ("lost", "ghosted")
# Отдельно от REPLIED_STATUSES: won/lost/ghosted — финал, автопроверке (см.
# outreach_check.py) там больше нечего делать, а REPLIED_STATUSES нарочно
# включает "won" для воронки (заказ подразумевает, что до этого был ответ).
ACTIVE_CHECK_STATUSES = ("sent", "replied", "negotiating")


class OutreachError(ValueError):
    """Неверный ввод в команде `out`/`karma` — красная строка, а не traceback."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UnratedAmount:
    """Сумма в валюте без курса в rates_to_rub — оплаченное и договорённое
    раздельно. Ревью: раньше это было одно число на валюту, и выигранная (уже
    пришедшая) и просто обещанная сделки в одной неизвестной валюте молча
    схлопывались в сумму, по которой нельзя понять, что из этого уже в кармане."""
    paid: float = 0.0
    unpaid: float = 0.0


@dataclass
class FunnelStats:
    sent: int
    replied: int
    negotiating: int
    won: int
    earned_rub: float     # won И оплачено (paid_at задан) — деньги реально пришли
    unpaid_rub: float     # won, но paid_at ещё нет — договорено, но не в кармане
    unrated: dict[str, UnratedAmount] = field(default_factory=dict)


class OutreachStore:
    """Тонкая обёртка над тем же sqlite3-соединением, что и `Storage`."""

    def __init__(self, storage: Storage) -> None:
        self.conn = storage.conn

    # --- outreach: запись и правки ---

    def add(
        self, url: str, channel: str, message: str | None = None, note: str | None = None,
    ) -> int:
        if channel not in CHANNELS:
            raise OutreachError(
                f"--channel должен быть comment или dm, получено {channel!r}"
            )
        subreddit, parsed_thread_id = parse_reddit_url(url)
        thread_id = None
        if parsed_thread_id and self._thread_exists(parsed_thread_id):
            thread_id = parsed_thread_id
        now = _now()
        cur = self.conn.execute(
            """
            INSERT INTO outreach
                (thread_id, url, subreddit, channel, sent_at, message, note, status, updated_at)
            VALUES (?,?,?,?,?,?,?,'sent',?)
            """,
            (thread_id, url, subreddit, channel, now, message, note, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _thread_exists(self, thread_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row is not None

    def get(self, row_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM outreach WHERE id = ?", (row_id,)).fetchone()

    def _require(self, row_id: int) -> sqlite3.Row:
        row = self.get(row_id)
        if row is None:
            raise OutreachError(f"отклик #{row_id} не найден")
        return row

    def set_status(self, row_id: int, status: str, note: str | None = None) -> None:
        if status not in STATUSES:
            raise OutreachError(
                f"статус должен быть одним из {', '.join(STATUSES)}, получено {status!r}"
            )
        if status == "won":
            # Ревью: `out set <id> won` в обход mark_won оставлял amount и
            # currency пустыми, а строка всё равно попадала в "заказов" —
            # заказ без суммы врёт воронке и рублёвому итогу.
            raise OutreachError(
                "статус won нельзя поставить через `out set` — нужны сумма и "
                "валюта, используйте `out won <id> --amount ... --currency ...`"
            )
        self._require(row_id)
        if note is None:
            self.conn.execute(
                "UPDATE outreach SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), row_id),
            )
        else:
            self.conn.execute(
                "UPDATE outreach SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                (status, note, _now(), row_id),
            )
        self.conn.commit()

    def mark_won(self, row_id: int, amount: float, currency: str, due: str | None) -> None:
        self._require(row_id)
        self.conn.execute(
            """UPDATE outreach SET status = 'won', amount = ?, currency = ?, due_date = ?,
                   updated_at = ? WHERE id = ?""",
            (amount, currency.upper(), due, _now(), row_id),
        )
        self.conn.commit()

    def mark_paid(self, row_id: int, paid_at: str | None = None) -> None:
        row = self._require(row_id)
        if row["status"] != "won":
            # Схема (см. storage_schema.py) сама говорит: amount/currency/
            # due_date/paid_at "заполняются при статусе won" — paid_at раньше
            # won описывает оплату сделки, которой по документам ещё нет.
            raise OutreachError(
                f"отклик #{row_id} ещё не в статусе won (сейчас {row['status']!r}) — "
                "сначала `out won <id> --amount ... --currency ...`"
            )
        self.conn.execute(
            "UPDATE outreach SET paid_at = ?, updated_at = ? WHERE id = ?",
            (paid_at or _now(), _now(), row_id),
        )
        self.conn.commit()

    def record_check(self, row_id: int, checked_at: str, entries: int, visible: bool) -> None:
        self.conn.execute(
            """UPDATE outreach SET last_checked_at = ?, last_seen_entries = ?,
                   comment_visible = ?, updated_at = ? WHERE id = ?""",
            (checked_at, entries, int(visible), _now(), row_id),
        )
        self.conn.commit()

    # --- outreach: чтение ---

    def list_rows(self, status: str | None = None, show_all: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM outreach"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        elif not show_all:
            placeholders = ", ".join("?" for _ in HIDDEN_BY_DEFAULT)
            sql += f" WHERE status NOT IN ({placeholders})"
            params.extend(HIDDEN_BY_DEFAULT)
        sql += " ORDER BY sent_at, id"
        return self.conn.execute(sql, params).fetchall()

    def rows_to_check(self) -> list[sqlite3.Row]:
        """Кандидаты автопроверки — см. leadparser/outreach_check.py."""
        placeholders = ", ".join("?" for _ in ACTIVE_CHECK_STATUSES)
        return self.conn.execute(
            f"SELECT * FROM outreach WHERE status IN ({placeholders}) ORDER BY id",
            ACTIVE_CHECK_STATUSES,
        ).fetchall()

    def funnel(self, rates: dict[str, float]) -> FunnelStats:
        rows = self.conn.execute(
            "SELECT status, amount, currency, paid_at FROM outreach"
        ).fetchall()
        sent = len(rows)
        replied = sum(1 for r in rows if r["status"] in REPLIED_STATUSES)
        negotiating = sum(1 for r in rows if r["status"] in NEGOTIATING_STATUSES)
        won = sum(1 for r in rows if r["status"] == "won")
        earned_rub = 0.0
        unpaid_rub = 0.0
        unrated: dict[str, UnratedAmount] = defaultdict(UnratedAmount)
        for row in rows:
            if row["status"] != "won" or row["amount"] is None:
                continue
            currency = (row["currency"] or "").upper()
            rate = rates.get(currency)
            if rate is None:
                # Нет курса — неизвестно, сколько это в рублях, независимо от
                # того, оплачено или нет: показываем отдельно в любом случае
                # (см. test_funnel_keeps_currency_without_configured_rate_...).
                # paid/unpaid внутри unrated — тоже раздельно (ревью: иначе
                # оплаченная и обещанная сделки в одной валюте без курса молча
                # складывались в одно число, см. test_funnel_splits_unrated_...).
                bucket = unrated[currency]
                if row["paid_at"] is not None:
                    bucket.paid += row["amount"]
                else:
                    bucket.unpaid += row["amount"]
            elif row["paid_at"] is not None:
                earned_rub += row["amount"] * rate
            else:
                unpaid_rub += row["amount"] * rate
        return FunnelStats(sent, replied, negotiating, won, earned_rub, unpaid_rub, dict(unrated))

    def subreddit_breakdown(self) -> list[tuple[str, int, int, int]]:
        """(сабреддит, откликов, ответов+, заказов) — сортировка по числу откликов."""
        rows = self.conn.execute(
            "SELECT COALESCE(subreddit, '—') AS sub, status FROM outreach"
        ).fetchall()
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for row in rows:
            bucket = counts[row["sub"]]
            bucket[0] += 1
            if row["status"] in REPLIED_STATUSES:
                bucket[1] += 1
            if row["status"] == "won":
                bucket[2] += 1
        return sorted(
            ((sub, sent, replied, won) for sub, (sent, replied, won) in counts.items()),
            key=lambda t: t[1], reverse=True,
        )

    # --- карма ---

    def record_karma(self, karma_value: int, note: str | None = None) -> None:
        today = date.today().isoformat()
        self.conn.execute(
            """INSERT INTO karma (recorded_on, karma, note) VALUES (?, ?, ?)
               ON CONFLICT(recorded_on) DO UPDATE SET
                   karma = excluded.karma, note = excluded.note""",
            (today, karma_value, note),
        )
        self.conn.commit()

    def karma_history(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM karma ORDER BY recorded_on").fetchall()

    def karma_growth_7d(self) -> int | None:
        """Прирост кармы за последние 7 дней, или `None`, если история короче
        недели — честно, а не приблизительно от самой первой записи."""
        rows = self.karma_history()
        if len(rows) < 2:
            return None
        latest = rows[-1]
        latest_date = date.fromisoformat(latest["recorded_on"])
        cutoff = latest_date - timedelta(days=7)
        earliest_date = date.fromisoformat(rows[0]["recorded_on"])
        if earliest_date > cutoff:
            return None
        baseline = max(
            (r for r in rows if date.fromisoformat(r["recorded_on"]) <= cutoff),
            key=lambda r: r["recorded_on"],
        )
        return latest["karma"] - baseline["karma"]
