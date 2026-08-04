"""
Tests reconcile_all_pending() - the orchestration glue over already
individually-tested pieces (database.get_unreconciled_signals(),
screener.reconcile.reconcile_signal(), database.
update_signal_reconciliation()). Runs against the REAL dev database
(same convention this project already uses elsewhere - see
tests/test_screener_engine.py's own DB-touching tests) but fakes the
yfinance fetch via monkeypatch, so it's still fast and deterministic -
no live network call, no flakiness from real market data changing day
to day.

Every test cleans up its own signal_log rows in a finally block, even
on failure, so a broken assertion never leaves test data behind for a
future run to trip over.

IMPORTANT: reconcile_all_pending() processes EVERY unreconciled row in
signal_log, not just this test's own - and since real Screener usage
now leaves real pending signals in that table, every monkeypatched
`_cached_fetch_one` below is scoped to TEST_TICKER (falling back to
None - a harmless "fetch failed" - for anything else). An unscoped
fake that returns the same made-up price history for every ticker
would apply nonsense data to real signals too, and depending on
whether their real trigger/stop happens to fall inside that fake
range, could silently write bogus reconciliation results to production
data. (This bit us once already - see git history around the
transaction-dedup fix this file was touched alongside.)
"""
import numpy as np
import pandas as pd
import pytest

import database
import screener.reconcile as reconcile

TEST_TICKER = "__RECONCILE_TEST__"


def _fake_history(signal_date, rows):
    """Builds a raw yfinance-shaped DataFrame (DatetimeIndex,
    Open/High/Low/Close/Volume) - 120 flat warmup days ending the day
    before `signal_date`, then `rows` (dicts with open/high/low/close)
    starting the day after."""
    warmup_dates = pd.bdate_range(end=pd.Timestamp(signal_date), periods=121)[:-1]
    warmup = pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1_000_000,
    }, index=warmup_dates)

    tail_dates = pd.bdate_range(pd.Timestamp(signal_date) + pd.Timedelta(days=1), periods=len(rows))
    tail = pd.DataFrame([
        {"Open": r["open"], "High": r["high"], "Low": r["low"], "Close": r["close"], "Volume": 1_000_000}
        for r in rows
    ], index=tail_dates)
    return pd.concat([warmup, tail])


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM signal_log WHERE ticker = %s", (TEST_TICKER,))
    conn.commit()


