# tests/test_reddit_source.py
"""Reddit закрыл анонимный JSON — источник переведён на `.rss` (Atom).

Все тесты разбора работают на настоящих фикстурах (`tests/fixtures/reddit_rss_*.xml`,
сохранённые ответы Reddit), а не на выдуманных структурах.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from leadparser.config import RedditConfig
from leadparser.sources.facebook import FacebookSource
from leadparser.sources.reddit import RedditSource, parse_rss_listing, parse_rss_thread

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


LISTING_XML = load("reddit_rss_listing.xml")
THREAD_XML = load("reddit_rss_thread.xml")


def test_parse_rss_listing_maps_fields():
    posts = parse_rss_listing(LISTING_XML, "n8n")
    assert [p.thread_id for p in posts] == [
        "reddit:1wobb41", "reddit:1wo3ded", "reddit:1wo22tw", "reddit:1wnyops",
    ]
    first = posts[0]
    assert first.source_key == "reddit:n8n"
    assert first.title == "Form submission tab does not pop up"
    assert first.author == "redditor_5"
    assert first.author_url == "https://www.reddit.com/user/redditor_5"
    assert first.url == (
        "https://www.reddit.com/r/n8n/comments/1wobb41/form_submission_tab_does_not_pop_up/"
    )
    assert first.created_at == datetime(2026, 9, 23, 16, 47, 2, tzinfo=timezone.utc)
    assert "I am a totally new user on n8n" in first.body


def test_parse_rss_listing_defaults_fields_rss_does_not_carry():
    """`num_comments`, `flair`, `stickied` в RSS нет — значения по умолчанию,
    `removed` — всегда `False` (RSS не размечает удалённые посты)."""
    first = parse_rss_listing(LISTING_XML, "n8n")[0]
    assert first.flair is None
    assert first.num_comments == 0
    assert first.stickied is False
    assert first.removed is False
    assert first.raw_json == {}


def test_parse_rss_listing_strips_tags_and_unescapes_entities():
    """B: `content` — это готовый HTML Reddit'а с двойным экранированием
    (`&amp;#39;` → `&#39;` на разборе XML → `'` через `html.unescape`)."""
    posts = parse_rss_listing(LISTING_XML, "n8n")
    body = posts[3].body  # redditor_2 — короткий пост без таблицы/картинки
    assert "<" not in body and ">" not in body
    assert "&#39;" not in body and "&amp;" not in body and "&quot;" not in body
    assert "you've built" in body
    assert "today's weather" in body


def test_parse_rss_listing_strips_submitted_by_trailer():
    """Каждая запись оканчивается навигационным блоком Reddit'а
    (`submitted by /u/NAME [link] [comments]`) — это не текст поста, а
    дублирует уже известного автора. Без вырезания он либо ест бюджет
    промпта, либо — у поста без selftext — становится всем телом целиком,
    и `render_batch`'s `body or '(no body)'` никогда не сработает."""
    for post in parse_rss_listing(LISTING_XML, "n8n"):
        lowered = post.body.lower()
        assert "submitted by" not in lowered
        assert "[link]" not in lowered
        assert "[comments]" not in lowered


