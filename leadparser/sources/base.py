from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol

from ..models import Message, Post


class Source(Protocol):
    name: str

    def list_new_posts(self, since: datetime) -> Iterator[Post]:
        """Посты источника, созданные не раньше `since`, от новых к старым."""
        ...

    def fetch_thread(self, thread_id: str) -> list[Message]:
        """Пост и его комментарии одним плоским списком, включая корневое сообщение."""
        ...

    def collection_targets(self) -> int:
        """Сколько целей сбора настроено (сабреддитов, групп)."""
        ...

    def collection_failures(self) -> list[str]:
        """Цели, сорвавшиеся в последнем `list_new_posts`, человекочитаемо."""
        ...
