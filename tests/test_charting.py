"""
Tests for charting.py's price-history handling - two separate real
production bugs, each in its own section below:

  1. build_archive_snapshot()'s completeness check - the guard that
     refuses to archive a chart when the fetched price history doesn't
     actually reach through the day being archived. Bugs on both sides
     of this check:
       - Too loose (the ORIGINAL bug): Yahoo Finance sometimes hasn't
         published a trading day's finalized candle yet, and without
         any check, that silently got archived as a "complete" chart
         missing its most recent day (confirmed for real against a
         SNOW journal entry that was one day stale).
       - Too strict (the bug this section's tests were added for): the
         Shortlist page's Save button and Journal Session archive
         using as_of=today's literal date even for a pre-market/
         intraday session, not just an evening one - demanding TODAY's
         final candle before today has even closed is asking for
         something that can't exist yet, so every same-day archive
         attempted before the close came back None, and the Daily
         Report sent right after had no charts in it at all.

  2. warm_price_cache_for_symbol()'s NaN sanitization before writing to
     the price_cache jsonb column - see that section's own docstring.

Database/network/rendering are all faked out (get_connection/
get_chart_preferences/get_drawings/fetch_history/render_png/yf.Ticker)
so these run as plain unit tests.
"""
import math
from datetime import date, datetime

import pandas as pd

import charting


def _fake_history(dates):
    idx = pd.to_datetime(dates)
    return pd.DataFrame({
        "Open": [10.0] * len(idx), "High": [11.0] * len(idx), "Low": [9.0] * len(idx),
        "Close": [10.5] * len(idx), "Volume": [1000] * len(idx),
    }, index=idx)


def _patch_common(monkeypatch, history, expected_last_trading_day):
    monkeypatch.setattr(charting.database, "get_connection", lambda: object())
    monkeypatch.setattr(
        charting.database, "get_chart_preferences",
        lambda conn: {"ma_text": "", "ma_colors": {}, "ma_type": "SMA"})
    monkeypatch.setattr(charting.database, "get_drawings", lambda conn, symbol: [])
    monkeypatch.setattr(charting, "fetch_history", lambda *args, **kwargs: history)
    monkeypatch.setattr(charting.timeutil, "expected_last_trading_day", lambda: expected_last_trading_day)
    monkeypatch.setattr(charting, "render_png", lambda fig, **kwargs: b"fake-png-bytes")


def test_none_when_todays_close_is_missing_after_the_close(monkeypatch):
    """The ORIGINAL bug this check exists for: as_of is a day that
    should already be closed (== expected_last_trading_day()), but the
    fetched history still only reaches the day before - Yahoo hasn't
    published the final candle yet. Must refuse, not silently archive a
    one-day-stale chart."""
    history = _fake_history(["2026-08-10", "2026-08-11"])  # missing 8/12
    _patch_common(monkeypatch, history, expected_last_trading_day=date(2026, 8, 12))
    result = charting.build_archive_snapshot(
        "AAPL", datetime(2026, 8, 1), 100.0, "Entry", datetime(2026, 8, 12))
    assert result is None


def test_succeeds_for_an_already_closed_day_with_complete_data(monkeypatch):
    history = _fake_history(["2026-08-10", "2026-08-11", "2026-08-12"])
    _patch_common(monkeypatch, history, expected_last_trading_day=date(2026, 8, 12))
    result = charting.build_archive_snapshot(
        "AAPL", datetime(2026, 8, 1), 100.0, "Entry", datetime(2026, 8, 12))
    assert result == b"fake-png-bytes"


def test_succeeds_intraday_before_close_even_without_todays_candle(monkeypatch):
    """The bug this fix addresses: a same-day Save/Journal Session
    archive attempted BEFORE today's close (as_of is today, but
    expected_last_trading_day() still points to yesterday since today
    hasn't finished trading) used to demand a candle for today that
    can't possibly exist yet, silently returning None. Must succeed
    using whatever's actually available - here, through yesterday."""
    history = _fake_history(["2026-08-10", "2026-08-11"])  # today (8/12) hasn't closed
    _patch_common(monkeypatch, history, expected_last_trading_day=date(2026, 8, 11))
    result = charting.build_archive_snapshot(
        "AAPL", datetime(2026, 8, 1), 100.0, "Entry", datetime(2026, 8, 12))
    assert result == b"fake-png-bytes"


