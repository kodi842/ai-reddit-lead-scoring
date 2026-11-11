"""Команда `leadparser dashboard`, монтируемая в основной cli.app.

Отдельный модуль по той же причине, что и outreach_cli.py (см. его
докстринг): cli.py уже у предела ~250 строк. `_load_config` — тот же, что
использует `report`; импортируем его лениво, внутри тела команды, а не на
верхнем уровне модуля — на верхнем уровне cli.py монтирует этот модуль
(`from .dashboard_cli import dashboard_cmd`), и импорт "на старте" в обратную
сторону дал бы цикл. Рендер — в leadparser/dashboard/render.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from .dashboard.render import render_dashboard, write_dashboard
from .outreach import OutreachStore
from .storage import Storage

console = Console()
CONFIG_OPTION = typer.Option(Path("config.yaml"), "--config", help="путь к config.yaml")


def dashboard_cmd(config: Path = CONFIG_OPTION) -> None:
    """Один HTML-файл на стабильном пути (`<reports_dir>/dashboard.html`,
    перезаписывается при каждом вызове): прогресс к цели, что делать сейчас,
    воронка, карма по дням, запас лидов, таблица откликов."""
    from .cli import _load_config

    cfg = _load_config(config)
    storage = Storage(cfg.db_path)
    try:
        store = OutreachStore(storage)
        now = datetime.now(timezone.utc)
        html = render_dashboard(storage, store, cfg.outreach, cfg.report.min_score, now)
        path = write_dashboard(html, cfg.reports_dir)
    finally:
        storage.close()
    console.print(f"[green]Дашборд:[/] {path}")
