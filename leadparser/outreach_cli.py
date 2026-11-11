"""Команды `out ...` и `karma`, монтируемые в основной `cli.app`.

Отдельный модуль по той же причине, что и leadparser/outreach.py: cli.py уже
у предела в ~250 строк. `_load_config`/`_build_sources` — те же, что строит
`run` для похода в Reddit (см. cli.py); импортируем их лениво, внутри тел
команд, а не на верхнем уровне модуля — на верхнем уровне cli.py монтирует
этот модуль (`from .outreach_cli import ...`), и обратный импорт "на старте"
дал бы цикл. Внутри функции тот же импорт срабатывает как обычный поиск
атрибута в уже загруженном модуле — cli.py к моменту вызова любой команды
всегда полностью импортирован. Разметка таблиц и текста — в outreach_render.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from .outreach import OutreachError, OutreachStore, mentions_reddit, parse_reddit_url
from .outreach_check import run_check
from .outreach_render import (
    print_check_result, print_funnel, print_karma_history, print_rows_table,
)
from .storage import Storage

out_app = typer.Typer(help="Учёт откликов на Reddit: кому писали и чем кончилось")
console = Console()
CONFIG_OPTION = typer.Option(Path("config.yaml"), "--config", help="путь к config.yaml")


def _open(config: Path):
    from .cli import _load_config
    cfg = _load_config(config)
    storage = Storage(cfg.db_path)
    return cfg, storage, OutreachStore(storage)


def _fail(exc: OutreachError) -> None:
    console.print(f"[red]{exc}[/]")
    raise typer.Exit(code=1)


@out_app.command("add")
def add_cmd(
    url: str,
    channel: str = typer.Option(..., "--channel", help="comment|dm"),
    message: str = typer.Option(None, "--message", help="что отправили"),
    note: str = typer.Option(None, "--note"),
    config: Path = CONFIG_OPTION,
) -> None:
    """Записать отправленный отклик (комментарий или личное сообщение)."""
    _, storage, store = _open(config)
    try:
        row_id = store.add(url=url, channel=channel, message=message, note=note)
    except OutreachError as exc:
        _fail(exc)
    finally:
        storage.close()
    console.print(f"[green]Записано:[/] отклик #{row_id} ({channel})")
    # Ревью (Important #3): ссылка явно про Reddit, но не подошла ни под одну
    # известную форму (redd.it, .../comments/id, .../r/sub/comments/id) —
    # видно сразу, а не спустя недели, когда out check тихо её не проверял.
    _, thread_key = parse_reddit_url(url)
    if thread_key is None and mentions_reddit(url):
        console.print(
            f"[yellow]Похоже на ссылку Reddit, но не разобрал сабреддит и id поста: "
            f"{url}[/] — автопроверка эту строку не увидит."
        )


@out_app.command("set")
def set_cmd(
    row_id: int, status: str, note: str = typer.Option(None, "--note"),
    config: Path = CONFIG_OPTION,
) -> None:
    """Сменить статус отклика (sent|replied|negotiating|lost|ghosted; для won
    используйте `out won`, ему нужны сумма и валюта)."""
    _, storage, store = _open(config)
    try:
        store.set_status(row_id, status, note)
    except OutreachError as exc:
        _fail(exc)
    finally:
        storage.close()
    console.print(f"[green]Отклик #{row_id}[/] → {status}")


@out_app.command("won")
def won_cmd(
    row_id: int,
    amount: float = typer.Option(..., "--amount"),
    currency: str = typer.Option(..., "--currency"),
    due: str = typer.Option(None, "--due", help="срок оплаты, ГГГГ-ММ-ДД"),
    config: Path = CONFIG_OPTION,
) -> None:
    """Отметить сделку выигранной: статус won, сумма, валюта, срок."""
    if due is not None:
        try:
            date.fromisoformat(due)
        except ValueError:
            console.print(f"[red]--due должен быть ГГГГ-ММ-ДД, получено {due!r}[/]")
            raise typer.Exit(code=1)
    _, storage, store = _open(config)
    try:
        store.mark_won(row_id, amount=amount, currency=currency, due=due)
    except OutreachError as exc:
        _fail(exc)
    finally:
        storage.close()
    console.print(f"[green]Отклик #{row_id}[/] → won, {amount:g} {currency.upper()}")


@out_app.command("paid")
def paid_cmd(row_id: int, config: Path = CONFIG_OPTION) -> None:
    """Проставить дату оплаты."""
    _, storage, store = _open(config)
    try:
        store.mark_paid(row_id)
    except OutreachError as exc:
        _fail(exc)
    finally:
        storage.close()
    console.print(f"[green]Отклик #{row_id}[/] отмечен оплаченным")


@out_app.command("list")
def list_cmd(
    status: str = typer.Option(None, "--status", help="показать только этот статус"),
    show_all: bool = typer.Option(False, "--all", help="не скрывать lost/ghosted"),
    config: Path = CONFIG_OPTION,
) -> None:
    """Таблица откликов, воронка и прогресс к недельной цели."""
    cfg, storage, store = _open(config)
    try:
        rows = store.list_rows(status=status, show_all=show_all)
        funnel = store.funnel(cfg.outreach.rates_to_rub)
        breakdown = store.subreddit_breakdown()
    finally:
        storage.close()
    print_rows_table(console, rows)
    print_funnel(console, funnel, breakdown, cfg.outreach.goal_rub)


def karma(
    value: int = typer.Argument(None, help="текущая карма аккаунта"),
    note: str = typer.Option(None, "--note"),
    config: Path = CONFIG_OPTION,
) -> None:
    """Записать снимок кармы за сегодня, либо (без аргумента) показать историю."""
    _, storage, store = _open(config)
    try:
        if value is not None:
            store.record_karma(value, note)
        history = store.karma_history()
        growth = store.karma_growth_7d()
    finally:
        storage.close()
    if value is not None:
        console.print(f"[green]Карма записана:[/] {value} на {date.today().isoformat()}")
    print_karma_history(console, history, growth)


@out_app.command("check")
def check_cmd(config: Path = CONFIG_OPTION) -> None:
    """Автопроверка активных откликов с каналом comment — сходить и посмотреть,
    а не «есть ли ответ»: это лента без родителя, и вслепую отличить ответ вам
    от чужого комментария нельзя (см. текст в конце вывода)."""
    from .cli import _build_sources
    from .net.blanktrail import BlankTrailError

    cfg, storage, store = _open(config)
    try:
        if not cfg.outreach.reddit_username:
            console.print(
                "[red]В config.yaml не задан outreach.reddit_username[/] — без него "
                "не с чем сравнивать авторов в ленте RSS."
            )
            raise typer.Exit(code=1)
        try:
            sources, client = _build_sources(cfg)
        except BlankTrailError as exc:
            console.print(f"[red]Не могу начать проверку:[/] {exc}")
            raise typer.Exit(code=1)
        try:
            result = run_check(
                store, sources["reddit"], cfg.outreach.reddit_username,
                datetime.now(timezone.utc),
            )
        finally:
            client.close()
    finally:
        storage.close()
    print_check_result(console, result)
