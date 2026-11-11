"""Разбор ссылок на Reddit-тред: сабреддит + id поста.

Вынесено из outreach.py, чтобы тот не разбухал за ~250 строк — разбор трёх
форм ссылки и проверка "это вообще Reddit?" не имеют отношения к хранению,
это чистый разбор текста. `outreach.py` реэкспортирует оба имени, так что
`from .outreach import parse_reddit_url` (уже используется в CLI, автопроверке
и тестах) продолжает работать без изменений.
"""
from __future__ import annotations

import re

# Полная форма несёт сабреддит; короткая (redd.it — то, что даёт кнопка
# «Поделиться» в мобильном приложении) и обрезанная (reddit.com/comments/…,
# без /r/<sub>/) — нет. До фикса обе нестандартные формы молча схлопывались в
# (None, None), как будто это не Reddit вовсе, и out check терял такую строку
# навсегда (id поста не извлекался — проверять было нечего).
_FULL_URL_RE = re.compile(
    r"reddit\.com/r/(?P<sub>[A-Za-z0-9_]+)/comments/(?P<pid>[A-Za-z0-9]+)", re.IGNORECASE
)
_SUBREDDITLESS_RE = re.compile(r"reddit\.com/comments/(?P<pid>[A-Za-z0-9]+)", re.IGNORECASE)
_SHORT_RE = re.compile(r"redd\.it/(?P<pid>[A-Za-z0-9]+)", re.IGNORECASE)


def parse_reddit_url(url: str) -> tuple[str | None, str | None]:
    """`.../r/AI_Agents/comments/1wowt2l/...` → `("AI_Agents", "reddit:1wowt2l")`.

    Короткая (`redd.it/1wowt2l`) и обрезанная (`reddit.com/comments/1wowt2l`)
    формы не несут сабреддита — возвращают `(None, "reddit:1wowt2l")`, id всё
    равно есть и автопроверке этого достаточно.

    `(None, None)`, если ссылка вообще не похожа на тред Reddit — отклик всё
    равно заносится (см. `OutreachStore.add`), просто без сабреддита и id;
    `mentions_reddit` ниже отличает этот случай от «это точно Reddit, просто
    ссылка непривычной формы» для предупреждения в CLI.
    """
    match = _FULL_URL_RE.search(url)
    if match:
        return match.group("sub"), f"reddit:{match.group('pid').lower()}"
    match = _SUBREDDITLESS_RE.search(url) or _SHORT_RE.search(url)
    if match:
        return None, f"reddit:{match.group('pid').lower()}"
    return None, None


def mentions_reddit(url: str) -> bool:
    """Ссылка называет домен Reddit, даже если `parse_reddit_url` её не разобрал —
    сигнал для `out add` предупредить сразу, а не терять строку молча."""
    lowered = url.lower()
    return "reddit.com" in lowered or "redd.it" in lowered
