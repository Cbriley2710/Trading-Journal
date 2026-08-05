"""
Tests for analyze_trades.match_trades_lifo() and its two preprocessing
steps (merge_partial_fills, net_same_day_short_opens).

No test file existed for this core function before - these lock in the
EXISTING correct behavior (long round trips, short round trips, partial
fill merging) as a safety net, plus the new same-day netting rule (see
net_same_day_short_opens()'s own docstring for the full story: broker
CSV exports have no time-of-day, so a day with BOTH a BUY and a
SELL_SHORT for the same symbol is order-dependent - the real NBIS
account showed as simultaneously LONG and SHORT the same stock
depending on which transaction match_trades_lifo() happened to process
first, which can never be real).
"""
from datetime import datetime

from analyze_trades import match_trades_lifo, net_same_day_short_opens


def _txn(date, symbol, action, price, quantity):
    return {"date": date, "symbol": symbol, "action": action, "price": price, "quantity": quantity}


# ---------------------------------------------------------------------
# Baseline behavior (must keep working exactly as before)
# ---------------------------------------------------------------------

def test_simple_long_round_trip():
    txns = [
        _txn(datetime(2026, 1, 5), "AAPL", "BUY", 150.0, 100),
        _txn(datetime(2026, 1, 12), "AAPL", "SELL", 155.0, 100),
    ]
    closed, open_long, open_short = match_trades_lifo(txns)
    assert len(closed) == 1
    assert closed[0]["direction"] == "LONG"
    assert closed[0]["profit_loss"] == 500.0
    assert open_long["AAPL"] == []
    assert open_short["AAPL"] == []


def test_simple_short_round_trip():
    txns = [
        _txn(datetime(2026, 1, 5), "TSLA", "SELL_SHORT", 200.0, 50),
        _txn(datetime(2026, 1, 12), "TSLA", "BUY", 190.0, 50),
    ]
    closed, open_long, open_short = match_trades_lifo(txns)
    assert len(closed) == 1
    assert closed[0]["direction"] == "SHORT"
    assert closed[0]["profit_loss"] == 500.0
    assert open_long["TSLA"] == []
    assert open_short["TSLA"] == []


def test_unmatched_buy_stays_open():
    txns = [_txn(datetime(2026, 1, 5), "MSFT", "BUY", 300.0, 25)]
    closed, open_long, open_short = match_trades_lifo(txns)
    assert closed == []
    assert open_long["MSFT"] == [{"price": 300.0, "quantity": 25, "date": datetime(2026, 1, 5)}]


def test_different_day_buy_then_short_is_unaffected():
    """A short opened on one day and covered on another already works
    fine on its own - net_same_day_short_opens() only ever acts on a
    BUY and a SELL_SHORT on the SAME day, so this must pass through
    completely unchanged."""
    txns = [
        _txn(datetime(2026, 1, 5), "GME", "BUY", 20.0, 100),
        _txn(datetime(2026, 1, 6), "GME", "SELL_SHORT", 25.0, 40),
    ]
    closed, open_long, open_short = match_trades_lifo(txns)
    assert closed == []
    assert open_long["GME"] == [{"price": 20.0, "quantity": 100, "date": datetime(2026, 1, 5)}]
    assert open_short["GME"] == [{"price": 25.0, "quantity": 40, "date": datetime(2026, 1, 6)}]


# ---------------------------------------------------------------------
# net_same_day_short_opens() in isolation
# ---------------------------------------------------------------------

def test_netting_noop_when_only_buys():
    txns = [_txn(datetime(2026, 1, 5), "AAPL", "BUY", 150.0, 100)]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert remaining == txns
    assert synthetic == []


def test_netting_noop_when_only_shorts():
    txns = [_txn(datetime(2026, 1, 5), "AAPL", "SELL_SHORT", 150.0, 100)]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert remaining == txns
    assert synthetic == []


def test_netting_equal_buy_and_short_fully_nets():
    txns = [
        _txn(datetime(2026, 1, 5), "NBIS", "BUY", 190.0, 100),
        _txn(datetime(2026, 1, 5), "NBIS", "SELL_SHORT", 191.0, 100),
    ]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert remaining == []
    assert len(synthetic) == 1
    trade = synthetic[0]
    assert trade["direction"] == "SHORT"
    assert trade["quantity"] == 100
    assert trade["buy_price"] == 190.0
    assert trade["sell_price"] == 191.0
    assert trade["profit_loss"] == 100.0