def test_none_for_a_past_as_of_date_with_incomplete_data(monkeypatch):
    """An as_of date in the past (e.g. re-archiving an older day) still
    demands completeness through that specific day - the relaxed check
    only applies when as_of is today AND today is still open."""
    history = _fake_history(["2026-08-05", "2026-08-06"])  # missing 8/7
    _patch_common(monkeypatch, history, expected_last_trading_day=date(2026, 8, 12))
    result = charting.build_archive_snapshot(
        "AAPL", datetime(2026, 8, 1), 100.0, "Entry", datetime(2026, 8, 7))
    assert result is None


def test_none_when_no_price_data_at_all(monkeypatch):
    _patch_common(monkeypatch, pd.DataFrame(), expected_last_trading_day=date(2026, 8, 12))
    result = charting.build_archive_snapshot(
        "BADTICKER", datetime(2026, 8, 1), 100.0, "Entry", datetime(2026, 8, 12))
    assert result is None


# --- warm_price_cache_for_symbol() NaN sanitization -------------------------
# A real production bug: warm_price_cache_for_symbol()'s own "today's bar
# must be complete" guard only checks the single MOST RECENT bar - but
# Yahoo Finance's 5-year history can have a stray NaN Open/High/Low/Close
# anywhere further back (a halted session, a delisting/relisting gap,
# some other historical data hole), confirmed for real against SCCO.
# psycopg2's Json() wrapper serializes with plain json.dumps(), which
# happily emits a bare `NaN` token for a float NaN - not valid JSON by
# the actual spec, so Postgres's own jsonb column rejected the whole
# INSERT (InvalidTextRepresentation: "Token 'NaN' is invalid") the moment
# a ticker's history happened to contain one bad historical bar, with no
# connection to that day's own data. Fixed by running the same
# _json_safe() the interactive chart's payload already needed (see that
# function's own docstring) over history_dict before it's saved.

class _FakeTicker:
    def __init__(self, history_df):
        self._history_df = history_df

    def history(self, period=None, interval=None):
        return self._history_df


def _patch_warm_cache_common(monkeypatch, history_df, expected_last_trading_day, saved_calls):
    monkeypatch.setattr(charting.yf, "Ticker", lambda symbol: _FakeTicker(history_df))
    monkeypatch.setattr(charting.timeutil, "expected_last_trading_day", lambda: expected_last_trading_day)
    monkeypatch.setattr(charting.database, "get_connection", lambda: object())
    monkeypatch.setattr(
        charting.database, "save_cached_price_history",
        lambda conn, symbol, history_dict, fetched_for_date: saved_calls.append(history_dict))


def test_warm_price_cache_sanitizes_a_nan_bar_earlier_in_history(monkeypatch):
    """The most recent bar is complete (passes the existing guard), but
    an OLDER bar has a NaN Close - must still cache successfully, with
    that NaN replaced by None rather than reaching json.dumps() as a
    raw float NaN."""
    history = pd.DataFrame({
        "Open": [10.0, 11.0], "High": [10.5, 11.5], "Low": [9.5, 10.5],
        "Close": [math.nan, 11.2], "Volume": [1000, 1200],
    }, index=pd.to_datetime(["2026-08-10", "2026-08-11"]))
    saved_calls = []
    _patch_warm_cache_common(monkeypatch, history, date(2026, 8, 11), saved_calls)

    result = charting.warm_price_cache_for_symbol("SCCO")

    assert result == "ok"
    assert len(saved_calls) == 1
    assert saved_calls[0]["close"] == [None, 11.2]


def test_warm_price_cache_not_ready_when_the_last_bar_itself_is_incomplete(monkeypatch):
    """Unchanged existing behavior: a NaN in the MOST RECENT bar still
    means "not ready yet," not something to sanitize and cache."""
    history = pd.DataFrame({
        "Open": [10.0, 11.0], "High": [10.5, 11.5], "Low": [9.5, 10.5],
        "Close": [10.2, float("nan")], "Volume": [1000, 1200],
    }, index=pd.to_datetime(["2026-08-10", "2026-08-11"]))
    saved_calls = []
    _patch_warm_cache_common(monkeypatch, history, date(2026, 8, 11), saved_calls)

    result = charting.warm_price_cache_for_symbol("SCCO")

    assert result == "not_ready"
    assert saved_calls == []
