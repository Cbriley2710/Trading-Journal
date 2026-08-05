"""
Tests for goals.py's two Process-goal metric functions (see that
module's own docstring) and status_zone() - all pure-function tests
against hand-built context dicts, no real database needed.
"""
from datetime import date, datetime

import pytest

import goals

# August 2026, for real: Aug 1 = Saturday, Aug 2 = Sunday, Aug 3-6 =
# Mon-Thu, Aug 7 = Friday - fixed reference dates so the Friday/
# Saturday exclusion can be checked against known, not re-derived, days.


def test_daily_journal_pct_excludes_friday_and_saturday():
    # window["end"] is always "today" (see resolve_window()'s "Monthly"
    # case) - here that's Wed 8/5, so the countable days are 8/1
    # through 8/4: Sat 8/1 is excluded (weekday), 8/5 itself is
    # excluded (today, see the dedicated test below), leaving
    # Sun/Mon/Tue (8/2-8/4) as the 3 countable days.
    window = {"kind": "range", "start": date(2026, 8, 1), "end": date(2026, 8, 5)}
    context = {"journaled_dates": {date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4)}}
    result = goals._daily_journal_pct(window, context)
    assert result == 100.0


def test_daily_journal_pct_ignores_a_journal_entry_on_an_excluded_day():
    """Journaling ON a Friday/Saturday (e.g. catching up early) must not
    inflate the numerator - those days never enter the denominator
    either, so they should have zero effect on the result."""
    window = {"kind": "range", "start": date(2026, 8, 1), "end": date(2026, 8, 5)}
    context = {"journaled_dates": {
        date(2026, 8, 1),  # Saturday - excluded day, journaled anyway
        date(2026, 8, 2), date(2026, 8, 3),  # only 2 of the 3 countable days
    }}
    result = goals._daily_journal_pct(window, context)
    assert result == pytest.approx(2 / 3 * 100)


def test_daily_journal_pct_excludes_today_even_if_already_journaled():
    """Today (window["end"]) never enters the denominator, even if it's
    already been journaled early - the % only ever reflects fully
    completed days, per the user's own rule (tonight's entry isn't due
    until tonight)."""
    window = {"kind": "range", "start": date(2026, 8, 1), "end": date(2026, 8, 5)}
    context = {"journaled_dates": {date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)}}
    result = goals._daily_journal_pct(window, context)
    assert result == 100.0  # not counted as a 4th day out of some larger denominator


def test_daily_journal_pct_none_when_no_countable_days():
    """A window whose only completed day is a Friday/Saturday (e.g. it's
    the 2nd of the month and the 1st was a Saturday) has nothing to
    count yet - None, not a divide-by-zero."""
    window = {"kind": "range", "start": date(2026, 8, 1), "end": date(2026, 8, 2)}  # today = Sunday 8/2
    context = {"journaled_dates": set()}
    assert goals._daily_journal_pct(window, context) is None


def test_daily_journal_pct_full_month_all_journaled():
    # Spans two weekends - today = Wed 8/12, so countable days are
    # 8/2-8/6 and 8/9-8/11 (8 days total), all journaled.
    window = {"kind": "range", "start": date(2026, 8, 1), "end": date(2026, 8, 12)}
    context = {"journaled_dates": {
        date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6),
        date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 11),
    }}
    assert goals._daily_journal_pct(window, context) == 100.0


def _trade(symbol, entry_date, exit_date, direction="LONG", quantity=100, buy_price=10.0, sell_price=11.0):
    return {
        "symbol": symbol, "direction": direction, "quantity": quantity,
        "buy_price": buy_price, "sell_price": sell_price,
        "entry_date": datetime.combine(entry_date, datetime.min.time()),
        "date": datetime.combine(exit_date, datetime.min.time()),
    }


