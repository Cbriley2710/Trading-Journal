"""
Screener Reconciliation
=====================
Fills in, for every logged signal, whether its trigger was hit inside
the 3-session window and what the ruleset's own exit would have
produced - return, R multiple, bars held, exit reason. This is genuinely
NEW logic, not a refactor of anything in daily_screen.py: that script's
own CLI output only ever PRINTS the exit rule as a line of help text -

    "Exit: 2 consecutive closes below 21 EMA x (1 - 0.25 x ADR%),
     or a decline > 3 x ADR% intraday."

- it was never actually simulated in code. Below is that simulation,
written from the same stated rule, plus the ORIGINAL stop price itself
(also never simulated, only sized) as the third, most fundamental exit.

THREE EXIT CONDITIONS, checked in this order for every session after
the trigger fills (never the fill day itself - see reconcile_signal()'s
own note on why):

  1. initial_stop         that day's LOW touches the signal's own fixed
                           stop price (the primary, risk-sized stop -
                           "initial stop as shown" in the CLI's text).
  2. volatility_decline   a single-session decline from the prior
                           close of more than 3x the signal's OWN adr%
                           (fixed at signal time, same adr the stop
                           price itself was built from - see this
                           function's own note on why NOT a constantly
                           re-measured adr).
  3. ma_trend_exit        TWO CONSECUTIVE closes below that day's own
                           rolling 21 EMA x (1 - 0.25 x that day's own
                           rolling adr%) - unlike #2, this one IS meant
                           to track the stock's current character, since
                           an EMA-based trend exit is inherently a
                           trailing/updating rule.

Whichever fires first, chronologically, ends the simulated trade.
Never triggering within the window, or triggering but not yet hitting
any exit in the price history available so far, are both legitimate,
non-final outcomes - see reconcile_signal()'s `resolved` flag.
"""
from dataclasses import dataclass
from datetime import date as date_type

import pandas as pd

import database
from screener.data import _cached_fetch_one, to_price_frame
from screener.engine import indicators
import timeutil


@dataclass
class ReconciliationOutcome:
    triggered: bool
    resolved: bool                      # True => write reconciled_at now
    trigger_date: date_type | None = None
    outcome_return: float | None = None  # % , (exit - trigger) / trigger * 100
    outcome_r: float | None = None       # multiples of the original $ risk/share
    outcome_bars: int | None = None      # trading sessions held, trigger to exit
    exit_reason: str | None = None       # "initial_stop" | "volatility_decline" | "ma_trend_exit"


def _find_trigger(price_history, signal_date, trigger, expires):
    """
    Looks for the first session AFTER `signal_date`, up to and
    including `expires` (the 3-session trigger window), whose High
    reaches `trigger`. Returns that session's date, or None if the
    whole window is covered by `price_history` and none of it
    qualified, or "unknown" (the literal string) if `price_history`
    doesn't yet reach `expires` - not enough time has passed to know.
    """
    window = price_history[
        (price_history.date > pd.Timestamp(signal_date)) &
        (price_history.date <= pd.Timestamp(expires))
    ].sort_values("date")
    hits = window[window.high >= trigger]
    if len(hits):
        return hits.iloc[0].date
    if price_history.date.max() < pd.Timestamp(expires):
        return "unknown"
    return None


