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
    close for: today, unless today is a Saturday or Sunday, in which
    case it's Friday. A simple weekday-only approximation (it doesn't
    know about market holidays), matching this project's existing
    "good enough, not a full trading calendar" approach elsewhere.

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
    """
    today = today_eastern()
    if today.weekday() == 5:  # Saturday
        return today - timedelta(days=1)
    if today.weekday() == 6:  # Sunday
        return today - timedelta(days=2)
    return today
