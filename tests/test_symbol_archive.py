"""
Tests for the watchlist-removal tracking (database.py) and the
storage-management job that PDFs/emails/deletes a long-removed,
never-traded watchlist ticker's Logbook (symbol_archive_report.py).

Runs against the REAL dev database (same convention as
tests/test_screener_reconcile_job.py), using a clearly-fake symbol
name so there's no risk of touching real data, cleaned up in a
finally block on every test even if an assertion fails. Real SMTP
sending (symbol_archive_report.send_symbol_archive_email) is always
monkeypatched - no test should ever send a real email.
"""
from datetime import datetime, timedelta

import pytest

import database
import symbol_archive_report as sar

TEST_SYMBOL = "__ARCHIVE_TEST__"


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM logbook_entries WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM chart_drawings WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM position_stops WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM price_cache WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM watchlist_removals WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM watchlist WHERE symbol = %s", (TEST_SYMBOL,))
    cur.execute("DELETE FROM trades WHERE symbol = %s", (TEST_SYMBOL,))
    conn.commit()


def test_remove_from_watchlist_records_removal(conn):
    _cleanup(conn)
    try:
        database.add_to_watchlist(conn, TEST_SYMBOL, list_id=1)
        database.remove_from_watchlist(conn, TEST_SYMBOL)
        stale = database.get_stale_watchlist_removals(conn, older_than_days=0)
        assert TEST_SYMBOL in stale
    finally:
        _cleanup(conn)


def test_re_adding_clears_the_removal(conn):
    _cleanup(conn)
    try:
        database.add_to_watchlist(conn, TEST_SYMBOL, list_id=1)
        database.remove_from_watchlist(conn, TEST_SYMBOL)
        database.add_to_watchlist(conn, TEST_SYMBOL, list_id=1)
        stale = database.get_stale_watchlist_removals(conn, older_than_days=0)
        assert TEST_SYMBOL not in stale
    finally:
        _cleanup(conn)


def test_get_stale_watchlist_removals_respects_grace_period(conn):
    _cleanup(conn)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO watchlist_removals (symbol, removed_at) VALUES (%s, %s)",
            (TEST_SYMBOL, datetime.now() - timedelta(days=3)),
        )
        conn.commit()
        assert TEST_SYMBOL not in database.get_stale_watchlist_removals(conn, older_than_days=7)
        assert TEST_SYMBOL in database.get_stale_watchlist_removals(conn, older_than_days=1)
    finally:
        _cleanup(conn)


def test_has_trade_history(conn):
    _cleanup(conn)
    try:
        assert database.has_trade_history(conn, TEST_SYMBOL) is False
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trades (symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (TEST_SYMBOL, datetime(2026, 1, 2), 100.0, 10, datetime(2026, 1, 5), 105.0, 50.0, "LONG"),
        )
        conn.commit()
        assert database.has_trade_history(conn, TEST_SYMBOL) is True
    finally:
        _cleanup(conn)


def test_delete_symbol_logbook_history_removes_everything(conn):
    _cleanup(conn)
    try:
        database.upsert_logbook_entry(conn, TEST_SYMBOL, datetime(2026, 1, 2).date(), notes="test note")
        database.delete_symbol_logbook_history(conn, TEST_SYMBOL)
        assert database.get_logbook_entries_for_symbol(conn, TEST_SYMBOL) == []
    finally:
        _cleanup(conn)


