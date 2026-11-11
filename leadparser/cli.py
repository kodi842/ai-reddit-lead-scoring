from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .config import Config, load_config
from .dashboard_cli import dashboard_cmd
from .llm.base import LLMError
from .llm.factory import build_backends
from .net.blanktrail import BlankTrail, BlankTrailError, ThrottledClient
from .outreach_cli import karma, out_app
from .pipeline import Pipeline
from .preflight import check_license
from .report.render import render_report, write_report
from .sources.reddit import RedditSource
from .storage import Storage

app = typer.Typer(help="Парсер лидов из Reddit (и Facebook — этап 2)")
app.add_typer(out_app, name="out")
app.command(name="karma")(karma)
app.command(name="dashboard")(dashboard_cmd)
console = Console()

CONFIG_OPTION = typer.Option(Path("config.yaml"), "--config", help="путь к config.yaml")
MAX_SHOWN_FAILURES = 10
STAGE_LABELS = {
    "collect": "сбор постов", "comments": "комментарии", "prefilter": "предфильтр",
    "score": "скоринг", "verify": "верификация",
}


class StageProgress:
    """Строка прогресса на каждую ступень: прогон идёт часами, и без неё между
    BlankTrail и финальной таблицей CLI молчит (§10 спеки)."""

    def __init__(self, bar: Progress) -> None:
        self._bar = bar
        self._tasks: dict[str, int] = {}

    def __call__(self, stage: str, done: int, total: int | None) -> None:
        if stage not in self._tasks:
            self._tasks[stage] = self._bar.add_task(
                STAGE_LABELS.get(stage, stage), total=total
            )
        self._bar.update(self._tasks[stage], completed=done, total=total)


def _parse_since(value: str | None) -> datetime | None:
    """`--since 2026-09-15` или `--since 2026-09-15T10:00`; наивное время — UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        console.print(
            f"[red]Не разобрал дату[/] {value!r}: нужен ГГГГ-ММ-ДД или ISO-время"
        )
        raise typer.Exit(code=1)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _print_collection_failures(stats: dict) -> None:
    """Сорванные сабреддиты — крупно, а не строчкой warning'а в скролле."""
    failed = list(stats.get("failed_sources") or [])
    if not failed:
        return
    targets = stats.get("source_targets", 0)
    console.print(
        f"[red]Сбор не удался:[/] {len(failed)} из {targets} источников не ответили"
    )
    for line in failed[:MAX_SHOWN_FAILURES]:
        console.print(f"  [red]✗[/] {line}")
    if len(failed) > MAX_SHOWN_FAILURES:
        console.print(f"  … и ещё {len(failed) - MAX_SHOWN_FAILURES}")


def _load_config(path: Path) -> Config:
    """Конфиг проверяется на загрузке — до первого платного вызова модели.

    Ловим ровно четыре вида «файл не годится»: нет файла, не YAML, не ложится
    на схему, ступени/бэкенды не сходятся. Ошибки в коде (TypeError и прочие)
    по-прежнему падают трейсбеком — их надо видеть, а не прятать.
    """
    try:
        return load_config(path)
    except (LLMError, ValidationError, yaml.YAMLError, OSError) as exc:
        console.print(f"[red]Конфиг не годится ({path}):[/] {exc}")
        raise typer.Exit(code=1)


def _stage_table(title: str, counts_for: Callable[[str], dict | None]) -> Table:
    """Таблица ok/ошибок/лимит по ступеням — одна и та же в `run` и в `stats`.

    `None` — «ступень не работала» (прочерки), `{}` — «работала, но пусто» (нули).
    """
    table = Table(title=title)
    table.add_column("ступень")
    for column in ("ok", "ошибок", "лимит"):
        table.add_column(column, justify="right")
    for stage in ("prefilter", "score", "verify"):
        counts = counts_for(stage)
        if counts is None:
            table.add_row(stage, "—", "—", "—")
        else:
            table.add_row(
                stage, *(str(counts.get(key, 0)) for key in ("ok", "error", "quota"))
            )
    return table


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _build_sources(cfg: Config) -> tuple[dict, ThrottledClient]:
    blanktrail = BlankTrail(cfg.blanktrail)
    port = blanktrail.ensure_port()
    console.print(f"[green]BlankTrail[/]: порт {port} готов")
    check_license(blanktrail, cfg.blanktrail.profile, console)
    client = ThrottledClient(
        proxy_url=blanktrail.proxy_url, interval_s=cfg.reddit.request_interval_s,
        verify=str(cfg.blanktrail.ca_cert) if cfg.blanktrail.ca_cert else True,
    )
    return {"reddit": RedditSource(client, cfg.reddit)}, client


