"""
Position Management
====================
This app never places trades or changes how many shares you buy - you
size every position yourself. What this module does is track a
RECOMMENDATION: an "ideal" position size (as a % of your account value,
set on the Settings page), which automatically shrinks after a run of
losing trades and grows back gradually after a run of winners. This is
sometimes called "progressive exposure" - trade smaller while you're
proving out an edge or recovering from a rough patch, and only trade
full size again once results back that up.

Two pieces:
- build_recent_trade_window(): gathers your most recent trades, closed
  AND currently open, so an open position's live gain/loss counts
  toward the recommendation just like a closed trade would.
- evaluate_position_sizing(): looks at that window and decides whether
  to step the recommended size up, down, or leave it alone.
"""

import charting
import database


def _closed_trade_entry(trade):
    """Turns one row from database.get_trades() into the shape
    build_recent_trade_window() works with. `key` is a tuple that
    uniquely identifies this exact trade - it's built the same way
    pages/1_Trade_Analyzer.py's _trade_key() is (symbol/dates/direction/
    quantity/prices, NOT trades.id, since database.rebuild_trades()
    regenerates every trade's id from scratch on each CSV import)."""
    is_win = trade["profit_loss"] >= 0
    key = (
        "CLOSED", trade["symbol"], trade["entry_date"].date().isoformat(),
        trade["date"].date().isoformat(), trade["direction"],
        trade["quantity"], trade["buy_price"], trade["sell_price"], is_win,
    )
    return {
        "symbol": trade["symbol"], "direction": trade["direction"],
        "entry_date": trade["entry_date"], "is_open": False,
        "is_win": is_win, "pl": trade["profit_loss"], "key": key,
    }


def _open_position_entry(position):
    """Turns one row from database.get_open_positions() into the same
    shape as _closed_trade_entry() above, using TODAY's live price to
    decide win or loss. Returns None if there's no current price to
    judge it by (e.g. Yahoo Finance is temporarily unreachable) - the
    caller skips it for this evaluation rather than guessing.

    Includes `is_win` inside the identifying `key` on purpose: an open
    position's win/loss status can flip day to day as the price moves,
    and we WANT that flip to look like a "different" window entry so
    evaluate_position_sizing() notices it (see that function's own
    docstring for why this matters)."""
    current_price = charting.fetch_latest_price(position["symbol"])
    if current_price is None:
        return None

    is_short = position["direction"] == "SHORT"
    cost_basis = position["avg_price"] * position["quantity"]
    current_value = current_price * position["quantity"]
    # A short position profits when the price FALLS - opposite direction
    # from a long one (same formula used elsewhere in this app, e.g.
    # pages/4_Open_Positions.py's own unrealized P/L calculation).
    unrealized_pl = (cost_basis - current_value) if is_short else (current_value - cost_basis)
    is_win = unrealized_pl >= 0

    key = (
        "OPEN", position["symbol"], position["direction"],
        position["entry_date"].isoformat(), position["quantity"],
        position["avg_price"], is_win,
    )
    return {
        "symbol": position["symbol"], "direction": position["direction"],
        "entry_date": position["entry_date"], "is_open": True,
        "is_win": is_win, "pl": unrealized_pl, "key": key,
    }


def build_recent_trade_window(conn, n):
    """
    Returns your most recent `n` trades - closed and currently open
    together - sorted oldest to newest by entry date. Each one is a
    dict with: symbol, direction, entry_date, is_open, is_win, pl, key.

    This is the same "closed trades + open positions, merged by entry
    date" idea pages/4_Open_Positions.py's "Last 10 Trades" chart
    already uses - both should call this one function so there's only
    one place that logic lives.
    """
    entries = [_closed_trade_entry(t) for t in database.get_trades(conn)]
    for position in database.get_open_positions(conn):
        entry = _open_position_entry(position)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e["entry_date"])
    return entries[-n:] if n > 0 else []


def newly_opened_positions_since(conn, cutoff_date):
    """
    Every position - closed or still open - whose entry_date is after
    `cutoff_date`, with its cost basis AT ENTRY (not today's value).
    Used by the Journal Session's Today's Thoughts step to check
    whether a newly-opened position was sized within the current
    Position Sizing recommendation - sizing is decided the moment a
    trade is opened, so that's what gets checked, regardless of
    whether the trade has since closed.

    Returns a list of {"symbol", "direction", "entry_date", "is_open",
    "cost_basis"} dicts, oldest first.
    """
    positions = []

    for trade in database.get_trades(conn):
        if trade["entry_date"].date() <= cutoff_date:
            continue
        is_short = trade["direction"] == "SHORT"
        # Same entry-price pairing as analyze_trades.trade_stats(): a
        # short's entry is the price it was SOLD at, not bought back at.
        entry_price = trade["sell_price"] if is_short else trade["buy_price"]
        positions.append({
            "symbol": trade["symbol"], "direction": trade["direction"],
            "entry_date": trade["entry_date"], "is_open": False,
            "cost_basis": entry_price * trade["quantity"],
        })

    for position in database.get_open_positions(conn):
        if position["entry_date"].date() <= cutoff_date:
            continue
        positions.append({
            "symbol": position["symbol"], "direction": position["direction"],
            "entry_date": position["entry_date"], "is_open": True,
            "cost_basis": position["avg_price"] * position["quantity"],
        })

    positions.sort(key=lambda p: p["entry_date"])
    return positions


