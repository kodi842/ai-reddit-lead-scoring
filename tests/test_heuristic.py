from datetime import datetime, timezone

from leadparser.config import HeuristicConfig, load_config
from leadparser.models import Post
from leadparser.stages.heuristic import apply_heuristic, skip_reason
from leadparser.storage import Storage

CFG = HeuristicConfig(
    stop_phrases=["[for hire]", "for hire", "showcase", "self-promotion", "megathread"],
    stop_authors=["AutoModerator"],
)
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def post(**over) -> Post:
    base = dict(
        thread_id="reddit:a1", source_key="reddit:n8n", url="https://r/x",
        title="Need a scraper built", body="I need someone to build this, paying",
        author="bob", author_url="https://www.reddit.com/user/bob", created_at=NOW,
        flair=None, num_comments=1, stickied=False, removed=False, raw_json={},
    )
    base.update(over)
    return Post(**base)


def test_normal_post_is_not_skipped():
    assert skip_reason(post(), CFG) is None


def test_short_post_is_not_skipped():
    assert skip_reason(post(body="Need a bot, DM me", title="bot"), CFG) is None


def test_stickied_is_skipped():
    assert skip_reason(post(stickied=True), CFG) == "stickied"


def test_removed_is_skipped():
    assert skip_reason(post(removed=True), CFG) == "removed"


def test_stop_author_is_skipped():
    assert skip_reason(post(author="AutoModerator"), CFG) == "stop_author"


def test_stop_phrase_in_title_is_skipped():
    assert skip_reason(post(title="[For Hire] Python dev available"), CFG) == "stop_phrase"


def test_stop_phrase_in_flair_is_skipped():
    assert skip_reason(post(flair="Showcase"), CFG) == "stop_phrase"


def test_stop_phrase_is_case_insensitive():
    assert skip_reason(post(title="MEGATHREAD: september"), CFG) == "stop_phrase"


def test_stop_phrase_in_body_is_ignored():
    body = "I saw a showcase thread but I actually need someone to build my scraper"
    assert skip_reason(post(body=body), CFG) is None


def test_apply_heuristic_marks_only_matching_posts():
    store = Storage(":memory:")
    posts = [
        post(thread_id="reddit:a1"),
        post(thread_id="reddit:a2", stickied=True),
        post(thread_id="reddit:a3", author="AutoModerator"),
    ]
    for p in posts:
        store.upsert_post(p)
    assert apply_heuristic(store, CFG, posts) == 2
    pending = [p.thread_id for p in store.posts_without_stage("prefilter", "prefilter_v1")]
    assert pending == ["reddit:a1"]
    store.close()


def test_real_config_does_not_drop_buyer_for_hire_posts(config_path):
    """Verify config.yaml only stops the bracketed [for hire] tag, not bare phrase.

    Bare "for hire" would drop legitimate buyer posts like "Need a Python dev for hire"
    — a paying customer marked as stop_phrase before any model sees them.
    Bracketed [for hire] targets the Reddit freelancer convention (e.g. "[For Hire] dev").
    """
    cfg = load_config(config_path)

    # Real buyer post should NOT be skipped (bare phrase was problematic)
    buyer_post = post(title="Need a Python dev for hire, budget $500")
    assert skip_reason(buyer_post, cfg.heuristic) is None

    # Bracketed tag should still be skipped (Reddit convention for freelancers)
    freelancer_post = post(title="[For Hire] Python dev available")
    assert skip_reason(freelancer_post, cfg.heuristic) == "stop_phrase"
