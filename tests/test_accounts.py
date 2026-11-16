"""
Tests for per-account tracking (see database.py's UNASSIGNED note and
_insert_transactions()'s ACCOUNTS note) - run against the REAL dev
database like tests/test_transaction_dedup.py, using a throwaway symbol
and fake account numbers, with every test cleaning up its own rows.
"""
from datetime import datetime

import pytest

import database

SYMBOL = "__ACCTTEST__"
ACCT_A, ACCT_B = "TESTACCT_A", "TESTACCT_B"


@pytest.fixture
def conn():
    return database.get_connection()


def _cleanup(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE symbol = %s", (SYMBOL,))
    cur.execute("DELETE FROM trades WHERE symbol = %s", (SYMBOL,))
    conn.commit()


def _tx(action, price, qty, account, day=1):
    return {"date": datetime(2024, 3, day), "symbol": SYMBOL, "action": action,
            "price": price, "quantity": qty, "source": "csv", "account": account}


def test_same_fill_in_two_accounts_is_kept_as_two_rows(conn):
    _cleanup(conn)
    try:
        assert database._insert_transactions(conn, [_tx("BUY", 10.0, 5, ACCT_A), _tx("BUY", 10.0, 5, ACCT_B)]) == 2
        # re-importing the same file adds nothing
        assert database._insert_transactions(conn, [_tx("BUY", 10.0, 5, ACCT_A), _tx("BUY", 10.0, 5, ACCT_B)]) == 0
    finally:
        _cleanup(conn)


def test_unassigned_row_is_claimed_by_a_matching_account_row(conn):
    _cleanup(conn)
    try:
        assert database._insert_transactions(conn, [_tx("BUY", 10.0, 5, None)]) == 1
        stats = {}
        assert database._insert_transactions(conn, [_tx("BUY", 10.0, 5, ACCT_A)], stats) == 0
        assert stats["adopted"] == 1
        cur = conn.cursor()
        cur.execute("SELECT account FROM transactions WHERE symbol = %s", (SYMBOL,))
        assert cur.fetchall() == [(ACCT_A,)]
    finally:
        _cleanup(conn)


def test_half_cent_price_difference_is_the_same_fill(conn):
    """The ACMR case: a legacy 55.095 and the CSV's 55.10."""
    _cleanup(conn)
    try:
        legacy = _tx("BUY", 55.095, 200, None)
        legacy["source"] = None
        assert database._insert_transactions(conn, [legacy]) == 1
        assert database._insert_transactions(conn, [_tx("BUY", 55.10, 200, ACCT_A)]) == 0
    finally:
        _cleanup(conn)


def test_a_sell_in_one_account_never_closes_a_buy_in_another(conn):
    _cleanup(conn)
    try:
        database._insert_transactions(conn, [_tx("BUY", 10.0, 5, ACCT_A, day=1), _tx("SELL", 12.0, 5, ACCT_B, day=2)])
        database.rebuild_trades(conn)
        assert [t for t in database.get_trades(conn, ACCT_A) if t["symbol"] == SYMBOL] == []
        assert [t for t in database.get_trades(conn, ACCT_B) if t["symbol"] == SYMBOL] == []
        # ...but each account's own buy then sell does match
        database._insert_transactions(conn, [_tx("SELL", 13.0, 5, ACCT_A, day=3)])
        database.rebuild_trades(conn)
        mine = [t for t in database.get_trades(conn, ACCT_A) if t["symbol"] == SYMBOL]
        assert len(mine) == 1 and mine[0]["profit_loss"] == 15.0 and mine[0]["account"] == ACCT_A
        assert len([t for t in database.get_trades(conn) if t["symbol"] == SYMBOL]) == 1
    finally:
        _cleanup(conn)
        database.rebuild_trades(conn)


def test_open_position_merges_across_accounts_only_in_the_all_view():
    positions = [
        {"symbol": "X", "direction": "LONG", "quantity": 10.0, "avg_price": 10.0,
         "entry_date": datetime(2024, 1, 2), "account": "A"},
        {"symbol": "X", "direction": "LONG", "quantity": 30.0, "avg_price": 20.0,
         "entry_date": datetime(2024, 1, 1), "account": "B"},
    ]
    merged = database._merge_positions_across_accounts(positions)
    assert len(merged) == 1
    assert merged[0]["quantity"] == 40.0 and merged[0]["avg_price"] == 17.5
    assert merged[0]["entry_date"] == datetime(2024, 1, 1) and merged[0]["account"] is None


def test_load_deposits_reads_wires_and_transfers_only(tmp_path):
    import analyze_trades
    csv_file = tmp_path / "x.csv"
    csv_file.write_text(
        "\n"
        "Run Date,Account,Account Number,Action,Symbol,Amount ($)\n"
        '01/09/2026,PRE-TAX,Z1,WIRE TRANSFER FROM BANK (Cash),,10000\n'
        '01/05/2026,Individual,Z2,WIRE TRANSFER TO BANK (Cash),,-60228.48\n'
        '04/09/2026,Individual,Z2,Electronic Funds Transfer Received (Cash),,15000\n'
        '04/10/2026,Individual,Z2,YOU BOUGHT ACME (ACME) (Cash),ACME,-500\n'
        '04/11/2026,Individual,Z2,DIVIDEND RECEIVED ACME (Cash),ACME,12\n',
        encoding="utf-8",
    )
    deposits = analyze_trades.load_deposits(csv_file)
    assert [(d["account"], d["amount"]) for d in deposits] == [("Z1", 10000.0), ("Z2", -60228.48), ("Z2", 15000.0)]
