"""
Timeutil
=====================
A trading journal should agree with you about what day/time it is -
but the server this app runs on (Streamlit Community Cloud, and the
separate GitHub Actions runner nightly_archive.py runs on) has no
particular reason to be set to your timezone. Both default to UTC,
which is several hours ahead of US Eastern - enough that in the
evening the server can already think it's "tomorrow" while it's still
today for anyone trading US markets. That's what was making journal
entries, "already generated today?" checks, and saved timestamps (like
when a ticker was added to a watchlist) land under the wrong date.

Since this app tracks US stocks, everything here uses US Eastern (not
your computer's own timezone, which Streamlit has no reliable way to
know anyway, and not a fixed UTC-5 offset, which would be wrong about
8 months of the year during Eastern Daylight Time) - the standard
library's zoneinfo handles the EST/EDT switch automatically.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def now_eastern():
    """
    The current date/time in US Eastern, as a plain (timezone-naive)
    datetime - a drop-in replacement for datetime.now() everywhere in
    this project. Naive on purpose: every date/time this app already
    stores (in a plain TIMESTAMP/DATE database column) or compares
    against is itself a naive value, so this only changes WHICH clock
    the value is read from, not the type of value code elsewhere gets
    back.
    """
    return datetime.now(EASTERN).replace(tzinfo=None)


def today_eastern():
    """Today's date in US Eastern - see now_eastern()."""
    return now_eastern().date()


def expected_last_trading_day():
    """
    The most recent date the market should already have a completed
    close for. A simple weekday-only approximation (it doesn't know
    about market holidays), matching this project's existing "good
    enough, not a full trading calendar" approach elsewhere.

    Used to tag and check the persistent price cache (see charting.py's
    warm_price_cache_for_symbol()/_daily_history_from_cache() and
    database.clear_stale_price_cache(), called from nightly_archive.py)
    by the TRADING DAY the cached data actually covers, rather than by
    whatever calendar day happens to be "today" the instant a scheduled
    job executes. That distinction matters because GitHub Actions
    doesn't fire scheduled jobs at a precise time - a run meant for
    right after Friday's close has actually landed after midnight
    Eastern (i.e. already Saturday) here. Tagging with today_eastern()
    in that case would mark Friday's real closing data as "fetched for
    Saturday," which then never matches "today" again on any later
    day - making a nightly cleanup step delete perfectly good data, and
    the cache serve nothing but live (rate-limit-prone) fetches from
    that point on. Tagging with the trading day instead means the same
    cached row keeps matching this same function's own answer all
    weekend, and still correctly expires once Monday's real trading
    day arrives.

    Also rolls back a calendar day BEFORE the weekend check whenever
    it's still early morning (before the market could plausibly have
    opened, let alone closed, today) - this is what makes
    nightly_archive.py's ~midnight-to-1am GitHub Actions run correctly
    target the trading day that just finished, rather than the brand
    new calendar day it's already rolled over into by the time it
    executes. A real bug found in production: that job archives each
    ticker's chart under whatever day THIS function returns, and
    without this adjustment it was archiving under a day that hadn't
    traded yet (Yahoo Finance had no close for it at all), while the
    actual just-finished trading day's own Logbook entry never got
    revisited/fixed by this nightly "safety net" run at all.
    """
    now = now_eastern()
    reference_date = now.date()
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        reference_date -= timedelta(days=1)
    if reference_date.weekday() == 5:  # Saturday
        return reference_date - timedelta(days=1)
    if reference_date.weekday() == 6:  # Sunday
        return reference_date - timedelta(days=2)
    return reference_date


def today_market_has_closed():
    """
    True once today's regular trading session has closed - False on a
    weekend (nothing closes "today" specifically) or any time before
    4:30pm Eastern on a weekday. The 30-minute buffer past the literal
    4:00pm close matches this project's other post-close jobs (see
    warm_price_cache.py's own ~4:30pm ET schedule) - Yahoo Finance's
    own data isn't necessarily finalized the instant the closing bell
    rings.

    NOT the same question expected_last_trading_day() answers (that
    one seriously means "which trading day should already have SOME
    completed close, treating an in-progress today as not it yet" -
    used for cache freshness, where a live in-progress "today" IS
    valid/wanted during market hours). This one exists specifically
    for nightly_archive.py's automatic Logbook archiving: even once
    expected_last_trading_day() resolves to today (any time from
    9:30am on), Yahoo Finance already has A row for today the whole
    time the market's open - live-updating, not finalized - so the
    existing "does the data reach the target day" completeness check
    in charting.build_archive_snapshot() can't tell "today, still
    trading" apart from "today, actually closed" on its own. A real
    bug found in production: GitHub Actions' scheduling delay landed
    this job at 10:48am ET one day, well after 9:30am, so it archived
    every tracked ticker's chart using that morning's still-forming
    candle as if it were the day's final one - hours before the
    session was actually over.
    """
    now = now_eastern()
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (16, 30)
