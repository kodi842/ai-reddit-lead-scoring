"""Rich-вывод для `out list`, `out check` и `karma`.

Вынесено из outreach_cli.py по той же причине, по которой рендер отчёта
(report/render.py) отделён от cli.py: модуль с Typer-командами не должен
разбухать от разметки таблиц.
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from .outreach import FunnelStats
from .outreach_check import DISCLAIMER, CheckResult


def visible_flag(channel: str, comment_visible: int | None) -> str:
    if channel != "comment":
        return "—"
    if comment_visible is None:
        return "не проверяли"
    return "виден" if comment_visible else "[red]скрыт[/]"


def payment_flag(status: str, paid_at: str | None) -> str:
    """Ревью (Important #1): деньги должны быть видны построчно, а не только в
    сумме воронки — иначе выигранная, но не оплаченная сделка молча выглядит
    так же, как оплаченная. Не приватная: тем же текстом пользуется дашборд
    (leadparser/dashboard/render.py), брифу важно, чтобы цифры не расходились."""
    if status != "won":
        return "—"
    return "оплачено" if paid_at else "[yellow]не оплачено[/]"


def print_rows_table(console: Console, rows) -> None:
    table = Table(title="Отклики")
    columns = (
        "id", "сабреддит", "канал", "дней назад", "статус", "сумма", "оплата", "срок",
        "комментарий",
    )
    for col in columns:
        table.add_column(col)
    now = datetime.now(timezone.utc)
    for row in rows:
        days_ago = (now - datetime.fromisoformat(row["sent_at"])).days
        amount = f"{row['amount']:g} {row['currency']}" if row["amount"] is not None else "—"
        table.add_row(
            str(row["id"]), row["subreddit"] or "—", row["channel"], str(days_ago),
            row["status"], amount, payment_flag(row["status"], row["paid_at"]),
            row["due_date"] or "—", visible_flag(row["channel"], row["comment_visible"]),
        )
    console.print(table)
    if not rows:
        console.print("Откликов пока нет: leadparser out add <url> --channel comment|dm")


def print_funnel(console: Console, stats: FunnelStats, breakdown, goal_rub: float) -> None:
    conversion = f"{stats.won / stats.sent:.0%}" if stats.sent else "—"
    console.print(
        f"Воронка: отправлено {stats.sent} / ответили {stats.replied} / "
        f"переговоры {stats.negotiating} / заказов {stats.won} / "
        f"заработано {stats.earned_rub:.0f} ₽ (конверсия в заказ: {conversion})"
    )
    if stats.unpaid_rub:
        # Ревью (Important #1): деньги реально не пришли — не в "заработано" и
        # не в прогрессе к цели, но и не пропадают из картины совсем.
        console.print(
            f"[yellow]Договорено, не оплачено: {stats.unpaid_rub:.0f} ₽[/] "
            "— деньги ещё не пришли, в цель не считаются."
        )
    for currency, bucket in sorted(stats.unrated.items()):
        # Ревью: paid и unpaid — раздельные строки, а не одна сумма на валюту.
        # Иначе оплаченная и просто обещанная сделки в одной валюте без курса
        # молча складывались в одно число, и по нему нельзя понять, что из
        # этого уже в кармане.
        if bucket.paid:
            console.print(
                f"[yellow]Ещё выиграно {bucket.paid:g} {currency} (оплачено) — курс "
                "в config.yaml (outreach.rates_to_rub) не задан, в рублёвый итог не включено.[/]"
            )
        if bucket.unpaid:
            console.print(
                f"[yellow]Ещё выиграно {bucket.unpaid:g} {currency} (не оплачено) — курс "
                "в config.yaml (outreach.rates_to_rub) не задан, в рублёвый итог не включено.[/]"
            )
    if breakdown:
        table = Table(title="По сабреддитам")
        for col in ("сабреддит", "откликов", "ответов", "заказов"):
            table.add_column(col)
        for sub, sent, replied, won in breakdown:
            table.add_row(sub, str(sent), str(replied), str(won))
        console.print(table)
    if goal_rub:
        pct = stats.earned_rub / goal_rub * 100
        if stats.earned_rub >= goal_rub:
            console.print(f"Цель {goal_rub:g} ₽ достигнута: {stats.earned_rub:.0f} ₽.")
        else:
            console.print(
                f"Прогресс к цели {goal_rub:g} ₽: {stats.earned_rub:.0f} ₽ "
                f"({pct:.0f}%), осталось {goal_rub - stats.earned_rub:.0f} ₽."
            )


def print_karma_history(console: Console, history, growth: int | None) -> None:
    if not history:
        console.print("Истории кармы пока нет: leadparser karma <число>")
        return
    table = Table(title="Карма")
    for col in ("дата", "карма", "заметка"):
        table.add_column(col)
    for row in history:
        table.add_row(row["recorded_on"], str(row["karma"]), row["note"] or "")
    console.print(table)
    if growth is None:
        console.print("Прирост за 7 дней: истории меньше недели — рано считать.")
    else:
        console.print(f"Прирост за 7 дней: {growth:+d}")


def print_check_result(console: Console, result: CheckResult) -> None:
    if not result.rows and not result.skipped_dm:
        console.print("Нечего проверять: нет активных откликов с каналом comment.")
        return
    for row in result.rows:
        prefix = f"#{row.row_id} r/{row.subreddit or '?'}"
        if row.unparseable:
            console.print(f"{prefix}: не разобрал ссылку {row.url} — пропущено.")
            continue
        if row.error:
            console.print(f"[yellow]{prefix}: проверить не удалось — {row.error}[/]")
            continue
        if row.newly_hidden:
            console.print(
                f"[red]{prefix}: комментарий не найден в публичной ленте — похоже на "
                f"теневую фильтрацию, со своего экрана вы это не увидите. "
                f"Загляните сами: {row.url}[/]"
            )
        elif row.visible:
            movement = ""
            if row.entries_before is not None and row.entries_now > row.entries_before:
                movement = (
                    f", записей в треде стало больше "
                    f"({row.entries_before} → {row.entries_now})"
                )
            console.print(f"{prefix}: комментарий виден{movement}.")
        else:
            console.print(f"{prefix}: комментарий по-прежнему не виден в публичной ленте.")
        if row.author_posted_after_send:
            console.print(
                f"    автор треда писал после вашей отправки — не обязательно ответ вам, "
                f"но стоит заглянуть: {row.url}"
            )
    if result.skipped_dm:
        console.print(
            f"Личных сообщений пропущено: {result.skipped_dm} — их проверить нельзя вовсе."
        )
    console.print(f"\n{DISCLAIMER}")
