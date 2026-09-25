"""
Tests for charting.open_position_returns() - the shared unrealized P/L
math behind get_calculated_account_value(), the Journal Session's live
fact tiles (pages/2_Shortlist.py's render_position_stats()), and the
Daily Report's per-position stats line (daily_report.py). Extracted so
those three could never independently drift apart - these tests cover
the pure math directly, no database or network needed.
"""
import charting


def test_long_position_gains_when_price_rises():
    position = {"avg_price": 100.0, "quantity": 50, "direction": "LONG"}

    returns = charting.open_position_returns(position, current_price=110.0)

    assert returns["unrealized_pl"] == 500.0  # (110-100) * 50
    assert returns["pct_change"] == 10.0
    assert returns["pct_of_account"] is None  # no account_value given


def test_short_position_gains_when_price_falls():
    # A short profits when price falls BELOW the average entry - the
    # opposite direction from a long position.
    position = {"avg_price": 100.0, "quantity": 50, "direction": "SHORT"}

    returns = charting.open_position_returns(position, current_price=90.0)

    assert returns["unrealized_pl"] == 500.0  # (100-90) * 50
    assert returns["pct_change"] == 10.0


def test_short_position_loses_when_price_rises():
    position = {"avg_price": 100.0, "quantity": 50, "direction": "SHORT"}

    returns = charting.open_position_returns(position, current_price=110.0)

    assert returns["unrealized_pl"] == -500.0
    assert returns["pct_change"] == -10.0


def test_pct_of_account_reflects_current_market_value_not_cost_basis():
    # $110 * 50 shares = $5,500 current value, against a $50,000 account.
    position = {"avg_price": 100.0, "quantity": 50, "direction": "LONG"}

    returns = charting.open_position_returns(position, current_price=110.0, account_value=50000.0)

    assert returns["pct_of_account"] == 11.0