def test_netting_leaves_buy_leftover_when_buy_exceeds_short():
    txns = [
        _txn(datetime(2026, 1, 5), "NBIS", "BUY", 190.0, 150),
        _txn(datetime(2026, 1, 5), "NBIS", "SELL_SHORT", 191.0, 100),
    ]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert len(synthetic) == 1
    assert synthetic[0]["quantity"] == 100
    assert len(remaining) == 1
    assert remaining[0]["action"] == "BUY"
    assert remaining[0]["quantity"] == 50


def test_netting_leaves_short_leftover_when_short_exceeds_buy():
    txns = [
        _txn(datetime(2026, 1, 5), "NBIS", "BUY", 190.0, 100),
        _txn(datetime(2026, 1, 5), "NBIS", "SELL_SHORT", 191.0, 150),
    ]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert len(synthetic) == 1
    assert synthetic[0]["quantity"] == 100
    assert len(remaining) == 1
    assert remaining[0]["action"] == "SELL_SHORT"
    assert remaining[0]["quantity"] == 50


def test_netting_ignores_other_symbols_and_other_actions():
    txns = [
        _txn(datetime(2026, 1, 5), "NBIS", "BUY", 190.0, 100),
        _txn(datetime(2026, 1, 5), "NBIS", "SELL_SHORT", 191.0, 100),
        _txn(datetime(2026, 1, 5), "AAPL", "BUY", 150.0, 10),
        _txn(datetime(2026, 1, 5), "NBIS", "SELL", 193.0, 5),
    ]
    remaining, synthetic = net_same_day_short_opens(txns)
    assert len(synthetic) == 1
    remaining_actions = sorted((t["symbol"], t["action"]) for t in remaining)
    assert remaining_actions == [("AAPL", "BUY"), ("NBIS", "SELL")]


# ---------------------------------------------------------------------
# End-to-end: the real NBIS-shaped tangle, order-independence
# ---------------------------------------------------------------------

def _nbis_shaped_transactions():
    """Loosely mirrors the real NBIS same-day tangle: same-day BUY and
    SELL_SHORT activity plus an ordinary same-day long round trip
    mixed in, all date-only (no time-of-day) like a real CSV import."""
    return [
        _txn(datetime(2026, 7, 30), "NBIS", "SELL_SHORT", 191.0, 700),
        _txn(datetime(2026, 7, 30), "NBIS", "BUY", 189.9, 600),
        _txn(datetime(2026, 7, 30), "NBIS", "SELL_SHORT", 188.1, 600),
        _txn(datetime(2026, 7, 30), "NBIS", "BUY", 191.1, 549),
        _txn(datetime(2026, 7, 30), "NBIS", "BUY", 191.27, 51),
        _txn(datetime(2026, 7, 30), "NBIS", "SELL", 193.92, 100),
        _txn(datetime(2026, 7, 30), "NBIS", "BUY", 187.63, 750),
        _txn(datetime(2026, 7, 30), "NBIS", "SELL", 193.85, 10),
    ]


def test_same_day_tangle_never_shows_simultaneous_long_and_short():
    txns = _nbis_shaped_transactions()
    closed, open_long, open_short = match_trades_lifo(txns)
    assert not (open_long["NBIS"] and open_short["NBIS"]), (
        "an account can never really be simultaneously long and short "
        "the same stock - this is exactly the bug net_same_day_short_opens() fixes"
    )


def test_same_day_tangle_result_is_order_independent():
    """The whole point of the netting rule: since the true execution
    order can't be recovered from the data, the RESULT must no longer
    depend on which arbitrary order the rows happen to be in."""
    forward = _nbis_shaped_transactions()
    reversed_txns = list(reversed(forward))

    closed_f, open_long_f, open_short_f = match_trades_lifo(forward)
    closed_r, open_long_r, open_short_r = match_trades_lifo(reversed_txns)

    assert open_long_f["NBIS"] == open_long_r["NBIS"]
    assert open_short_f["NBIS"] == open_short_r["NBIS"]
    assert sum(t["profit_loss"] for t in closed_f) == sum(t["profit_loss"] for t in closed_r)
