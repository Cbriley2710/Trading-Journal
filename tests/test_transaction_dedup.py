"""
Regression tests for database._insert_transactions()'s duplicate
detection (see that function's own docstring for the full story) -
run against the REAL dev database, same convention already used by
tests/test_screener_reconcile_job.py. All test rows use a throwaway
symbol so they can never collide with real trade data, and every test
cleans up its own rows even on failure.

The bug this locks in: a legacy (source=None) row re-synced later via
SnapTrade (source="snaptrade") at a very slightly different price used
to NEVER be recognized as a duplicate, because the old check required
BOTH sources to be truthy before allowing the fuzzy price tolerance -
a None source failed that check and fell through to requiring an exact
price match, which two independent platforms essentially never produce
for the same real fill. Confirmed causing 46 real duplicate
transactions (inflated open positions) in production before the fix.
"""
from datetime import datetime

import pytest

import database

SYMBOL = "__DEDUPTEST__"


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE symbol = %s", (SYMBOL,))
    conn.commit()


def test_none_source_vs_known_source_close_price_is_deduped(conn):
    """The actual production bug: a legacy (source=None) row and a
    later snaptrade-sourced row for the same real fill, prices a
    fraction of a cent apart - must be recognized as the same trade."""
    _cleanup(conn)
    try:
        first = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 100.00, "quantity": 50, "source": None,
        }])
        assert first == 1

        second = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 100.0296, "quantity": 50, "source": "snaptrade",
        }])
        assert second == 0, "should have been recognized as a duplicate of the None-source row"

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions WHERE symbol = %s", (SYMBOL,))
        assert cur.fetchone()[0] == 1
    finally:
        _cleanup(conn)


def test_both_none_source_close_but_different_price_not_deduped(conn):
    """The OLDER bug this must not reintroduce: two genuinely separate
    fills (both legacy/unknown source) a few cents apart must stay
    separate, not get merged into one."""
    _cleanup(conn)
    try:
        first = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "SELL",
            "price": 200.00, "quantity": 100, "source": None,
        }])
        assert first == 1

        second = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "SELL",
            "price": 200.05, "quantity": 100, "source": None,
        }])
        assert second == 1, "two separate same-source fills must NOT be merged"

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions WHERE symbol = %s", (SYMBOL,))
        assert cur.fetchone()[0] == 2
    finally:
        _cleanup(conn)


def test_known_different_sources_close_price_still_deduped(conn):
    """Existing behavior (csv vs snaptrade) must keep working."""
    _cleanup(conn)
    try:
        database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 50.00, "quantity": 10, "source": "csv",
        }])
        second = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 50.0049, "quantity": 10, "source": "snaptrade",
        }])
        assert second == 0
    finally:
        _cleanup(conn)


def test_same_known_source_requires_exact_price(conn):
    """Two snaptrade rows a few cents apart are genuinely separate
    fills, not the same trade reported twice - same protection as the
    both-None case, just with a known source on both sides."""
    _cleanup(conn)
    try:
        database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 75.00, "quantity": 20, "source": "snaptrade",
        }])
        second = database._insert_transactions(conn, [{
            "date": datetime(2024, 3, 1), "symbol": SYMBOL, "action": "BUY",
            "price": 75.03, "quantity": 20, "source": "snaptrade",
        }])
        assert second == 1
    finally:
        _cleanup(conn)
