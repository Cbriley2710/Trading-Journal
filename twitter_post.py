"""
Twitter Post
=====================
Posts a chart image + journal/review notes to the user's personal X
(Twitter) account - a "Tweet" checkbox on the Journal Session
(pages/2_Shortlist.py) and Trade Review Session (pages/1_Trade_
Analyzer.py) alongside their existing Save & Next flow, always behind
a read-only preview (see render_tweet_preview()) with an explicit
Post/Cancel confirmation - never posted silently as a side effect of
journaling.

Needs a Twitter/X Developer app with Read+Write access (OAuth 1.0a
user-context credentials, not just a bearer token - media upload still
requires this even under the v2 API). Four secrets, same get_secret()
lookup daily_report.py's email sending already uses:
TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN,
TWITTER_ACCESS_SECRET (see .streamlit/secrets.toml.example).

Main and friend are separate Streamlit Cloud deployments with entirely
separate secrets - this "just works" per-deployment once each person
adds their own keys (same pattern as REPORT_EMAIL_*), no code here
needs to know about the other deployment. A missing/invalid credential
comes back as a plain (False, message) - see post_tweet()'s own
docstring - so a deployment without X set up yet fails with a clear
toast instead of a crash.
"""

import io

import streamlit as st
import tweepy

from report_utils import get_secret

TWEET_CHAR_LIMIT = 280


def truncate_caption(text, limit=TWEET_CHAR_LIMIT):
    """
    The exact string that will actually be posted as the tweet's
    caption - `text` as-is if it already fits within `limit`,
    otherwise cut at the last whole word before the limit (so a tweet
    never ends mid-word) with "…" appended. Pure function, no network -
    the SAME truncation the read-only preview shows before posting, so
    what you approve in the preview is exactly what goes out.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    # Reserve one character for the "…" itself before cutting.
    cutoff = text[:limit - 1].rstrip()
    last_space = cutoff.rfind(" ")
    if last_space > 0:
        cutoff = cutoff[:last_space]
    return cutoff.rstrip() + "…"


def post_tweet(image_bytes, caption_text):
    """
    Posts one tweet: `image_bytes` (PNG, already rendered by
    charting.build_archive_snapshot()/build_trade_review_snapshot())
    as the attached media, `caption_text` (already truncated - see
    truncate_caption()) as the tweet text. Returns (success, message) -
    never raises, same contract daily_report.generate_and_send_report()
    already uses, so a caller can st.toast() the result either way
    without its own try/except.

    Media upload (tweepy.API, the older v1.1 client) needs OAuth 1.0a
    user-context credentials - a v2-only bearer token can post TEXT
    tweets but can't attach media, which is why this needs all four
    TWITTER_* secrets, not just an API key.
    """
    try:
        api_key = get_secret("TWITTER_API_KEY")
        api_secret = get_secret("TWITTER_API_SECRET")
        access_token = get_secret("TWITTER_ACCESS_TOKEN")
        access_secret = get_secret("TWITTER_ACCESS_SECRET")
    except RuntimeError as exc:
        return False, f"X posting isn't set up for this account: {exc}"

    try:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        v1_api = tweepy.API(auth)
        media = v1_api.media_upload(filename="chart.png", file=io.BytesIO(image_bytes))

        client = tweepy.Client(
            consumer_key=api_key, consumer_secret=api_secret,
            access_token=access_token, access_token_secret=access_secret,
        )
        client.create_tweet(text=caption_text, media_ids=[media.media_id])
    except Exception as exc:
        return False, f"Could not post to X: {exc}"

    return True, "Posted to X."


def render_tweet_preview(pending_tweet):
    """
    The read-only "here's what will be posted" screen - the chart image
    and the already-truncated caption (see truncate_caption(), applied
    BEFORE this ever gets called, so what's shown here is exactly what
    post_tweet() would send - no separate editing step, per the user's
    own choice of a read-only preview over an editable one). Shared by
    both the Journal Session and Trade Review Session interstitials,
    since the preview itself doesn't differ between them - only how
    each session builds `pending_tweet` and what happens after Post/
    Cancel differs, which stays in each page's own code.

    Returns "post", "cancel", or None (still deciding, neither button
    clicked yet this render) - callers act on the result and clear
    their own session_state["pending_tweet"].
    """
    st.subheader(f"Post {pending_tweet['symbol']} to X?")
    st.image(pending_tweet["image"])
    st.text_area(
        "Caption (read-only)", value=pending_tweet["caption"], height=100,
        disabled=True, key="_tweet_preview_caption",
    )
    st.caption(f"{len(pending_tweet['caption'])}/{TWEET_CHAR_LIMIT} characters")

    cols = st.columns([1, 1, 4])
    if cols[0].button("Post Tweet", type="primary"):
        return "post"
    if cols[1].button("Cancel"):
        return "cancel"
    return None
