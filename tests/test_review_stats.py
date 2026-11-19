"""Tests for analyze_trades.review_session_stats() - the numbers behind
the rolling "last 6 Review Sessions" table."""
from datetime import date

import pytest

from analyze_trades import review_session_stats


def _review(buy, sell, qty, entry, exit_, direction="LONG"):
    return {
        "symbol": "X", "direction": direction, "buy_price": buy, "sell_price": sell, "quantity": qty,
        "profit_loss": (sell - buy) * qty, "entry_date": entry, "exit_date": exit_,
    }


REVIEWS = [
    _review(10.0, 12.0, 100, date(2026, 1, 1), date(2026, 1, 11)),   # win: +200, held 10 days, cost 1,000
    _review(20.0, 19.0, 100, date(2026, 1, 5), date(2026, 1, 7)),    # loss: -100, held 2 days, cost 2,000
]


def test_stats_for_a_two_trade_session():
    stats = review_session_stats(REVIEWS, [10_000.0, 20_000.0], portfolio_value=30_000.0)
    assert stats["trades"] == 2 and stats["batting_avg"] == 50.0
    assert stats["total_pl"] == 100.0 and stats["expectancy"] == 50.0
    assert stats["profit_factor"] == pytest.approx(2.0)            # 200 won / 100 lost
    assert stats["avg_position_pct"] == pytest.approx((10.0 + 10.0) / 2)   # 1,000/10,000 and 2,000/20,000
    assert stats["equity_change_pct"] == pytest.approx(100 / 30_000 * 100)  # total P/L over the whole portfolio
    assert stats["avg_days_win"] == 10 and stats["avg_days_loss"] == 2
    # the one losing trade: -100 on a 20,000 account
    assert stats["largest_loss_dollar"] == -100.0
    assert stats["largest_loss_pct"] == pytest.approx(-0.5)


def test_largest_loss_picks_the_biggest_dollar_loser_and_none_without_losses():
    reviews = [
        _review(10.0, 9.0, 100, date(2026, 1, 1), date(2026, 1, 2)),    # -100 on 1,000 account
        _review(10.0, 8.0, 100, date(2026, 1, 1), date(2026, 1, 2)),    # -200 on 50,000 account
    ]
    stats = review_session_stats(reviews, [1_000.0, 50_000.0])
    assert stats["largest_loss_dollar"] == -200.0 and stats["largest_loss_pct"] == pytest.approx(-0.4)
    assert review_session_stats([REVIEWS[0]], [10_000.0])["largest_loss_dollar"] is None


def test_missing_balances_give_none_not_zero():
    stats = review_session_stats(REVIEWS, [None, None], portfolio_value=None)
    assert stats["avg_position_pct"] is None and stats["equity_change_pct"] is None


def test_no_losses_means_no_profit_factor_and_empty_session_is_none():
    winners = [REVIEWS[0]]
    assert review_session_stats(winners, [10_000.0])["profit_factor"] is None
    assert review_session_stats([], []) is None