def test_archive_and_delete_skips_symbols_with_trade_history(conn, monkeypatch):
    _cleanup(conn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trades (symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (TEST_SYMBOL, datetime(2026, 1, 2), 100.0, 10, datetime(2026, 1, 5), 105.0, 50.0, "LONG"),
        )
        conn.commit()
        database.upsert_logbook_entry(conn, TEST_SYMBOL, datetime(2026, 1, 2).date(), notes="real trade note")
        cur.execute(
            "INSERT INTO watchlist_removals (symbol, removed_at) VALUES (%s, %s)",
            (TEST_SYMBOL, datetime.now() - timedelta(days=30)),
        )
        conn.commit()

        send_called = []
        monkeypatch.setattr(sar, "send_symbol_archive_email", lambda *a, **k: send_called.append(True))

        sar._archive_and_delete_one_symbol(conn, TEST_SYMBOL)

        assert send_called == []
        assert database.get_logbook_entries_for_symbol(conn, TEST_SYMBOL) != []
    finally:
        _cleanup(conn)


def test_archive_and_delete_archives_and_removes_watchlist_only_symbol(conn, monkeypatch):
    _cleanup(conn)
    try:
        database.upsert_logbook_entry(conn, TEST_SYMBOL, datetime(2026, 1, 2).date(), notes="watchlist idea")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO watchlist_removals (symbol, removed_at) VALUES (%s, %s)",
            (TEST_SYMBOL, datetime.now() - timedelta(days=30)),
        )
        conn.commit()

        sent = []
        monkeypatch.setattr(
            sar, "send_symbol_archive_email", lambda pdf_bytes, symbol: sent.append((symbol, len(pdf_bytes))))

        sar._archive_and_delete_one_symbol(conn, TEST_SYMBOL)

        assert len(sent) == 1
        assert sent[0][0] == TEST_SYMBOL
        assert sent[0][1] > 0
        assert database.get_logbook_entries_for_symbol(conn, TEST_SYMBOL) == []
        assert TEST_SYMBOL not in database.get_stale_watchlist_removals(conn, older_than_days=0)
    finally:
        _cleanup(conn)


def test_archive_and_delete_leaves_data_alone_if_email_fails(conn, monkeypatch):
    _cleanup(conn)
    try:
        database.upsert_logbook_entry(conn, TEST_SYMBOL, datetime(2026, 1, 2).date(), notes="watchlist idea")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO watchlist_removals (symbol, removed_at) VALUES (%s, %s)",
            (TEST_SYMBOL, datetime.now() - timedelta(days=30)),
        )
        conn.commit()

        def _boom(*a, **k):
            raise RuntimeError("SMTP is down")
        monkeypatch.setattr(sar, "send_symbol_archive_email", _boom)

        # _archive_and_delete_one_symbol() itself doesn't swallow
        # exceptions - that's archive_and_delete_stale_watchlist_
        # symbols()'s job (its per-symbol try/except, so one bad
        # symbol doesn't stop the rest). Here we're directly checking
        # the more important thing: a failure BEFORE the delete call
        # leaves the data completely untouched.
        with pytest.raises(RuntimeError):
            sar._archive_and_delete_one_symbol(conn, TEST_SYMBOL)

        assert database.get_logbook_entries_for_symbol(conn, TEST_SYMBOL) != []
        assert TEST_SYMBOL in database.get_stale_watchlist_removals(conn, older_than_days=0)
    finally:
        _cleanup(conn)


def test_orchestrator_catches_a_failing_symbol_without_stopping(conn, monkeypatch):
    """
    archive_and_delete_stale_watchlist_symbols() itself sweeps in
    EVERY real stale symbol via database.get_stale_watchlist_removals()
    - unsafe to call directly in a test against the shared dev/prod
    database (see this file's own module docstring). Scoped to just
    TEST_SYMBOL here by monkeypatching that lookup, so this test can
    exercise the outer per-symbol try/except without ever touching
    whatever real symbols might actually be stale at test time.
    """
    _cleanup(conn)
    try:
        monkeypatch.setattr(database, "get_stale_watchlist_removals", lambda conn, days: [TEST_SYMBOL])
        monkeypatch.setattr(
            sar, "_archive_and_delete_one_symbol",
            lambda conn, symbol: (_ for _ in ()).throw(RuntimeError("boom")))

        sar.archive_and_delete_stale_watchlist_symbols(conn)  # should not raise
    finally:
        _cleanup(conn)
