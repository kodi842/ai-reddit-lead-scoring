from __future__ import annotations

from ..config import HeuristicConfig
from ..models import Post
from ..storage import Storage


def skip_reason(post: Post, cfg: HeuristicConfig) -> str | None:
    """Очевидно нелидовые посты — отбрасываем до всякой LLM.

    Смотрим только на заголовок и флаер: стоп-фраза в теле поста ничего не значит,
    а короткий пост («Need a bot, DM me») вполне может быть лидом.
    """
    if post.stickied:
        return "stickied"
    if post.removed:
        return "removed"
    if post.author in cfg.stop_authors or post.author == "[deleted]":
        return "stop_author"
    haystack = f"{post.title} {post.flair or ''}".lower()
    if any(phrase.lower() in haystack for phrase in cfg.stop_phrases):
        return "stop_phrase"
    return None


def apply_heuristic(storage: Storage, cfg: HeuristicConfig, posts: list[Post]) -> int:
    skipped = 0
    for post in posts:
        reason = skip_reason(post, cfg)
        if reason:
            storage.set_skip_reason(post.thread_id, reason)
            skipped += 1
    return skipped
