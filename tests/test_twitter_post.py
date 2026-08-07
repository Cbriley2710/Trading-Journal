"""
Tests for twitter_post.py's pure truncate_caption() logic - no network,
no real X credentials needed. post_tweet() itself isn't unit-tested
(same reasoning this codebase already applies to email-sending: no
live credentials to test against) - covered by manual verification
once real credentials exist, and by the fact that a missing-secret
RuntimeError already comes back as a plain (False, message) rather
than raising (see twitter_post.post_tweet()'s own docstring).
"""
import twitter_post


def test_truncate_caption_under_limit_unchanged():
    text = "Short caption under the limit."
    assert twitter_post.truncate_caption(text) == text


def test_truncate_caption_strips_surrounding_whitespace():
    assert twitter_post.truncate_caption("  short  ") == "short"


def test_truncate_caption_exact_limit_unchanged():
    text = "x" * 280
    assert twitter_post.truncate_caption(text) == text


def test_truncate_caption_over_limit_cuts_at_word_boundary():
    text = "word " * 100  # way over 280 chars
    result = twitter_post.truncate_caption(text)
    assert len(result) <= 280
    assert result.endswith("…")
    # Cut cleanly at a word boundary - no partial word right before the ellipsis.
    assert not result[:-1].endswith(" ")
    assert result[:-1].strip().endswith("word")


def test_truncate_caption_over_limit_no_spaces_hard_cuts():
    text = "x" * 281  # one char over, nothing to break on
    result = twitter_post.truncate_caption(text)
    assert len(result) == 280
    assert result.endswith("…")


def test_truncate_caption_custom_limit():
    text = "one two three four five"
    result = twitter_post.truncate_caption(text, limit=10)
    assert len(result) <= 10
    assert result.endswith("…")


def test_build_caption_appends_cashtag():
    assert twitter_post.build_caption("Broke out of the range", "NVDA") == "Broke out of the range $NVDA"


def test_build_caption_with_empty_notes():
    assert twitter_post.build_caption("", "NVDA") == "$NVDA"


def test_build_caption_cashtag_always_survives_truncation():
    text = "word " * 100  # way over the limit on its own
    result = twitter_post.build_caption(text, "NVDA", limit=50)
    assert len(result) <= 50
    assert result.endswith("$NVDA")
