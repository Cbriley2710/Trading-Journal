"""
Tests for database.get_previous_journal_date(), position_sizing.
newly_opened_positions_since(), and analyze_trades.partial_sell_notes()
- three pieces behind the Journal Session's "since your last journal
session" summary (see ui.render_since_last_journal_summary()). The first
two hit the REAL dev database, same convention as tests/test_csv_upload_
tracking.py: throwaway symbols for transactions/trades, and far-future
throwaway dates for daily_journal_notes so a real journal entry is never
touched, everything deleted again in a `finally` block. partial_sell_
notes() is a pure function (plain dict records in, no DB) - its tests
need none of that.
"""
from datetime import date, datetime

import pytest

import analyze_trades
import database
import position_sizing

SYMBOL = "__JOURNALSUMMARYTEST__"
JOURNAL_DATE_1 = date(2099, 1, 1)
JOURNAL_DATE_2 = date(2099, 1, 5)


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup_journal_notes(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_journal_notes WHERE entry_date IN (%s, %s)", (JOURNAL_DATE_1, JOURNAL_DATE_2))
    conn.commit()


def _cleanup_trades_and_transactions(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM trades WHERE symbol = %s", (SYMBOL,))
    cur.execute("DELETE FROM transactions WHERE symbol = %s", (SYMBOL,))
    conn.commit()
    database.clear_trade_cache()


def test_get_previous_journal_date(conn):
    _cleanup_journal_notes(conn)
    try:
        database.save_daily_journal_note(conn, JOURNAL_DATE_1, "test note 1")
        database.save_daily_journal_note(conn, JOURNAL_DATE_2, "test note 2")

        assert database.get_previous_journal_date(conn, date(2099, 1, 10)) == JOURNAL_DATE_2
        # A date far enough in the past that no real journal entry
        # could exist before it - the "never journaled before" case.
        assert database.get_previous_journal_date(conn, date(1900, 1, 1)) is None
    finally:
        _cleanup_journal_notes(conn)


def _insert_trade(conn, entry_date, exit_date, buy_price, sell_price, quantity, profit_loss, direction="LONG"):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trades (symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (SYMBOL, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction),
    )
    conn.commit()
    database.clear_trade_cache()


def test_newly_opened_positions_since_includes_closed_trade_after_cutoff(conn):
    _cleanup_trades_and_transactions(conn)
    try:
        cutoff = date(2099, 1, 1)
        # Before the cutoff - should be excluded.
        _insert_trade(
            conn, datetime(2098, 12, 20), datetime(2098, 12, 25),
            buy_price=10.0, sell_price=12.0, quantity=100, profit_loss=200.0,
        )
        # After the cutoff, LONG - entry price is buy_price.
        _insert_trade(
            conn, datetime(2099, 1, 2), datetime(2099, 1, 3),
            buy_price=20.0, sell_price=25.0, quantity=50, profit_loss=250.0,
        )
        # After the cutoff, SHORT - entry price is sell_price (see
        # analyze_trades.trade_stats() for why the pairing flips).
        _insert_trade(
            conn, datetime(2099, 1, 4), datetime(2099, 1, 5),
            buy_price=8.0, sell_price=15.0, quantity=10, profit_loss=70.0, direction="SHORT",
        )

        results = position_sizing.newly_opened_positions_since(conn, cutoff)
        matching = [r for r in results if not r["is_open"]]

        assert len(matching) == 2
        long_trade = next(r for r in matching if r["direction"] == "LONG")
        short_trade = next(r for r in matching if r["direction"] == "SHORT")
        assert long_trade["cost_basis"] == pytest.approx(20.0 * 50)
        assert short_trade["cost_basis"] == pytest.approx(15.0 * 10)
    finally:
        _cleanup_trades_and_transactions(conn)


def _insert_transaction(conn, txn_date, action, price, quantity):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (date, symbol, action, price, quantity, source) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (txn_date, SYMBOL, action, price, quantity, "csv"),
    )
    conn.commit()
    database.clear_trade_cache()


def test_newly_opened_positions_since_includes_open_position_after_cutoff(conn):
    _cleanup_trades_and_transactions(conn)
    try:
        cutoff = date(2099, 1, 1)
        # A buy with no matching sell - still open.
        _insert_transaction(conn, datetime(2099, 1, 2), "BUY", 30.0, 20)

        results = position_sizing.newly_opened_positions_since(conn, cutoff)
        open_positions = [r for r in results if r["is_open"]]

        assert len(open_positions) == 1
        assert open_positions[0]["cost_basis"] == pytest.approx(30.0 * 20)
    finally:
        _cleanup_trades_and_transactions(conn)


def test_partial_sell_notes_flags_a_symbol_still_open():
    # Sold 30 of what was a 100-share position (70 still open) - 30% sold.
    closed_trades = [{"symbol": "AAPL", "quantity": 30}]
    open_positions = [{"symbol": "AAPL", "quantity": 70}]

    notes = analyze_trades.partial_sell_notes(closed_trades, open_positions)

    pct_sold, open_qty = notes["AAPL"]
    assert pct_sold == pytest.approx(30.0)
    assert open_qty == 70


def test_partial_sell_notes_sums_multiple_closed_trades_for_the_same_symbol():
    # Two separate closed trades (e.g. two different LIFO-matched lots)
    # both sold this session - their quantities should combine before
    # computing the percentage, not just use the last one seen.
    closed_trades = [{"symbol": "AAPL", "quantity": 20}, {"symbol": "AAPL", "quantity": 10}]
    open_positions = [{"symbol": "AAPL", "quantity": 70}]

    notes = analyze_trades.partial_sell_notes(closed_trades, open_positions)

    pct_sold, open_qty = notes["AAPL"]
    assert pct_sold == pytest.approx(30.0)  # (20+10) sold of 100 total


def test_partial_sell_notes_excludes_a_fully_closed_symbol():
    # No open position left for this symbol - a full exit, not a partial
    # sell, so it shouldn't show up at all.
    closed_trades = [{"symbol": "AAPL", "quantity": 100}]
    open_positions = []

    notes = analyze_trades.partial_sell_notes(closed_trades, open_positions)

    assert "AAPL" not in notes


def test_partial_sell_notes_ignores_open_positions_never_sold_from():
    # An open position that has nothing to do with this session's closed
    # trades shouldn't produce a note either.
    closed_trades = [{"symbol": "AAPL", "quantity": 30}]
    open_positions = [{"symbol": "AAPL", "quantity": 70}, {"symbol": "MSFT", "quantity": 50}]

    notes = analyze_trades.partial_sell_notes(closed_trades, open_positions)

    assert list(notes.keys()) == ["AAPL"]
