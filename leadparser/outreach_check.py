"""Автопроверка `out check`: подсказка «сходи посмотри», а не уведомление.

Reddit отдаёт комментарии плоским списком без родителя (см. `parse_rss_thread`
в sources/reddit.py) — отличить «ответили именно вам» от «кто-то написал в
треде» программно нельзя, и личные сообщения не проверить вовсе. Обе эти
границы явно перечислены в `DISCLAIMER`, и это же держат в уме все проверки
ниже: они сообщают факты (виден ли наш комментарий, писал ли автор треда
после отправки), а не выдают эти факты за «вам ответили».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .net.blanktrail import BlankTrailError
from .outreach import OutreachStore, parse_reddit_url
from .outreach_dashboard import record_movement

DISCLAIMER = (
    "Лента комментариев Reddit плоская — у записи нет родителя, поэтому "
    "отличить «ответили вам» от «кто-то написал в треде» автоматически нельзя. "
    "Это подсказка «сходи посмотри своими глазами», а не уведомление об ответе. "
    "Личные сообщения Reddit не отдаёт вовсе — строки с каналом dm ниже не проверялись."
)


@dataclass
class RowCheckResult:
    row_id: int
    subreddit: str | None
    url: str
    entries_now: int | None = None
    entries_before: int | None = None
    visible: bool | None = None  # None — не проверяли (ошибка сети/непарсибл url)
    newly_hidden: bool = False   # именно НОВОСТЬ, а не «до сих пор скрыт» — см. run_check
    author_posted_after_send: bool = False
    error: str | None = None
    unparseable: bool = False


@dataclass
class CheckResult:
    rows: list[RowCheckResult]
    skipped_dm: int


def run_check(
    store: OutreachStore, reddit_source, reddit_username: str, now: datetime,
) -> CheckResult:
    """Прогоняет автопроверку по всем активным откликам с каналом `comment`.

    `reddit_source` — что угодно с методом `.fetch_thread(thread_id) ->
    list[Message]` (в проде — sources.reddit.RedditSource поверх того же
    BlankTrail+ThrottledClient, что и `run`; в тестах — поддельный клиент).
    Одна сорвавшаяся строка не должна останавливать проверку остальных — тот
    же принцип устойчивости, что у RedditSource.list_new_posts.
    """
    candidates = store.rows_to_check()
    comment_rows = [r for r in candidates if r["channel"] == "comment"]
    skipped_dm = sum(1 for r in candidates if r["channel"] != "comment")

    results: list[RowCheckResult] = []
    for row in comment_rows:
        _, thread_id = parse_reddit_url(row["url"])
        if thread_id is None:
            results.append(RowCheckResult(
                row_id=row["id"], subreddit=row["subreddit"], url=row["url"],
                unparseable=True,
            ))
            continue
        try:
            messages = reddit_source.fetch_thread(thread_id)
        except BlankTrailError as exc:
            results.append(RowCheckResult(
                row_id=row["id"], subreddit=row["subreddit"], url=row["url"], error=str(exc),
            ))
            continue

        entries_now = len(messages)
        entries_before = row["last_seen_entries"]
        visible = any(m.author == reddit_username for m in messages)
        post_author = messages[0].author if messages else None
        sent_at = datetime.fromisoformat(row["sent_at"])
        author_posted_after_send = any(
            m.author == post_author and m.created_at > sent_at for m in messages[1:]
        )

        # "Громко" сигналим только о НОВОСТИ: либо это первая проверка после
        # отправки и комментария сразу не видно, либо на прошлой проверке он
        # ещё был виден, а теперь пропал. Если он не находился и в прошлый раз —
        # это уже известный факт, шуметь о нём заново незачем (брифа: "говорим
        # об этом громко" — не то же самое, что "повторяем на каждой проверке").
        first_check = row["last_seen_entries"] is None
        was_visible_before = row["comment_visible"]
        newly_hidden = (not visible) and (first_check or was_visible_before == 1)

        results.append(RowCheckResult(
            row_id=row["id"], subreddit=row["subreddit"], url=row["url"],
            entries_now=entries_now, entries_before=entries_before,
            visible=visible, newly_hidden=newly_hidden,
            author_posted_after_send=author_posted_after_send,
        ))
        store.record_check(
            row["id"], checked_at=now.isoformat(), entries=entries_now, visible=visible
        )
        # Дашборд не ходит в Reddit сам (см. leadparser/outreach_dashboard.py) —
        # "было" теряется в ту же секунду, когда record_check выше перезапишет
        # last_seen_entries, поэтому сравнение снимаем именно здесь и кладём в
        # отдельную таблицу, а не полагаемся на то, что кто-то потом её восстановит.
        grew = entries_before is not None and entries_now > entries_before
        record_movement(
            store.conn, row["id"], checked_at=now.isoformat(), grew=grew,
            entries_before=entries_before, entries_now=entries_now,
        )

    return CheckResult(rows=results, skipped_dm=skipped_dm)
