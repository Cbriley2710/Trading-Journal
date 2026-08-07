"""
Tests for timeutil.expected_last_trading_day() - specifically the
early-morning rollback logic added after a real production bug: the
nightly archiving job runs ~midnight-to-1am Eastern, which on a plain
weekday had already rolled over into a brand new calendar day that
hadn't traded yet, so the job archived (and emailed the Daily Report
for) a day with no real close instead of the trading day that just
finished.
"""
from datetime import date, datetime

import timeutil


def _set_now(monkeypatch, when):
    monkeypatch.setattr(timeutil, "now_eastern", lambda: when)


def test_weekday_afternoon_returns_the_same_day(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 5, 15, 0))  # Wednesday 3pm
    assert timeutil.expected_last_trading_day() == date(2026, 8, 5)


def test_weekday_evening_returns_the_same_day(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 5, 22, 0))  # Wednesday 10pm
    assert timeutil.expected_last_trading_day() == date(2026, 8, 5)


def test_early_friday_morning_rolls_back_to_thursday(monkeypatch):
    # The exact real-world case that caused the production bug: the
    # nightly job fires ~1am Eastern during EDT.
    _set_now(monkeypatch, datetime(2026, 8, 7, 1, 0))  # Friday 1am
    assert timeutil.expected_last_trading_day() == date(2026, 8, 6)


def test_early_saturday_morning_rolls_back_to_friday(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 8, 1, 0))  # Saturday 1am
    assert timeutil.expected_last_trading_day() == date(2026, 8, 7)


def test_early_monday_morning_rolls_back_through_the_weekend_to_friday(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 10, 1, 0))  # Monday 1am
    assert timeutil.expected_last_trading_day() == date(2026, 8, 7)


def test_saturday_afternoon_returns_friday(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 8, 15, 0))  # Saturday 3pm
    assert timeutil.expected_last_trading_day() == date(2026, 8, 7)


def test_sunday_afternoon_returns_friday(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 9, 15, 0))  # Sunday 3pm
    assert timeutil.expected_last_trading_day() == date(2026, 8, 7)


def test_exactly_at_market_open_returns_the_same_day(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 5, 9, 30))  # Wednesday 9:30am
    assert timeutil.expected_last_trading_day() == date(2026, 8, 5)


def test_one_minute_before_market_open_rolls_back(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 5, 9, 29))  # Wednesday 9:29am
    assert timeutil.expected_last_trading_day() == date(2026, 8, 4)


# --- today_market_has_closed() ---
# Added after a real production bug: GitHub Actions' scheduling delay
# landed the nightly archiving job at 10:48am ET (well after market
# open) one day, and it archived every tracked ticker's chart using
# that morning's still-forming candle as if it were the day's final
# one - expected_last_trading_day() had already resolved to "today" by
# then (correctly, per that function's own job), but nothing was
# checking whether today's SESSION had actually finished yet.

def test_mid_morning_market_has_not_closed(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 7, 10, 48))  # Friday 10:48am - the real incident
    assert timeutil.today_market_has_closed() is False


def test_just_before_close_has_not_closed(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 7, 15, 59))  # Friday 3:59pm
    assert timeutil.today_market_has_closed() is False


def test_right_at_close_has_not_closed_yet_within_buffer(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 7, 16, 0))  # Friday 4:00pm - inside the 30min buffer
    assert timeutil.today_market_has_closed() is False


def test_after_the_close_buffer_has_closed(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 7, 16, 30))  # Friday 4:30pm
    assert timeutil.today_market_has_closed() is True


def test_late_evening_has_closed(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 7, 22, 0))  # Friday 10pm
    assert timeutil.today_market_has_closed() is True


def test_weekend_never_has_closed(monkeypatch):
    _set_now(monkeypatch, datetime(2026, 8, 8, 20, 0))  # Saturday 8pm
    assert timeutil.today_market_has_closed() is False
