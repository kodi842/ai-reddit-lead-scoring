from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import Message, Post

STAGE_2 = (
    "Источник Facebook — этап 2. Нужны: аккаунт-участник групп, список групп в "
    "config.yaml и браузер через порт BlankTrail. См. §12 дизайн-документа."
)


class FacebookSource:
    """Контракт источника Facebook. Реализация — этап 2."""

    name = "facebook"

    def __init__(self, client: Any, groups: list[str]) -> None:
        self.client = client
        self.groups = groups

    def collection_targets(self) -> int:
        return len(self.groups)

    def collection_failures(self) -> list[str]:
        return []

    def list_new_posts(self, since: datetime) -> Iterator[Post]:
        raise NotImplementedError(STAGE_2)

    def fetch_thread(self, thread_id: str) -> list[Message]:
        raise NotImplementedError(STAGE_2)
