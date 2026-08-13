"""
Tests for database.record_csv_upload()/get_new_trades_since_last_upload()
- the "new trades since your last CSV upload" tracking shown on the
Journal Session's Today's Thoughts step and the Daily Report cover
page (see ui.render_new_trades_since_upload()). Run against the REAL
dev database (same convention as tests/test_transaction_dedup.py) - a
throwaway symbol keeps test transactions from colliding with real
trade data. csv_upload_tracking's single row is LIVE, shared state the
real app also reads (same table shape as account_settings) - snapshotted
and restored exactly rather than just deleted, the same "DB test safety
for shared-state rows" precaution that table already needs.

The cutoff is transactions.id (insertion order), not a date - see
database.py's own comment in init_db() on why a CSV-imported
transaction's date (always flattened to midnight, no time-of-day)
would make a same-day comparison unreliable.
"""
from datetime import datetime

import pytest

import database

SYMBOL = "__NEWTRADESTEST__"


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup_transactions(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE symbol = %s", (SYMBOL,))
    conn.commit()


def _insert_transaction(conn, date, action, price, quantity):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (date, symbol, action, price, quantity, source) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (date, SYMBOL, action, price, quantity, "csv"),
    )
    conn.commit()


def _snapshot_upload_tracking(conn):
    cur = conn.cursor()
    cur.execute("SELECT uploaded_at, cutoff_id, previous_cutoff_id FROM csv_upload_tracking WHERE id = 1")
    return cur.fetchone()


def _restore_upload_tracking(conn, snapshot):
    cur = conn.cursor()
    if snapshot is None:
        cur.execute("DELETE FROM csv_upload_tracking WHERE id = 1")
    else:
        cur.execute(
            """
            INSERT INTO csv_upload_tracking (id, uploaded_at, cutoff_id, previous_cutoff_id)
            VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                uploaded_at = EXCLUDED.uploaded_at,
                cutoff_id = EXCLUDED.cutoff_id,
                previous_cutoff_id = EXCLUDED.previous_cutoff_id
            """,
            snapshot,
        )
    conn.commit()


def test_record_csv_upload_shifts_cutoff_forward(conn):
    original = _snapshot_upload_tracking(conn)
    _cleanup_transactions(conn)
    try:
        database.record_csv_upload(conn, datetime(2026, 1, 1))
        first_cutoff_id = _snapshot_upload_tracking(conn)[1]

        _insert_transaction(conn, datetime(2026, 1, 2), "BUY", 10.0, 5)

        database.record_csv_upload(conn, datetime(2026, 1, 3))
        _uploaded_at, cutoff_id, previous_cutoff_id = _snapshot_upload_tracking(conn)
        assert previous_cutoff_id == first_cutoff_id, "previous_cutoff_id should be the OLD cutoff_id"
        assert cutoff_id > first_cutoff_id, "cutoff_id should include the just-inserted row"
    finally:
        _cleanup_transactions(conn)
        _restore_upload_tracking(conn, original)


def test_get_new_trades_since_last_upload_returns_only_whats_new(conn):
    original = _snapshot_upload_tracking(conn)
    _cleanup_transactions(conn)
    try:
        database.record_csv_upload(conn, datetime(2026, 1, 1))  # baseline: nothing new yet

        _insert_transaction(conn, datetime(2026, 1, 2), "BUY", 123.45, 10)
        _insert_transaction(conn, datetime(2026, 1, 3), "SELL_SHORT", 67.89, 3)

        # This upload's own diff picks up both rows added since the baseline above.
        database.record_csv_upload(conn, datetime(2026, 1, 4))

        new_trades = [t for t in database.get_new_trades_since_last_upload(conn) if t["symbol"] == SYMBOL]
        assert len(new_trades) == 2
        assert new_trades[0]["action"] == "BUY"
        assert new_trades[0]["price"] == 123.45
        assert new_trades[1]["action"] == "SELL_SHORT"
    finally:
        _cleanup_transactions(conn)
        _restore_upload_tracking(conn, original)


def test_get_new_trades_since_last_upload_nothing_new_since_the_last_upload(conn):
    """A second upload with no new transactions in between must show
    nothing - the window only ever covers what came in since the
    PREVIOUS upload, not the whole transaction history."""
    original = _snapshot_upload_tracking(conn)
    _cleanup_transactions(conn)
    try:
        database.record_csv_upload(conn, datetime(2026, 1, 1))
        _insert_transaction(conn, datetime(2026, 1, 2), "BUY", 50.0, 1)
        database.record_csv_upload(conn, datetime(2026, 1, 3))  # acknowledges the row above

        database.record_csv_upload(conn, datetime(2026, 1, 4))  # nothing new happened in between

        new_trades = [t for t in database.get_new_trades_since_last_upload(conn) if t["symbol"] == SYMBOL]
        assert new_trades == []
    finally:
        _cleanup_transactions(conn)
        _restore_upload_tracking(conn, original)


def test_get_new_trades_since_last_upload_empty_before_any_upload(conn):
    """No csv_upload_tracking row at all (never uploaded) - nothing to
    compare against, so this must return [] rather than dumping the
    entire transaction history as "new"."""
    original = _snapshot_upload_tracking(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM csv_upload_tracking WHERE id = 1")
    conn.commit()
    try:
        assert database.get_new_trades_since_last_upload(conn) == []
    finally:
        _restore_upload_tracking(conn, original)
