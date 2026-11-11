from __future__ import annotations

from typing import Any

from rich.console import Console

from .net.blanktrail import BlankTrail, BlankTrailError


def check_license(blanktrail: BlankTrail, profile: dict[str, Any], console: Console) -> None:
    """Предполётная проверка лицензии BlankTrail — диагностика, не условие запуска.

    На сервисных тарифах порт ходит только на домены из `allowed_domains`, и
    запрос к Reddit вернёт 403 от самого BlankTrail, а не от Reddit — тот же
    симптом, что при блокировке, но другая причина и другое лечение. Поэтому
    печатаем план и предупреждения, но не останавливаем прогон: сбой самого
    запроса к /license/status — тоже только предупреждение, а не основание
    его прерывать.
    """
    try:
        status = blanktrail.license_status()
    except BlankTrailError as exc:
        console.print(f"[yellow]Не удалось проверить лицензию BlankTrail:[/] {exc}")
        return
    console.print(f"BlankTrail: тариф {status.get('plan', '?')}")
    allowed = [str(domain).lower() for domain in (status.get("allowed_domains") or [])]
    if allowed and not any("reddit.com" in domain for domain in allowed):
        console.print(
            "[red]Тариф BlankTrail ограничивает порт списком доменов, и "
            f"reddit.com в него не входит (разрешены: {', '.join(allowed)}) — "
            "запрос к Reddit получит 403 от самого прокси.[/]"
        )
    if profile.get("js_solver") and not status.get("js_solver_max_procs"):
        console.print(
            "[red]js_solver включён в профиле, а js_solver_max_procs=0 — "
            "Challenge Breaker не сработает.[/]"
        )
