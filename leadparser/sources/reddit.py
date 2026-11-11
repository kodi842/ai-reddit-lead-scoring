from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ..config import RedditConfig
from ..models import Message, Post

log = logging.getLogger(__name__)

BASE = "https://www.reddit.com"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
# Каждая карточка поста в Reddit'овском `content` заканчивается этим блоком —
# он не текст поста, а дублирует уже известного нам author. У поста без
# selftext (чистая ссылка) это весь content целиком: без вырезания пустое
# тело выглядело бы как правдоподобный текст, а не как честный "тела нет".
_TRAILER_RE = re.compile(
    r"\s*submitted\s+by\s*/u/\S+\s*(?:\[link\]\s*)?(?:\[comments\]\s*)?$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
# AutoModerator — тот же шумовой автор, что heuristic.stop_authors режет на
# уровне постов; его канонический комментарий не должен есть место в промпте
# со своим лимитом символов (render_thread, MAX_TOTAL_CHARS).
_SKIPPED_COMMENT_AUTHORS = {"AutoModerator"}


def _clean_body(raw_html: str | None) -> str:
    """`content` — уже готовый HTML-сниппет Reddit'а, экранированный один раз
    как текст XML: после `.text` теги — настоящие символы `<>`, а сущности
    внутри HTML (например `&#39;`) остаются нераскодированными. Снимаем теги
    регуляркой (стандартный `xml.etree` не парсит произвольный обрывочный
    HTML), раскодируем сущности через `html.unescape` (иначе `&amp;#39;` так
    и останется текстом, а не апострофом), срезаем навигационный хвост
    Reddit'а и схлопываем пробельные разрывы, оставшиеся от вырезанных тегов.
    """
    without_tags = _TAG_RE.sub("", raw_html or "")
    unescaped = html.unescape(without_tags)
    without_trailer = _TRAILER_RE.sub("", unescaped)
    return _WHITESPACE_RE.sub(" ", without_trailer).strip()


def _strip_kind_prefix(raw_id: str) -> str:
    """`t3_1wobb41` → `1wobb41`, `t1_pblj859` → `pblj859`."""
    return raw_id.split("_", 1)[-1] if "_" in raw_id else raw_id


def _strip_user_prefix(name: str) -> str:
    return name[len("/u/"):] if name.startswith("/u/") else name


def _parse_updated(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(value)


def _entries(xml_text: str) -> list[ET.Element]:
    root = ET.fromstring(xml_text)
    return root.findall(f"{ATOM_NS}entry")


def _entry_id(entry: ET.Element) -> str:
    return entry.findtext(f"{ATOM_NS}id") or ""


def _entry_author(entry: ET.Element) -> str:
    name = entry.findtext(f"{ATOM_NS}author/{ATOM_NS}name") or ""
    return _strip_user_prefix(name) or "[deleted]"


def _entry_permalink(entry: ET.Element) -> str:
    link = entry.find(f"{ATOM_NS}link")
    return link.get("href", "") if link is not None else ""


def _entry_body(entry: ET.Element) -> str:
    return _clean_body(entry.findtext(f"{ATOM_NS}content"))


def parse_rss_listing(xml_text: str, subreddit: str) -> list[Post]:
    """`/r/{sub}/new/.rss` — записи от новых к старым, один пост на запись."""
    posts: list[Post] = []
    for entry in _entries(xml_text):
        post_id = _strip_kind_prefix(_entry_id(entry))
        author = _entry_author(entry)
        posts.append(
            Post(
                thread_id=f"reddit:{post_id}",
                source_key=f"reddit:{subreddit}",
                url=_entry_permalink(entry),
                title=entry.findtext(f"{ATOM_NS}title") or "",
                body=_entry_body(entry),
                author=author,
                author_url=f"{BASE}/user/{author}",
                created_at=_parse_updated(entry.findtext(f"{ATOM_NS}updated")),
            )
        )
    return posts


def parse_rss_thread(xml_text: str, thread_id: str, max_messages: int) -> list[Message]:
    """`/comments/{id}/.rss` — первая запись всегда сам пост (`t3_…`), дальше —
    комментарии (`t1_…`) плоским списком: Atom не передаёт дерево ответов, так
    что все они честно ложатся на `depth=1` с `parent_id` поста (см. модуль
    `render_thread` — он и так рендерит плоский список)."""
    entries = _entries(xml_text)
    root_id = thread_id.split(":", 1)[-1]
    if entries:
        post_id = _strip_kind_prefix(_entry_id(entries[0])) or root_id
    else:
        post_id = root_id

    messages: list[Message] = []
    for i, entry in enumerate(entries):
        is_post = i == 0
        author = _entry_author(entry)
        if not is_post and author in _SKIPPED_COMMENT_AUTHORS:
            continue
        msg_id = _strip_kind_prefix(_entry_id(entry)) or (post_id if is_post else "")
        messages.append(
            Message(
                thread_id=thread_id,
                msg_id=msg_id,
                parent_id=None if is_post else post_id,
                author=author,
                body=_entry_body(entry),
                created_at=_parse_updated(entry.findtext(f"{ATOM_NS}updated")),
                depth=0 if is_post else 1,
            )
        )
    return messages[:max_messages]


class RedditSource:
    name = "reddit"

    def __init__(self, client: Any, cfg: RedditConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._failures: list[str] = []

    def collection_targets(self) -> int:
        """Сколько сабреддитов настроено — знаменатель для «упало всё»."""
        return len(self.cfg.subreddits)

    def collection_failures(self) -> list[str]:
        """Сабреддиты, сорвавшиеся в последнем `list_new_posts`.

        Одиночный сбой — это по-прежнему предупреждение (§10 спеки), но сбой
        всех сабреддитов сразу означает блокировку, а не «нового ничего нет»:
        пайплайн обязан уметь отличить одно от другого.
        """
        return list(self._failures)

    def list_new_posts(self, since: datetime) -> Iterator[Post]:
        # Счётчик — за этот вызов, а не за всё время жизни источника.
        # Тело генератора выполняется при первом next(), то есть ровно тогда,
        # когда вызывающий начинает обход — сбросить раньше нечего.
        self._failures = []
        for sub in self.cfg.subreddits:
            try:
                yield from self._list_subreddit(sub, since)
            except Exception as exc:  # один сабреддит не должен ломать прогон
                log.warning("сабреддит r/%s пропущен: %s", sub, exc)
                self._failures.append(f"r/{sub}: {str(exc)[:200]}")

    def _list_subreddit(self, sub: str, since: datetime) -> Iterator[Post]:
        # `.rss` не пагинирует — до `listing_limit` (потолок Reddit) записей
        # одним запросом, поэтому вместо цикла по страницам сразу разбираем
        # ленту и останавливаемся на первой записи старше `since`.
        url = f"{BASE}/r/{sub}/new/.rss?limit={self.cfg.listing_limit}"
        posts = parse_rss_listing(self.client.get_text(url), sub)
        for post in posts:
            if post.created_at < since:
                return
            yield post

    def fetch_thread(self, thread_id: str) -> list[Message]:
        # Сабреддит в `thread_id` не хранится — используем форму без него.
        # Проверено живым запросом: Reddit резолвит `/comments/{id}/.rss` без
        # `/r/{sub}` на тот же тред и отдаёт пост с комментариями (200, Atom).
        post_id = thread_id.split(":", 1)[1]
        url = f"{BASE}/comments/{post_id}/.rss?limit=100"
        return parse_rss_thread(
            self.client.get_text(url), thread_id, self.cfg.max_messages_per_thread
        )
