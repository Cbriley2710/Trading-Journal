"""
Tests for twitter_post.py: truncate_caption()/build_caption() are pure
(no network, no real X credentials needed) - covered directly.
post_tweet() itself needs real X credentials to fully verify end to
end (that part is still manual, same reasoning this codebase already
applies to email-sending), but its ERROR-BRANCHING logic - which of
the three possible (False, message) results comes back for which
failure, and that a real success returns (True, ...) - is exactly the
kind of thing that regresses silently without a test, as the image-
upload-vs-tweet-text 403 split (added after a real production bug)
already showed. Covered here by monkeypatching get_secret() and every
tweepy call, so no real network call ever happens.
"""
from unittest.mock import MagicMock

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


def test_build_caption_prepends_cashtag():
    assert twitter_post.build_caption("Broke out of the range", "NVDA") == "$NVDA Broke out of the range"


def test_build_caption_with_empty_notes():
    assert twitter_post.build_caption("", "NVDA") == "$NVDA"


def test_build_caption_cashtag_always_survives_truncation():
    text = "word " * 100  # way over the limit on its own
    result = twitter_post.build_caption(text, "NVDA", limit=50)
    assert len(result) <= 50
    assert result.startswith("$NVDA")


FAKE_SECRETS = {
    "TWITTER_API_KEY": "key", "TWITTER_API_SECRET": "secret",
    "TWITTER_ACCESS_TOKEN": "token", "TWITTER_ACCESS_SECRET": "token_secret",
}


def _patch_secrets(monkeypatch, secrets=FAKE_SECRETS):
    monkeypatch.setattr(twitter_post, "get_secret", lambda key: secrets[key])


def test_post_tweet_missing_secret_returns_clear_message(monkeypatch):
    def _missing(key):
        raise RuntimeError(f"No {key} found.")
    monkeypatch.setattr(twitter_post, "get_secret", _missing)

    success, message = twitter_post.post_tweet(b"fake-png-bytes", "caption")

    assert success is False
    assert "X posting isn't set up for this account" in message


def test_post_tweet_image_upload_failure_reports_upload_step(monkeypatch):
    _patch_secrets(monkeypatch)
    monkeypatch.setattr(twitter_post.tweepy, "OAuth1UserHandler", MagicMock())
    fake_api = MagicMock()
    fake_api.media_upload.side_effect = Exception("403 Forbidden")
    monkeypatch.setattr(twitter_post.tweepy, "API", lambda auth: fake_api)

    success, message = twitter_post.post_tweet(b"fake-png-bytes", "caption")

    assert success is False
    assert message.startswith("Could not upload chart image to X:")
    assert "403 Forbidden" in message


def test_post_tweet_text_failure_reports_tweet_step_not_upload_step(monkeypatch):
    _patch_secrets(monkeypatch)
    monkeypatch.setattr(twitter_post.tweepy, "OAuth1UserHandler", MagicMock())
    fake_media = MagicMock(media_id="12345")
    fake_api = MagicMock()
    fake_api.media_upload.return_value = fake_media
    monkeypatch.setattr(twitter_post.tweepy, "API", lambda auth: fake_api)

    fake_client = MagicMock()
    fake_client.create_tweet.side_effect = Exception("403 Forbidden")
    monkeypatch.setattr(twitter_post.tweepy, "Client", lambda **kwargs: fake_client)

    success, message = twitter_post.post_tweet(b"fake-png-bytes", "caption")

    assert success is False
    assert message.startswith("Could not post tweet text to X:")
    assert "403 Forbidden" in message


def test_post_tweet_success(monkeypatch):
    _patch_secrets(monkeypatch)
    monkeypatch.setattr(twitter_post.tweepy, "OAuth1UserHandler", MagicMock())
    fake_media = MagicMock(media_id="12345")
    fake_api = MagicMock()
    fake_api.media_upload.return_value = fake_media
    monkeypatch.setattr(twitter_post.tweepy, "API", lambda auth: fake_api)

    fake_client = MagicMock()
    monkeypatch.setattr(twitter_post.tweepy, "Client", lambda **kwargs: fake_client)

    success, message = twitter_post.post_tweet(b"fake-png-bytes", "caption")

    assert (success, message) == (True, "Posted to X.")
    fake_client.create_tweet.assert_called_once_with(text="caption", media_ids=["12345"])