def _review(trade, report_created_at, reflections_notes="Some reflections"):
    return {
        "symbol": trade["symbol"], "entry_date": trade["entry_date"].date(),
        "exit_date": trade["date"].date(), "direction": trade["direction"],
        "quantity": trade["quantity"], "buy_price": trade["buy_price"], "sell_price": trade["sell_price"],
        "report_created_at": report_created_at, "reflections_notes": reflections_notes,
    }


def test_month_is_credited_true_when_fully_reviewed_before_deadline():
    trade = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    context = {"trades": [trade], "reviewed_trade_keys": [_review(trade, datetime(2026, 8, 15))]}
    # July's deadline is end of August - today (9/15) is past it, but
    # that shouldn't matter for a month that's already credited.
    result = goals._month_is_credited(date(2026, 7, 1), context, today=date(2026, 9, 15))
    assert result is True


def test_month_is_credited_false_when_never_reviewed_past_deadline():
    trade = _trade("AAPL", date(2026, 6, 5), date(2026, 6, 10))
    context = {"trades": [trade], "reviewed_trade_keys": []}
    result = goals._month_is_credited(date(2026, 6, 1), context, today=date(2026, 9, 15))
    assert result is False


def test_month_is_credited_pending_when_deadline_not_yet_passed():
    """Not yet reviewed, but the following month isn't over yet - not a
    failure, just not decided."""
    trade = _trade("AAPL", date(2026, 8, 5), date(2026, 8, 10))
    context = {"trades": [trade], "reviewed_trade_keys": []}
    # August's deadline is end of September - today (9/15) hasn't reached it.
    result = goals._month_is_credited(date(2026, 8, 1), context, today=date(2026, 9, 15))
    assert result is None


def test_month_is_credited_none_when_no_trades_that_month():
    context = {"trades": [], "reviewed_trade_keys": []}
    result = goals._month_is_credited(date(2026, 5, 1), context, today=date(2026, 9, 15))
    assert result is None


def test_month_is_credited_false_for_partial_coverage_past_deadline():
    """Only one of two trades from the month got reviewed - not fully
    done, and the deadline has passed, so this is a real miss."""
    trade1 = _trade("AAPL", date(2026, 4, 5), date(2026, 4, 10))
    trade2 = _trade("MSFT", date(2026, 4, 6), date(2026, 4, 12))
    context = {"trades": [trade1, trade2], "reviewed_trade_keys": [_review(trade1, datetime(2026, 5, 10))]}
    result = goals._month_is_credited(date(2026, 4, 1), context, today=date(2026, 9, 15))
    assert result is False


def test_month_is_credited_false_when_reflections_missing():
    """Reviewed, but the report's reflections were never filled in -
    doesn't count as "fully finished" even past the deadline."""
    trade = _trade("AAPL", date(2026, 6, 5), date(2026, 6, 10))
    context = {"trades": [trade], "reviewed_trade_keys": [_review(trade, datetime(2026, 7, 5), reflections_notes="")]}
    result = goals._month_is_credited(date(2026, 6, 1), context, today=date(2026, 9, 15))
    assert result is False


def test_month_is_credited_false_when_review_completed_after_deadline():
    """Reviewed with reflections, but not until AFTER the deadline -
    per the user's own rule, that's permanently not credited even
    though the work eventually got done."""
    trade = _trade("AAPL", date(2026, 6, 5), date(2026, 6, 10))
    # June's deadline is end of July; this review was created in August, too late.
    context = {"trades": [trade], "reviewed_trade_keys": [_review(trade, datetime(2026, 8, 5))]}
    result = goals._month_is_credited(date(2026, 6, 1), context, today=date(2026, 9, 15))
    assert result is False