def decide_tier_step(state, tier_pcts, losers, winners, loss_threshold, win_threshold, window_key):
    """
    The pure decision at the heart of this feature - given where you
    ARE (state) and what the current rolling window looks like (losers/
    winners/window_key), decides whether to step the tier down, up, or
    leave it alone. Kept separate from evaluate_position_sizing() below
    (which does the database/price-fetching work around it) so this
    part - the actual rule - can be tested directly with hand-built
    numbers, no database or network needed.

    `state` is a dict shaped like database.get_position_sizing_state()'s
    return value. Returns a new dict of that same shape, plus a
    "step_direction" key (None, "down", or "up").

    Only ever moves one tier per call, and only when `window_key`
    differs from the key that caused the LAST step in that same
    direction - otherwise calling this repeatedly with an unchanged
    window (e.g. viewing the same page twice) would keep pushing the
    tier further every time.
    """
    lowest_tier_index = len(tier_pcts) - 1
    tier_index = state["current_tier_index"]
    down_key = state["last_down_window_key"]
    up_key = state["last_up_window_key"]
    step_direction = None

    should_step_down = losers >= loss_threshold and window_key != down_key
    should_step_up = winners >= win_threshold and window_key != up_key

    if should_step_down and tier_index < lowest_tier_index:
        tier_index += 1
        down_key = window_key
        step_direction = "down"
    elif should_step_up and tier_index > 0:
        tier_index -= 1
        up_key = window_key
        step_direction = "up"

    return {
        "current_tier_index": tier_index,
        "last_down_window_key": down_key,
        "last_up_window_key": up_key,
        "step_direction": step_direction,
    }


def evaluate_position_sizing(conn):
    """
    The main entry point for this feature. Figures out which "tier" of
    the ideal baseline you should be trading at right now, based on how
    your most recent trades have gone, and returns everything the
    Settings page, Open Positions page, and Daily Report need to show
    it.

    Tier 0 is always full size (100% of your ideal baseline); higher
    tier numbers are smaller. A tier step only happens when the rolling
    window has genuinely changed since the last time it caused a step
    in that same direction - otherwise looking at this twice in one day
    with nothing new happening would keep shrinking your size forever.
    That "has it changed" check is done with a window key: every trade
    in the window contributes its own identifying key (see
    _closed_trade_entry/_open_position_entry above), and those are
    joined into one long string. If that string matches the one saved
    from the last downward (or upward) step, nothing's actually new, so
    no further step is taken.

    Returns a dict with: tier_index, tier_pct, recommended_pct_of_account,
    recommended_dollar_amount (None if no account value is set yet),
    window, losers, winners, window_size, step_direction (None/"up"/"down").
    """
    settings = database.get_position_sizing_settings(conn)
    state = database.get_position_sizing_state(conn)

    window = build_recent_trade_window(conn, settings["window_size"])
    losers = sum(1 for e in window if not e["is_win"])
    winners = sum(1 for e in window if e["is_win"])
    window_key = "|".join(str(e["key"]) for e in window)

    tier_pcts = settings["tier_pcts"]
    new_state = decide_tier_step(
        state, tier_pcts, losers, winners,
        settings["loss_threshold"], settings["win_threshold"], window_key,
    )
    tier_index = new_state["current_tier_index"]
    step_direction = new_state["step_direction"]

    if step_direction is not None:
        database._save_position_sizing_state(
            conn, tier_index, new_state["last_down_window_key"], new_state["last_up_window_key"],
        )

    account_value = charting.get_calculated_account_value(conn)
    recommended_pct_of_account = settings["ideal_baseline_pct"] * tier_pcts[tier_index] / 100
    recommended_dollar_amount = (
        recommended_pct_of_account / 100 * account_value if account_value else None
    )

    return {
        "tier_index": tier_index,
        "tier_pct": tier_pcts[tier_index],
        "ideal_baseline_pct": settings["ideal_baseline_pct"],
        "recommended_pct_of_account": recommended_pct_of_account,
        "recommended_dollar_amount": recommended_dollar_amount,
        "window": window,
        "losers": losers,
        "winners": winners,
        "window_size": len(window),
        "step_direction": step_direction,
        "account_value": account_value,
    }
