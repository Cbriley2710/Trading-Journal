"""
Tests for monthly_review.py's month math and "what still needs a
review" check - plain dicts, no database or email involved.
"""
from datetime import date, datetime

import monthly_review


def _trade(exit_day, symbol="AAPL"):
    # Same dict shape database.get_trades() returns.
    return {
        "symbol": symbol, "direction": "LONG", "entry_date": datetime(2026, 9, 1), "date": exit_day,
        "quantity": 10, "buy_price": 100.0, "sell_price": 110.0, "profit_loss": 100.0,
    }


def _reviewed_key(trade):
    return (
        trade["symbol"], trade["entry_date"].date(), trade["date"].date(), trade["direction"],
        10.0, 100.0, 110.0)


def test_previous_month_start_normal_and_january():
    assert monthly_review.previous_month_start(date(2026, 11, 1)) == date(2026, 10, 1)
    assert monthly_review.previous_month_start(date(2026, 11, 30)) == date(2026, 10, 1)
    assert monthly_review.previous_month_start(date(2027, 1, 1)) == date(2026, 12, 1)


def test_only_previous_months_exits_count():
    trades = [_trade(datetime(2026, 9, 30)), _trade(datetime(2026, 10, 15)), _trade(datetime(2026, 11, 2))]
    month_start, month_trades, unreviewed = monthly_review.pending_for_previous_month(
        trades, set(), set(), today=date(2026, 11, 1))
    assert month_start == date(2026, 10, 1)
    assert len(month_trades) == 1 and len(unreviewed) == 1


def test_reviewed_trades_are_not_pending():
    oct_trade = _trade(datetime(2026, 10, 15))
    other = _trade(datetime(2026, 10, 20), symbol="MSFT")
    result = monthly_review.pending_for_previous_month(
        [oct_trade, other], {_reviewed_key(oct_trade)}, set(), today=date(2026, 11, 1))
    _, month_trades, unreviewed = result
    assert len(month_trades) == 2 and unreviewed == [other]


def test_returns_none_when_all_reviewed_or_no_trades():
    oct_trade = _trade(datetime(2026, 10, 15))
    assert monthly_review.pending_for_previous_month(
        [oct_trade], {_reviewed_key(oct_trade)}, set(), today=date(2026, 11, 1)) is None
    assert monthly_review.pending_for_previous_month([], set(), set(), today=date(2026, 11, 1)) is None


def test_old_review_without_prices_matches_loosely():
    oct_trade = _trade(datetime(2026, 10, 15))
    loose = {("AAPL", date(2026, 9, 1), date(2026, 10, 15), "LONG")}
    assert monthly_review.pending_for_previous_month(
        [oct_trade], set(), loose, today=date(2026, 11, 1)) is None