def test_monthly_review_pct_averages_only_decided_months(monkeypatch):
    """End-to-end through _monthly_review_pct(): April (miss, partial),
    June (miss, never reviewed), July (credited) are all past their
    deadline and get averaged; August (pending) and every trade-less
    month are left out entirely."""
    monkeypatch.setattr(goals.timeutil, "today_eastern", lambda: date(2026, 9, 15))

    april1 = _trade("AAPL", date(2026, 4, 5), date(2026, 4, 10))
    april2 = _trade("MSFT", date(2026, 4, 6), date(2026, 4, 12))
    june = _trade("TSLA", date(2026, 6, 5), date(2026, 6, 10))
    july1 = _trade("NVDA", date(2026, 7, 5), date(2026, 7, 10))
    july2 = _trade("AMD", date(2026, 7, 6), date(2026, 7, 12))
    august = _trade("SNOW", date(2026, 8, 5), date(2026, 8, 10))

    context = {
        "trades": [april1, april2, june, july1, july2, august],
        "reviewed_trade_keys": [
            _review(april1, datetime(2026, 5, 10)),  # april2 never reviewed -> April = False
            _review(july1, datetime(2026, 8, 15)),
            _review(july2, datetime(2026, 8, 20)),  # both July trades reviewed in time -> True
            # june: never reviewed -> False; august: never reviewed but deadline not passed -> pending
        ],
    }
    window = goals.resolve_window("Yearly")
    result = goals._monthly_review_pct(window, context)
    assert result == pytest.approx(1 / 3 * 100)


def test_stop_loss_coverage_pct_partial():
    context = {
        "open_positions": [{"symbol": "AAPL"}, {"symbol": "TSLA"}, {"symbol": "MSFT"}],
        "stop_losses": {"AAPL": 150.0, "TSLA": 200.0},  # MSFT has none
    }
    assert goals._stop_loss_coverage_pct(None, context) == pytest.approx(2 / 3 * 100)


def test_stop_loss_coverage_pct_full_coverage():
    context = {"open_positions": [{"symbol": "AAPL"}], "stop_losses": {"AAPL": 150.0}}
    assert goals._stop_loss_coverage_pct(None, context) == 100.0


def test_stop_loss_coverage_pct_zero_coverage():
    context = {"open_positions": [{"symbol": "AAPL"}], "stop_losses": {}}
    assert goals._stop_loss_coverage_pct(None, context) == 0.0


def test_stop_loss_coverage_pct_none_when_no_open_positions():
    context = {"open_positions": [], "stop_losses": {}}
    assert goals._stop_loss_coverage_pct(None, context) is None


def _stop_event(symbol, set_at):
    return {"symbol": symbol, "stop_loss": 100.0, "set_at": datetime.combine(set_at, datetime.min.time())}


def test_trade_had_stop_loss_true_when_event_during_trade():
    trade = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    events = [_stop_event("AAPL", date(2026, 7, 7))]
    assert goals._trade_had_stop_loss(trade, events) is True


def test_trade_had_stop_loss_false_when_event_outside_trade_window():
    trade = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    events = [_stop_event("AAPL", date(2026, 7, 15))]  # set after the trade already closed
    assert goals._trade_had_stop_loss(trade, events) is False


def test_trade_had_stop_loss_false_for_different_symbol():
    trade = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    events = [_stop_event("MSFT", date(2026, 7, 7))]
    assert goals._trade_had_stop_loss(trade, events) is False


def test_trade_had_stop_loss_true_on_boundary_dates():
    """A stop set exactly on the entry or exit day still counts."""
    trade = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    assert goals._trade_had_stop_loss(trade, [_stop_event("AAPL", date(2026, 7, 5))]) is True
    assert goals._trade_had_stop_loss(trade, [_stop_event("AAPL", date(2026, 7, 10))]) is True


def test_stop_loss_usage_pct_basic():
    protected = _trade("AAPL", date(2026, 7, 5), date(2026, 7, 10))
    unprotected = _trade("MSFT", date(2026, 7, 6), date(2026, 7, 12))
    context = {
        "trades": [protected, unprotected],
        "stop_loss_events": [_stop_event("AAPL", date(2026, 7, 7))],
    }
    window = {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}
    assert goals._stop_loss_usage_pct(window, context) == 50.0


