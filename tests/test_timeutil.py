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


# --- is_journaling_day() / last_journaling_day() ---
# Added after a real bug: render_journal_box() showed "yesterday's"
# journal entry using plain calendar-yesterday, so opening the app on a
# Sunday always showed "No entry" (calendar-yesterday is Saturday, a
# day the user never journals) even though a real entry existed from
# Thursday. Friday is ALSO excluded - the user writes up Friday's
# session on Sunday, under Sunday's own date, so Friday's date itself
# never has anything saved under it either.

def test_monday_through_thursday_are_journaling_days():
    for day in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)):  # Mon-Thu
        assert timeutil.is_journaling_day(day) is True


def test_friday_and_saturday_are_not_journaling_days():
    assert timeutil.is_journaling_day(date(2026, 8, 7)) is False  # Friday
    assert timeutil.is_journaling_day(date(2026, 8, 8)) is False  # Saturday


def test_sunday_is_a_journaling_day():
    # Friday's session gets written up here, under Sunday's own date.
    assert timeutil.is_journaling_day(date(2026, 8, 9)) is True


def test_last_journaling_day_on_sunday_skips_back_to_thursday():
    assert timeutil.last_journaling_day(date(2026, 8, 9)) == date(2026, 8, 6)  # Sun -> Thu


def test_last_journaling_day_on_monday_is_sunday():
    # Sunday already has an entry (Friday's session, written up then) -
    # unaffected by this fix, matches plain calendar-yesterday.
    assert timeutil.last_journaling_day(date(2026, 8, 10)) == date(2026, 8, 9)  # Mon -> Sun


def test_last_journaling_day_on_a_midweek_day_is_plain_yesterday():
    assert timeutil.last_journaling_day(date(2026, 8, 5)) == date(2026, 8, 4)  # Wed -> Tue


def test_last_journaling_day_on_saturday_skips_back_to_thursday():
    assert timeutil.last_journaling_day(date(2026, 8, 8)) == date(2026, 8, 6)  # Sat -> Thu
