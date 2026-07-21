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

from datetime import datetime
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
