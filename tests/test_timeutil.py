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