def test_stop_loss_usage_pct_none_when_no_trades_in_window():
    context = {"trades": [], "stop_loss_events": []}
    window = {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}
    assert goals._stop_loss_usage_pct(window, context) is None


def test_equity_growth_pct_basic(monkeypatch):
    monkeypatch.setattr(goals.database, "get_realized_pl_since", lambda conn, start: 5000.0)
    context = {"jan1_balance": 100000.0, "conn": None}
    window = {"start": date(2026, 1, 1), "end": date(2026, 8, 5)}
    assert goals._equity_growth_pct(window, context) == 5.0


def test_equity_growth_pct_none_without_jan1_balance():
    context = {"jan1_balance": None, "conn": None}
    window = {"start": date(2026, 1, 1), "end": date(2026, 8, 5)}
    assert goals._equity_growth_pct(window, context) is None


def test_equity_growth_vs_spy_excess_return(monkeypatch):
    monkeypatch.setattr(goals.database, "get_realized_pl_since", lambda conn, start: 5000.0)
    context = {
        "jan1_balance": 100000.0, "conn": None,
        "fetch_period_return_pct": lambda symbol, start: 3.0 if symbol == "SPY" else None,
    }
    window = {"start": date(2026, 1, 1), "end": date(2026, 8, 5)}
    # Account grew 5% (5000/100000), SPY grew 3% over the same period -> beating it by 2 points.
    assert goals._equity_growth_vs_spy(window, context) == pytest.approx(2.0)


def test_equity_growth_vs_qqq_none_when_benchmark_unavailable(monkeypatch):
    """A failed/rate-limited price fetch for the benchmark (see
    charting.fetch_period_return_pct()'s own None case) must not crash
    or silently show 0 - the whole goal is unknown until that data's
    available."""
    monkeypatch.setattr(goals.database, "get_realized_pl_since", lambda conn, start: 5000.0)
    context = {
        "jan1_balance": 100000.0, "conn": None,
        "fetch_period_return_pct": lambda symbol, start: None,
    }
    window = {"start": date(2026, 1, 1), "end": date(2026, 8, 5)}
    assert goals._equity_growth_vs_qqq(window, context) is None


def test_equity_growth_vs_benchmark_none_without_jan1_balance():
    context = {
        "jan1_balance": None, "conn": None,
        "fetch_period_return_pct": lambda symbol, start: 3.0,
    }
    window = {"start": date(2026, 1, 1), "end": date(2026, 8, 5)}
    assert goals._equity_growth_vs_spy(window, context) is None


def test_status_zone_higher_is_better():
    assert goals.status_zone(90, warning_level=80, alert_level=60, direction="higher_is_better") == "Good"
    assert goals.status_zone(70, warning_level=80, alert_level=60, direction="higher_is_better") == "Warning"
    assert goals.status_zone(50, warning_level=80, alert_level=60, direction="higher_is_better") == "Alert"
    # Boundaries: exactly at a threshold counts as meeting it (not below it).
    assert goals.status_zone(80, warning_level=80, alert_level=60, direction="higher_is_better") == "Good"
    assert goals.status_zone(60, warning_level=80, alert_level=60, direction="higher_is_better") == "Warning"


def test_status_zone_lower_is_better():
    assert goals.status_zone(10, warning_level=20, alert_level=40, direction="lower_is_better") == "Good"
    assert goals.status_zone(30, warning_level=20, alert_level=40, direction="lower_is_better") == "Warning"
    assert goals.status_zone(50, warning_level=20, alert_level=40, direction="lower_is_better") == "Alert"


def test_status_zone_none_when_thresholds_unset():
    assert goals.status_zone(90, warning_level=None, alert_level=None, direction="higher_is_better") is None
    assert goals.status_zone(None, warning_level=80, alert_level=60, direction="higher_is_better") is None