def test_parse_rss_listing_link_only_post_has_empty_body():
    """Пост-ссылка без selftext: карточка Reddit'а — картинка + 'submitted by
    ... [link] [comments]', ни единого слова настоящего текста. Пустое тело —
    честный сигнал для `render_batch`'s `body or '(no body)'`, а не
    правдоподобный на вид мусор."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        '<author><name>/u/ann</name></author>'
        '<content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;'
        '&lt;a href=&quot;https://www.reddit.com/r/n8n/comments/xyz999/cool_tool/&quot;&gt;'
        '&lt;img src=&quot;https://preview.redd.it/abc.png&quot; alt=&quot;Cool tool&quot; /&gt;'
        '&lt;/a&gt;&lt;/td&gt;&lt;td&gt; &amp;#32;submitted by&amp;#32;'
        '&lt;a href=&quot;https://www.reddit.com/user/ann&quot;&gt; /u/ann &lt;/a&gt;&lt;br/&gt;'
        '&lt;span&gt;&lt;a href=&quot;https://example.com/cool-tool&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt;'
        '&amp;#32;&lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/n8n/comments/xyz999/cool_tool/&quot;&gt;'
        '[comments]&lt;/a&gt;&lt;/span&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>'
        '<id>t3_xyz999</id>'
        '<link href="https://www.reddit.com/r/n8n/comments/xyz999/cool_tool/" />'
        '<updated>2026-09-23T10:00:00+00:00</updated>'
        '<title>Cool tool</title></entry></feed>'
    )
    posts = parse_rss_listing(xml, "n8n")
    assert posts[0].body == ""


def test_parse_rss_thread_flattens_post_and_comments():
    """Первая запись — сам пост (`t3_…`), остальные — комментарии (`t1_…`),
    плоским списком: RSS не передаёт дерево ответов.

    AutoModerator в списке комментариев нет: `heuristic.stop_authors` уже
    считает его шумом на уровне постов, а его канонический комментарий из
    настоящей фикстуры не должен есть место в промпте с лимитом символов."""
    msgs = parse_rss_thread(THREAD_XML, "reddit:1wobb41", max_messages=100)
    assert [m.msg_id for m in msgs] == [
        "1wobb41", "pblkywu", "pblzkjc", "pbm5knc", "pbmdlpb",
    ]
    post = msgs[0]
    assert post.depth == 0
    assert post.parent_id is None
    assert post.author == "redditor_5"
    assert post.created_at == datetime(2026, 9, 23, 16, 47, 2, tzinfo=timezone.utc)

    comments = msgs[1:]
    assert [c.author for c in comments] == [
        "redditor_4", "redditor_1", "redditor_7", "redditor_3",
    ]
    assert "AutoModerator" not in [c.author for c in comments]
    assert all(c.depth == 1 for c in comments)
    assert all(c.parent_id == "1wobb41" for c in comments)


def test_parse_rss_thread_post_entry_strips_submitted_by_trailer():
    """Первая запись треда — та же карточка поста, что и в листинге, с тем же
    навигационным хвостом Reddit'а — и его тоже нужно срезать."""
    post = parse_rss_thread(THREAD_XML, "reddit:1wobb41", max_messages=100)[0]
    lowered = post.body.lower()
    assert "submitted by" not in lowered
    assert "[link]" not in lowered
    assert "[comments]" not in lowered


def test_parse_rss_thread_decodes_comment_body():
    msgs = parse_rss_thread(THREAD_XML, "reddit:1wobb41", max_messages=100)
    bp041 = msgs[2]  # post, redditor_4, redditor_1 — AutoModerator отфильтрован
    assert bp041.author == "redditor_1"
    assert "<" not in bp041.body and "&#39;" not in bp041.body
    assert "browser's popup blocker" in bp041.body
    assert '"Testing" URL' in bp041.body
    assert "localhost:5678" in bp041.body  # текст из <code> остаётся, тег снят


def test_parse_rss_thread_respects_max_messages():
    """AutoModerator не в счёт: лимит считает только то, что реально попадёт
    в промпт, а не отфильтрованный шум."""
    msgs = parse_rss_thread(THREAD_XML, "reddit:1wobb41", max_messages=2)
    assert [m.msg_id for m in msgs] == ["1wobb41", "pblkywu"]


class FakeClient:
    def __init__(self, responses: dict):
        self.responses = responses
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        for fragment, payload in self.responses.items():
            if fragment in url:
                return payload
        raise AssertionError(f"неожиданный URL: {url}")


def test_list_new_posts_builds_listing_url_with_configured_limit():
    client = FakeClient({"/r/n8n/new/.rss": LISTING_XML})
    cfg = RedditConfig(subreddits=["n8n"], listing_limit=4)
    posts = list(RedditSource(client, cfg).list_new_posts(
        datetime(2000, 1, 1, tzinfo=timezone.utc)
    ))
    assert len(posts) == 4
    assert len(client.urls) == 1
    assert client.urls[0] == "https://www.reddit.com/r/n8n/new/.rss?limit=4"