def reconcile_signal(ticker, signal_date, trigger, stop, adr, expires, price_history):
    """
    `price_history` is this ONE ticker's own daily OHLCV (ticker,date,
    open,high,low,close,volume - the same shape screener.data.
    fetch_universe() produces), ideally covering well before
    `signal_date` (indicators() needs real warmup for its own rolling
    21 EMA/ADR%) through however much is available since.

    Returns a ReconciliationOutcome. `resolved=False` means "don't have
    enough data yet to know" - the caller should leave that signal's DB
    row untouched and try again on a later run, NOT write a partial/
    wrong answer. `resolved=True` covers both a completed trade (an
    exit fired) and "never triggered, and the window's fully elapsed" -
    both are final answers worth recording.
    """
    trigger_date = _find_trigger(price_history, signal_date, trigger, expires)

    if trigger_date is None:
        return ReconciliationOutcome(triggered=False, resolved=True)
    if trigger_date == "unknown":
        return ReconciliationOutcome(triggered=False, resolved=False)

    enriched = indicators(price_history)
    enriched = enriched.sort_values("date").reset_index(drop=True)

    # Exit checks start the SESSION AFTER the fill, not the fill day
    # itself - daily bars can't tell us whether the fill happened
    # before or after that same day's low, so treating the fill day as
    # "clean" is the simplifying assumption (documented here since nothing
    # in the reference script ever had to make this call).
    after_fill = enriched[enriched.date > pd.Timestamp(trigger_date)]

    consecutive_below_trend = 0
    for _, day in after_fill.iterrows():
        if pd.isna(day.close):
            continue

        if day.low <= stop:
            return _finish(trigger, stop, trigger_date, day.date, stop,
                            after_fill, "initial_stop")

        prior_close_rows = enriched[enriched.date < day.date]
        if prior_close_rows.empty:
            continue
        prior_close = prior_close_rows.iloc[-1].close
        decline_pct = (prior_close - day.low) / prior_close * 100
        if decline_pct > 3 * adr:
            exit_price = round(prior_close * (1 - 3 * adr / 100), 2)
            return _finish(trigger, stop, trigger_date, day.date, exit_price,
                            after_fill, "volatility_decline")

        if pd.notna(day.ema21) and pd.notna(day.adr):
            threshold = day.ema21 * (1 - 0.25 * day.adr / 100)
            below_trend = day.close < threshold
        else:
            below_trend = False
        consecutive_below_trend = consecutive_below_trend + 1 if below_trend else 0
        if consecutive_below_trend >= 2:
            return _finish(trigger, stop, trigger_date, day.date, day.close,
                            after_fill, "ma_trend_exit")

    # Triggered, but no exit condition has fired in the data available
    # so far - a genuinely open position as of this reconciliation run,
    # not an error. Left unresolved for a later run to pick back up.
    return ReconciliationOutcome(triggered=True, resolved=False, trigger_date=trigger_date)


def _finish(trigger, stop, trigger_date, exit_date, exit_price, after_fill, reason):
    outcome_return = (exit_price - trigger) / trigger * 100
    risk_per_share = trigger - stop
    outcome_r = (exit_price - trigger) / risk_per_share if risk_per_share else None
    bars_held = int((after_fill.date <= exit_date).sum())
    return ReconciliationOutcome(
        triggered=True, resolved=True, trigger_date=trigger_date,
        outcome_return=round(outcome_return, 2),
        outcome_r=round(outcome_r, 2) if outcome_r is not None else None,
        outcome_bars=bars_held, exit_reason=reason,
    )


def reconcile_all_pending(conn):
    """
    The reconciliation job: pulls every signal_log row screener.reconcile
    hasn't finished with yet (database.get_unreconciled_signals()),
    refetches each affected ticker's own price history ONCE (grouped -
    a ticker with several still-open signals doesn't get fetched
    multiple times), and writes back whatever reconcile_signal() could
    now determine. Meant to run periodically (e.g. from the same nightly
    job that already warms the price cache, or a button on the History
    tab) - safe to call as often as you like, since every write is
    idempotent (a signal already fully reconciled never shows up in
    get_unreconciled_signals() again).

    Returns {"resolved": N, "still_pending": N, "fetch_failed": [tickers]}
    - `fetch_failed` covers a ticker that's since been delisted or
      otherwise can't be refetched; those signals are simply left alone
      (not an error to raise) since they'll just get retried the next
      time this runs.
    """
    pending = database.get_unreconciled_signals(conn)
    if not pending:
        return {"resolved": 0, "still_pending": 0, "fetch_failed": []}

    by_ticker = {}
    for signal in pending:
        by_ticker.setdefault(signal["ticker"], []).append(signal)

    session_date = timeutil.expected_last_trading_day()
    resolved_count = 0
    still_pending_count = 0
    fetch_failed = []

    for ticker, signals in by_ticker.items():
        raw_history = _cached_fetch_one(ticker, session_date)
        if raw_history is None:
            fetch_failed.append(ticker)
            still_pending_count += len(signals)
            continue
        price_history = to_price_frame(ticker, raw_history)

        for signal in signals:
            outcome = reconcile_signal(
                ticker, signal["signal_date"], signal["trigger_price"],
                signal["stop_price"], signal["adr"], signal["expires"], price_history,
            )
            if outcome.trigger_date is None and not outcome.resolved:
                still_pending_count += 1
                continue
            database.update_signal_reconciliation(
                conn, signal["signal_date"], ticker,
                triggered=outcome.triggered, trigger_date=outcome.trigger_date,
                outcome_return=outcome.outcome_return, outcome_r=outcome.outcome_r,
                outcome_bars=outcome.outcome_bars, exit_reason=outcome.exit_reason,
                mark_reconciled=outcome.resolved,
            )
            if outcome.resolved:
                resolved_count += 1
            else:
                still_pending_count += 1

    return {"resolved": resolved_count, "still_pending": still_pending_count,
            "fetch_failed": fetch_failed}
