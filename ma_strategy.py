"""
MA Strategy
=====================
A discretionary trend-following exit rule this project can now track
for you: sell a LONG position once it's closed below its own N-day
moving average for a set number of days in a row (the mirror version
for a SHORT is closing back ABOVE the MA) - and, separately, once that
moving average has climbed far enough past your cost basis, trail your
stop-loss up to it instead of leaving your original fixed stop in
place forever.

Every number this depends on - the MA period, how many closes count as
a signal, how far the MA needs to clear cost basis before trailing
kicks in, and the "approaching"/"extended" distance thresholds used
for the warning badges - has a global default (see database.
get_strategy_settings(), edited on the Settings page) and can be
overridden per position (see database.get_position_ma_settings()).

This module only COMPUTES the signal and, if asked, applies it - see
pages/4_Open_Positions.py's "MA Stop Rule" table for where a position
actually opts in via mode ("off"/"manual"/"auto").
"""

from datetime import datetime, timedelta

import charting
import database
import timeutil


def compute_signal(symbol, cost_basis_per_share, is_short, settings):
    """
    Fetches recent daily price history for `symbol` and returns where
    it stands against its own MA Stop Rule:
        {
            "ma_value": float or None,        # today's MA value
            "signal_closes": int,             # how many of the most
                                               # recent `closes_threshold`
                                               # closes are on the
                                               # WRONG side of the MA
                                               # for this position's
                                               # direction
            "sell_signal": bool,              # signal_closes reached
                                               # the threshold
            "unlocked": bool,                 # the MA has cleared
                                               # cost basis by enough
                                               # to trail a stop to it
            "distance_pct": float or None,    # current price's %
                                               # distance from the MA
                                               # (always positive - a
                                               # magnitude, not a
                                               # direction)
            "approaching": bool,
            "extended": bool,
        }
    `settings` is this position's already-merged settings (see
    database.get_position_ma_settings()) - needs "ma_period",
    "closes_threshold", "unlock_pct", "approach_pct", "extended_pct".
    A LONG is "wrong side" below the MA and "unlocked" once the MA is
    above cost basis by unlock_pct; a SHORT is the mirror image of
    both. Returns every field as None/False if price history couldn't
    be fetched (same as a bad/delisted symbol elsewhere in this app).
    """
    period = settings["ma_period"]
    empty_result = {
        "ma_value": None, "signal_closes": 0, "sell_signal": False,
        "unlocked": False, "distance_pct": None, "approaching": False, "extended": False,
    }

    # Extra calendar days before the window we actually need, same
    # buffer build_figure()'s own MA lookback uses, so the MA already
    # has a real value on the earliest day we'll check. fetch_history()
    # compares its display_start against a DatetimeIndex internally, so
    # this needs to be a real datetime, not a plain date, the same way
    # every other caller in this project already converts (see
    # database.get_trades()'s entry_date, for instance).
    lookback_days = int(period * charting.LOOKBACK_DAYS_PER_PERIOD["1d"]) + 10
    end = datetime.combine(timeutil.today_eastern(), datetime.min.time())
    start = end - timedelta(days=lookback_days)

    history = charting.fetch_history(symbol, start, start, end, "1d", [period])
    ma_col = f"MA{period}"
    if history.empty or ma_col not in history.columns:
        return empty_result

    valid = history.dropna(subset=[ma_col])
    if valid.empty:
        return empty_result

    # .item() converts pandas/numpy scalar types (np.float64, np.bool_)
    # to plain Python ones - avoids surprises later, e.g. in Streamlit's
    # data_editor number columns or if this ever gets JSON-serialized.
    ma_value = valid[ma_col].iloc[-1].item()
    current_price = valid["Close"].iloc[-1].item()

    recent = valid.tail(settings["closes_threshold"])
    if is_short:
        signal_closes = int((recent["Close"] > recent[ma_col]).sum())
        unlocked = bool(ma_value < cost_basis_per_share * (1 - settings["unlock_pct"] / 100)) if cost_basis_per_share else False
    else:
        signal_closes = int((recent["Close"] < recent[ma_col]).sum())
        unlocked = bool(ma_value > cost_basis_per_share * (1 + settings["unlock_pct"] / 100)) if cost_basis_per_share else False

    distance_pct = abs(current_price - ma_value) / ma_value * 100 if ma_value else None

    return {
        "ma_value": ma_value,
        "signal_closes": signal_closes,
        "sell_signal": signal_closes >= settings["closes_threshold"],
        "unlocked": unlocked,
        "distance_pct": distance_pct,
        "approaching": distance_pct is not None and distance_pct <= settings["approach_pct"],
        "extended": distance_pct is not None and distance_pct >= settings["extended_pct"],
    }


def apply_auto_stop(conn, symbol, is_short, ma_value, current_stop):
    """
    For a position in "auto" mode whose MA has unlocked (see
    compute_signal()'s "unlocked" field - only call this when that's
    True): moves the stop-loss to the MA value, but ONLY in the
    protective direction - up towards the MA for a LONG, down towards
    it for a SHORT - never loosening whatever stop is already saved.
    A trailing stop should only ever tighten, so if you'd manually set
    a stop closer to the current price than the MA is, this leaves it
    alone rather than dragging it back out.

    Returns the resulting stop-loss (whether or not it actually
    changed), so the caller can display it without a second database
    read.
    """
    if current_stop is None:
        new_stop = ma_value
    elif is_short:
        new_stop = min(current_stop, ma_value)
    else:
        new_stop = max(current_stop, ma_value)

    if new_stop != current_stop:
        database.set_stop_loss(conn, symbol, new_stop)
    return new_stop