@app.command()
def run(
    config: Path = CONFIG_OPTION,
    lookback: int = typer.Option(
        None, "--lookback", help="окно в днях (перебивает конфиг); 0 — нулевое окно"
    ),
    max_posts: int = typer.Option(None, "--max-posts", help="ограничить число постов (проба)"),
    no_report: bool = typer.Option(False, "--no-report", help="не рендерить HTML"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Собрать посты, оценить их LLM-каскадом и отрендерить отчёт."""
    _setup_logging(verbose)
    cfg = _load_config(config)
    # 0 — валидное значение и означает окно нулевой ширины: since = now, то есть
    # ни новых постов, ни тредов в отчёте. Разбор уже собранного идёт как обычно —
    # так «--lookback 0» и догоняет накопившийся хвост.
    if lookback is not None:
        cfg.lookback_days = lookback

    try:
        backends = build_backends(cfg.llm)
        sources, client = _build_sources(cfg)
    except (BlankTrailError, LLMError) as exc:
        console.print(f"[red]Не могу начать прогон:[/] {exc}")
        raise typer.Exit(code=1)

    storage = Storage(cfg.db_path)
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
        ) as bar:
            result = Pipeline(
                cfg, storage, sources, backends, progress=StageProgress(bar)
            ).run(max_posts=max_posts, write_html=not no_report)
    finally:
        client.close()
        storage.close()

    console.print(_stage_table(
        f"Прогон #{result.run_id} — {result.status}", result.stats.get
    ))
    console.print(
        f"постов собрано: {result.stats.get('collected', 0)}, "
        f"отброшено эвристикой: {result.stats.get('skipped', 0)}, "
        f"тредов с комментариями: {result.stats.get('comments_fetched', 0)}"
    )
    _print_collection_failures(result.stats)
    if result.report_path:
        console.print(f"[green]Отчёт:[/] {result.report_path}")
    if result.status == "error":
        console.print(
            f"[red]Прогон оборвался на ступени «{result.stats.get('failed_at')}»:[/] "
            f"{result.stats.get('error')}"
        )
        console.print("Прогресс сохранён — исправьте причину и запустите `run` снова.")
        raise typer.Exit(code=1)
    if result.stats.get("all_sources_failed") and result.status != "paused":
        console.print(
            "[red]Ни один источник не ответил — собирать было нечего.[/] "
            "Проверьте BlankTrail и доступность Reddit, затем повторите."
        )
        raise typer.Exit(code=1)
    if result.status == "paused":
        console.print(
            "[yellow]Прогон приостановлен из-за лимита модели. "
            "Прогресс сохранён — запустите `leadparser run` позже.[/]"
        )
        console.print(
            f"остановились на ступени «{result.stats.get('paused_at')}», "
            f"ждут моего разбора: {result.stats.get('pending')}"
        )
        raise typer.Exit(code=2)


@app.command()
def report(
    config: Path = CONFIG_OPTION,
    since: str = typer.Option(
        None, "--since", help="показывать треды не старше даты (ГГГГ-ММ-ДД)"
    ),
    min_score: int = typer.Option(None, "--min-score", help="порог показа лида"),
) -> None:
    """Перерендерить HTML-отчёт из базы, ничего не запрашивая заново."""
    cfg = _load_config(config)
    if min_score is not None:
        cfg.report.min_score = min_score
    since_dt = _parse_since(since)
    storage = Storage(cfg.db_path)
    try:
        now = datetime.now(tz=timezone.utc)
        rows = storage.threads_for_report(cfg.report.min_score, since_dt)
        html = render_report(rows, storage, cfg.report, now, storage.pending_count())
        path = write_report(html, cfg.reports_dir, now)
    finally:
        storage.close()
    console.print(f"[green]Отчёт:[/] {path}")


@app.command()
def stats(config: Path = CONFIG_OPTION) -> None:
    """Показать, что уже лежит в базе: ступени, статусы, ждущие разбора треды."""
    cfg = _load_config(config)
    storage = Storage(cfg.db_path)
    try:
        counts = storage.stage_counts()
        pending = storage.pending_count()
        rows = storage.threads_for_report(cfg.report.min_score)
    finally:
        storage.close()
    console.print(_stage_table("Состояние базы", lambda stage: counts.get(stage, {})))
    console.print(f"тредов с лидами {cfg.report.min_score}+: {len(rows)}")
    console.print(f"⏳ ждут моего разбора: {pending}")


if __name__ == "__main__":
    app()
