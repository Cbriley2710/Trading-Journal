"""
Tests for charting.build_archive_snapshot()'s completeness check - the
guard that refuses to archive a chart when the fetched price history
doesn't actually reach through the day being archived. Real production
bugs on both sides of this check:

  - Too loose (the ORIGINAL bug): Yahoo Finance sometimes hasn't
    published a trading day's finalized candle yet, and without any
    check, that silently got archived as a "complete" chart missing
    its most recent day (confirmed for real against a SNOW journal
    entry that was one day stale).
  - Too strict (the bug this file's tests were added for): the
    Shortlist page's Save button and Journal Session archive using
    as_of=today's literal date even for a pre-market/intraday session,
    not just an evening one - demanding TODAY's final candle before
    today has even closed is asking for something that can't exist
    yet, so every same-day archive attempted before the close came
    back None, and the Daily Report sent right after had no charts in
    it at all.

Database/network/rendering are all faked out (get_connection/
get_chart_preferences/get_drawings/fetch_history/render_png) so these
run as plain unit tests - only the completeness-check branch itself
(and a real, un-mocked build_figure() call) is under test.
"""
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
