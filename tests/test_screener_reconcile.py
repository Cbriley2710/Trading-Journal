"""
Tests for screener/reconcile.py - genuinely new logic (see that
module's own docstring), so these are hand-built deterministic price
paths designed to isolate ONE exit condition at a time, not a
characterization of some prior behavior.

Every scenario shares a 120-day flat, low-volatility warmup (so
indicators() has real data to compute a stable 21 EMA/ADR% from before
the interesting part starts) followed by a hand-controlled tail.
"""
import numpy as np
import pandas as pd
import pytest

from screener.reconcile import reconcile_signal

WARMUP_DAYS = 120
BASE_PRICE = 100.0


def _warmup(start="2024-01-02"):
    dates = pd.bdate_range(start, periods=WARMUP_DAYS)
    close = np.full(WARMUP_DAYS, BASE_PRICE)
    return pd.DataFrame(dict(
        ticker="X", date=dates, open=close, high=close * 1.01,
        low=close * 0.99, close=close, volume=np.full(WARMUP_DAYS, 1_000_000),
    ))


def _append_days(warmup, rows):
    """`rows` is a list of dicts with open/high/low/close (volume
    defaults to 1,000,000) - dates continue as business days right
    after the warmup's last date."""
    last_date = warmup.date.iloc[-1]
    extra_dates = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=len(rows))
    extra = pd.DataFrame([
        dict(ticker="X", date=d, volume=r.get("volume", 1_000_000),
             open=r["open"], high=r["high"], low=r["low"], close=r["close"])
        for d, r in zip(extra_dates, rows)
    ])
    return pd.concat([warmup, extra], ignore_index=True), extra_dates


def test_never_triggered_within_window():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    trigger, stop, adr = 105.0, 98.0, 3.0
    # 3 sessions, none reaching the trigger (all highs stay under 105).
    rows = [dict(open=100, high=101, low=99, close=100)] * 3
    history, extra_dates = _append_days(warmup, rows)
    expires = extra_dates[-1]

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, expires, history)
    assert outcome.resolved is True
    assert outcome.triggered is False
    assert outcome.trigger_date is None


def test_window_not_yet_elapsed_is_unresolved():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    trigger, stop, adr = 105.0, 98.0, 3.0
    # Only 1 session of data past signal_date, but expires is 3 out -
    # not enough data yet to know if it triggers on day 2 or 3.
    rows = [dict(open=100, high=101, low=99, close=100)]
    history, extra_dates = _append_days(warmup, rows)
    fake_expires = extra_dates[-1] + pd.Timedelta(days=5)

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, fake_expires, history)
    assert outcome.resolved is False
    assert outcome.triggered is False


def test_triggered_then_hits_initial_stop():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    trigger, stop, adr = 105.0, 98.0, 3.0
    rows = [
        dict(open=104, high=106, low=103, close=105.5),   # day 1: triggers (high >= 105)
        dict(open=104, high=105, low=100, close=101),      # day 2: drifts down, still above stop
        dict(open=100, high=101, low=97, close=98.5),       # day 3: low breaches stop (98 <= 98)
        dict(open=98, high=99, low=95, close=96),            # would be another decline if not exited
    ]
    history, extra_dates = _append_days(warmup, rows)
    expires = extra_dates[2]

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, expires, history)
    assert outcome.resolved is True
    assert outcome.triggered is True
    assert outcome.trigger_date == extra_dates[0]
    assert outcome.exit_reason == "initial_stop"
    # Exit fill is the stop price itself.
    expected_return = (stop - trigger) / trigger * 100
    assert outcome.outcome_return == pytest.approx(round(expected_return, 2))
    assert outcome.outcome_r == pytest.approx(-1.0, abs=0.01)  # exits exactly at 1R loss
    assert outcome.outcome_bars == 2  # day 2 and day 3 after the fill


def test_triggered_then_volatility_decline_without_touching_stop():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    # A wide stop (well below any single day's low here) isolates the
    # volatility-decline condition from the initial-stop condition -
    # both could otherwise fire on the same day.
    trigger, stop, adr = 105.0, 80.0, 3.0
    rows = [
        dict(open=104, high=106, low=103, close=105.5),      # day 1: triggers
        dict(open=105, high=106, low=104, close=105.5),      # day 2: quiet, no exit
        # day 3: low is a >9% decline from day 2's close (105.5) -
        # comfortably past 3 x adr(3%) = 9%, but low=95 is still well
        # above stop=80.
        dict(open=105, high=105, low=95, close=97),
    ]
    history, extra_dates = _append_days(warmup, rows)
    expires = extra_dates[0]

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, expires, history)
    assert outcome.resolved is True
    assert outcome.triggered is True
    assert outcome.exit_reason == "volatility_decline"
    assert outcome.outcome_bars == 2


def test_triggered_then_ma_trend_exit():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    # Stop and volatility threshold both set far away so neither can
    # fire - isolates the two-consecutive-closes-below-trend condition.
    trigger, stop, adr = 105.0, 50.0, 1.0
    rows = [dict(open=104, high=106, low=103, close=105.5)]  # day 1: triggers
    # A long, gentle grind down - well within any single day's
    # tolerance for the other two conditions, but eventually closes
    # below the (slower-moving) 21 EMA's buffer two days running.
    drift = np.linspace(105, 90, 40)
    for i, price in enumerate(drift):
        rows.append(dict(open=price + 0.3, high=price + 0.5, low=price - 0.5, close=price))
    history, extra_dates = _append_days(warmup, rows)
    expires = extra_dates[0]

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, expires, history)
    assert outcome.resolved is True
    assert outcome.triggered is True
    assert outcome.exit_reason == "ma_trend_exit"
    assert outcome.outcome_return < 0  # a trend exit here should be a modest loss, not a stop-out


def test_triggered_but_still_open_stays_unresolved():
    warmup = _warmup()
    signal_date = warmup.date.iloc[-1]
    trigger, stop, adr = 105.0, 90.0, 3.0
    # Triggers, then just sits quietly - no exit condition anywhere
    # close to firing in the data available.
    rows = [dict(open=104, high=106, low=103, close=105.5)]
    rows += [dict(open=105.5, high=106, low=105, close=105.5)] * 10
    history, extra_dates = _append_days(warmup, rows)
    expires = extra_dates[0]

    outcome = reconcile_signal("X", signal_date, trigger, stop, adr, expires, history)
    assert outcome.resolved is False
    assert outcome.triggered is True
    assert outcome.trigger_date == extra_dates[0]
    assert outcome.exit_reason is None