def test_reconcile_all_pending_resolves_a_stopped_out_signal(conn, monkeypatch):
    signal_date = pd.Timestamp("2024-06-03").date()
    trigger, stop, adr = 105.0, 98.0, 3.0
    expires = pd.Timestamp("2024-06-06").date()

    _cleanup(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signal_log
            (signal_date, ticker, tier, trigger_price, stop_price, risk_pct, shares,
             position_usd, risk_usd, adr, rs_excess, rsline_at_high, nr7_2,
             pct_off_high, close_price, gate_pct, expires)
        VALUES (%s, %s, 'strict', %s, %s, 5.0, 10, 1000, 70, %s, 10.0, TRUE, FALSE, -1.0, 104.0, 1.0, %s)
        """,
        (signal_date, TEST_TICKER, trigger, stop, adr, expires),
    )
    conn.commit()

    rows = [
        dict(open=104, high=106, low=103, close=105.5),  # triggers day 1
        dict(open=104, high=105, low=100, close=101),
        dict(open=100, high=101, low=97, close=98.5),     # stop hit (low <= 98)
    ]
    fake = _fake_history(signal_date, rows)
    monkeypatch.setattr(
        reconcile, "_cached_fetch_one",
        lambda ticker, session_date: fake if ticker == TEST_TICKER else None)

    try:
        summary = reconcile.reconcile_all_pending(conn)
        assert summary["resolved"] >= 1
        assert TEST_TICKER not in summary["fetch_failed"]

        remaining = [s for s in database.get_unreconciled_signals(conn) if s["ticker"] == TEST_TICKER]
        assert remaining == []

        history = database.get_signals_for_history(
            conn, pd.Timestamp("2024-01-01").date(), pd.Timestamp("2024-12-31").date())
        row = [h for h in history if h["ticker"] == TEST_TICKER][0]
        assert row["triggered"] is True
        assert row["exit_reason"] == "initial_stop"
        assert row["outcome_r"] == pytest.approx(-1.0, abs=0.01)
    finally:
        _cleanup(conn)


def test_reconcile_all_pending_leaves_still_open_signal_unreconciled(conn, monkeypatch):
    signal_date = pd.Timestamp("2024-06-03").date()
    trigger, stop, adr = 105.0, 90.0, 3.0
    expires = pd.Timestamp("2024-06-06").date()

    _cleanup(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signal_log
            (signal_date, ticker, tier, trigger_price, stop_price, risk_pct, shares,
             position_usd, risk_usd, adr, rs_excess, rsline_at_high, nr7_2,
             pct_off_high, close_price, gate_pct, expires)
        VALUES (%s, %s, 'strict', %s, %s, 14.0, 10, 1000, 150, %s, 10.0, TRUE, FALSE, -1.0, 104.0, 1.0, %s)
        """,
        (signal_date, TEST_TICKER, trigger, stop, adr, expires),
    )
    conn.commit()

    rows = [dict(open=104, high=106, low=103, close=105.5)]
    rows += [dict(open=105.5, high=106, low=105, close=105.5)] * 5  # quiet, no exit
    fake = _fake_history(signal_date, rows)
    # Scoped to TEST_TICKER only - other pending signals in the table
    # (real ones, from actual Screener use) must never be reconciled
    # against this fake price data. Returning None for anything else is
    # a harmless "fetch failed" for those, not a corrupting write.
    monkeypatch.setattr(
        reconcile, "_cached_fetch_one",
        lambda ticker, session_date: fake if ticker == TEST_TICKER else None)

    try:
        summary = reconcile.reconcile_all_pending(conn)
        assert summary["still_pending"] >= 1

        remaining = [s for s in database.get_unreconciled_signals(conn) if s["ticker"] == TEST_TICKER]
        assert len(remaining) == 1
        assert remaining[0]["triggered"] is True
        assert remaining[0]["trigger_date"] is not None
        assert remaining[0]["outcome_r"] is None
    finally:
        _cleanup(conn)


def test_reconcile_all_pending_records_fetch_failure_without_crashing(conn, monkeypatch):
    signal_date = pd.Timestamp("2024-06-03").date()
    expires = pd.Timestamp("2024-06-06").date()

    _cleanup(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signal_log
            (signal_date, ticker, tier, trigger_price, stop_price, risk_pct, shares,
             position_usd, risk_usd, adr, rs_excess, rsline_at_high, nr7_2,
             pct_off_high, close_price, gate_pct, expires)
        VALUES (%s, %s, 'strict', 105.0, 98.0, 6.7, 10, 1000, 70, 3.0, 10.0, TRUE, FALSE, -1.0, 104.0, 1.0, %s)
        """,
        (signal_date, TEST_TICKER, expires),
    )
    conn.commit()

    # Unconditionally None is safe here regardless of ticker (a fetch
    # failure never writes anything - see reconcile_all_pending()) -
    # unlike the other two tests above, there's no fake price data that
    # could apply nonsense to a real signal. Other real pending tickers
    # will also show up as "fetch failed" in this run as a side effect;
    # the assertions below only check TEST_TICKER's own outcome.
    monkeypatch.setattr(reconcile, "_cached_fetch_one", lambda ticker, session_date: None)

    try:
        summary = reconcile.reconcile_all_pending(conn)
        assert TEST_TICKER in summary["fetch_failed"]
        assert summary["resolved"] == 0
        remaining = [s for s in database.get_unreconciled_signals(conn) if s["ticker"] == TEST_TICKER]
        assert len(remaining) == 1  # untouched, will retry next run
    finally:
        _cleanup(conn)