def test_list_new_posts_stops_at_since():
    """Записи идут от новых к старым — выходим из сабреддита на первой,
    что старше `since`, вместо того чтобы разбирать всю ленту."""
    client = FakeClient({"/r/n8n/new/.rss": LISTING_XML})
    cfg = RedditConfig(subreddits=["n8n"])
    since = datetime(2026, 9, 23, 11, 0, 0, tzinfo=timezone.utc)
    posts = list(RedditSource(client, cfg).list_new_posts(since))
    assert [p.thread_id for p in posts] == ["reddit:1wobb41", "reddit:1wo3ded"]


def test_list_new_posts_survives_one_broken_subreddit():
    class PartlyBroken(FakeClient):
        def get_text(self, url: str) -> str:
            self.urls.append(url)
            if "/r/broken/" in url:
                raise RuntimeError("403")
            return LISTING_XML

    cfg = RedditConfig(subreddits=["broken", "n8n"])
    source = RedditSource(PartlyBroken({}), cfg)
    posts = list(source.list_new_posts(datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert [p.thread_id for p in posts] == [
        "reddit:1wobb41", "reddit:1wo3ded", "reddit:1wo22tw", "reddit:1wnyops",
    ]
    assert len(source.collection_failures()) == 1
    assert "broken" in source.collection_failures()[0]
    assert "403" in source.collection_failures()[0]


def test_collection_failures_reset_between_calls():
    class PartlyBroken(FakeClient):
        def get_text(self, url: str) -> str:
            self.urls.append(url)
            if "/r/broken/" in url:
                raise RuntimeError("403")
            return LISTING_XML

    cfg = RedditConfig(subreddits=["broken", "n8n"])
    source = RedditSource(PartlyBroken({}), cfg)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    list(source.list_new_posts(since))
    assert len(source.collection_failures()) == 1
    list(source.list_new_posts(since))
    assert len(source.collection_failures()) == 1


def test_list_new_posts_counts_failed_subreddits():
    """A1: полный провал сбора обязан быть виден, а не утонуть в warning'ах."""

    class AllBroken(FakeClient):
        def get_text(self, url: str) -> str:
            self.urls.append(url)
            raise RuntimeError("403 от reddit")

    cfg = RedditConfig(subreddits=["webdev", "n8n"])
    source = RedditSource(AllBroken({}), cfg)
    posts = list(source.list_new_posts(datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert posts == []
    assert source.collection_targets() == 2
    failures = source.collection_failures()
    assert len(failures) == 2
    assert "webdev" in failures[0] and "403" in failures[0]


def test_fetch_thread_builds_subredditless_url_and_parses():
    """Сабреддит в `thread_id` не хранится — используем форму без него
    (проверено живым запросом: `https://www.reddit.com/comments/{id}/.rss`)."""
    client = FakeClient({"/comments/1wobb41/.rss": THREAD_XML})
    cfg = RedditConfig(max_messages_per_thread=100)
    msgs = RedditSource(client, cfg).fetch_thread("reddit:1wobb41")
    assert [m.msg_id for m in msgs] == [
        "1wobb41", "pblkywu", "pblzkjc", "pbm5knc", "pbmdlpb",
    ]
    assert client.urls[0] == "https://www.reddit.com/comments/1wobb41/.rss?limit=100"
    assert "/r/" not in client.urls[0]


def test_fetch_thread_respects_max_messages_per_thread():
    client = FakeClient({"/comments/1wobb41/.rss": THREAD_XML})
    cfg = RedditConfig(max_messages_per_thread=3)
    msgs = RedditSource(client, cfg).fetch_thread("reddit:1wobb41")
    assert len(msgs) == 3


def test_facebook_source_is_a_stub():
    source = FacebookSource(client=None, groups=[])
    assert source.name == "facebook"
    with pytest.raises(NotImplementedError, match="этап 2"):
        list(source.list_new_posts(datetime.now(tz=timezone.utc)))
    with pytest.raises(NotImplementedError, match="этап 2"):
        source.fetch_thread("facebook:1")
