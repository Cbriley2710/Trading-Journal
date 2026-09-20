"""
Tests institutional_holdings.py's XML parsing (the part with no network
call involved) plus get_snapshot_for_ticker()'s New/Increased/Decreased/
Closed logic. The snapshot tests run against the REAL dev database (same
convention as tests/test_csv_upload_tracking.py) using a throwaway fund
name/CIK that can't collide with the real curated list - cleaned up via
ON DELETE CASCADE from hedge_funds down to hedge_fund_holdings.
"""
from datetime import date

import pytest

import database
import institutional_holdings

FUND_NAME = "__TEST FUND__"
FUND_CIK = "0000000001"
TICKER = "__TESTTICK__"

SAMPLE_INFOTABLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
    <infoTable>
        <nameOfIssuer>EQUINIX INC</nameOfIssuer>
        <titleOfClass>COM</titleOfClass>
        <cusip>29444U700</cusip>
        <value>156358500</value>
        <shrsOrPrnAmt>
            <sshPrnamt>150000</sshPrnamt>
            <sshPrnamtType>SH</sshPrnamtType>
        </shrsOrPrnAmt>
        <investmentDiscretion>SOLE</investmentDiscretion>
        <votingAuthority>
            <Sole>150000</Sole>
            <Shared>0</Shared>
            <None>0</None>
        </votingAuthority>
    </infoTable>
    <infoTable>
        <nameOfIssuer>ETSY INC</nameOfIssuer>
        <titleOfClass>COM</titleOfClass>
        <cusip>29786A106</cusip>
        <value>376650000</value>
        <shrsOrPrnAmt>
            <sshPrnamt>5000000</sshPrnamt>
            <sshPrnamtType>SH</sshPrnamtType>
        </shrsOrPrnAmt>
        <investmentDiscretion>SOLE</investmentDiscretion>
        <votingAuthority>
            <Sole>5000000</Sole>
            <Shared>0</Shared>
            <None>0</None>
        </votingAuthority>
    </infoTable>
</informationTable>
"""


def test_parse_infotable_xml_reads_cusip_shares_and_value_in_dollars():
    holdings = institutional_holdings.parse_infotable_xml(SAMPLE_INFOTABLE_XML)

    assert len(holdings) == 2
    equinix = next(h for h in holdings if h["cusip"] == "29444U700")
    assert equinix["issuer_name"] == "EQUINIX INC"
    assert equinix["shares"] == 150000
    # Current-schema 13F filings report <value> in whole dollars already -
    # 156358500 -> $156,358,500, NOT $156,358,500,000 (that earlier, wrong
    # assumption was only caught by sanity-checking an implied fund total
    # against real-world scale - see institutional_holdings.py's comment).
    assert equinix["value_usd"] == 156358500


def test_parse_infotable_xml_returns_empty_list_for_no_rows():
    empty_xml = b"""<?xml version="1.0"?>
    <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"></informationTable>
    """
    assert institutional_holdings.parse_infotable_xml(empty_xml) == []


FUND_CIK_2 = "0000000002"


@pytest.fixture
def conn():
    conn = database.get_connection()
    yield conn
    cur = conn.cursor()
    cur.execute("DELETE FROM hedge_funds WHERE sec_cik IN (%s, %s)", (FUND_CIK, FUND_CIK_2))
    conn.commit()


def _make_test_fund(conn):
    database.add_hedge_fund(conn, FUND_NAME, FUND_CIK)
    return next(f for f in database.get_hedge_funds(conn) if f["sec_cik"] == FUND_CIK)


def _save(conn, fund_id, quarter_end, filed_date, holdings, total=None):
    """Thin wrapper so most tests don't have to spell out a portfolio
    total they don't care about - defaults to "this fund's ONLY
    reported positions add up to 100% of it", which is exactly true
    when a test only lists the position(s) it's checking."""
    if total is None:
        total = sum(h["value_usd"] for h in holdings)
    database.save_fund_holdings(conn, fund_id, quarter_end, filed_date, holdings, total)


def test_snapshot_shows_increased_when_shares_go_up_between_quarters(conn):
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 3, 31), date(2026, 5, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 1000, "value_usd": 100000},
    ])
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 2000, "value_usd": 250000},
    ])

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, TICKER)
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["held"] is True
    assert row["shares"] == 2000
    assert row["change"] == "Increased"
    assert row["change_pct"] == 100.0  # 1000 -> 2000 shares is a +100% increase
    assert row["portfolio_pct"] == 100.0  # its only position that quarter


def test_snapshot_shows_closed_when_a_position_disappears(conn):
    # The fund still holds SOME other stock this quarter (a real 13F with
    # zero rows at all would never happen for a real fund) - it's TESTCUSIP1
    # specifically that's gone, which is what should register as "Closed".
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 3, 31), date(2026, 5, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 1000, "value_usd": 100000},
    ])
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP2", "issuer_name": "Other Co", "ticker": "__OTHERTICK__", "shares": 300, "value_usd": 9000},
    ])

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, TICKER)
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["held"] is False
    assert row["change"] == "Closed"
    assert row["change_pct"] == -100.0


def test_snapshot_shows_new_for_a_first_ever_position(conn):
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 500, "value_usd": 50000},
    ])

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, TICKER)
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["held"] is True
    assert row["change"] == "New"
    assert row["change_pct"] is None  # can't quantify a % increase from zero


def test_snapshot_not_held_when_fund_has_no_data_at_all(conn):
    _make_test_fund(conn)

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, "__NOSUCHTICKER__")
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["held"] is False
    assert row["change"] is None


def test_portfolio_pct_reflects_a_smaller_share_of_a_bigger_fund(conn):
    fund = _make_test_fund(conn)
    # This position is worth $100,000, but the fund's TOTAL reported value
    # that quarter was $1,000,000 - so it's 10% of the portfolio, not 100%.
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 500, "value_usd": 100000},
    ], total=1000000)

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, TICKER)
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["portfolio_pct"] == 10.0


def test_snapshot_flags_a_fund_as_stale_when_a_peer_has_a_newer_quarter(conn):
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 3, 31), date(2026, 5, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 500, "value_usd": 50000},
    ])
    # A second, real curated fund reports a NEWER quarter than our test
    # fund - this is what should make the test fund's row read "stale".
    other = _make_second_test_fund(conn)
    _save(conn, other["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP2", "issuer_name": "Other Co", "ticker": "__OTHERTICK__", "shares": 10, "value_usd": 1000},
    ])

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, TICKER)
    row = next(r for r in snapshot if r["fund"] == FUND_NAME)

    assert row["stale"] is True


def _make_second_test_fund(conn):
    database.add_hedge_fund(conn, "__TEST FUND 2__", FUND_CIK_2)
    return next(f for f in database.get_hedge_funds(conn) if f["sec_cik"] == FUND_CIK_2)


def test_recent_moves_excludes_funds_with_only_one_quarter_of_data(conn):
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Test Co", "ticker": TICKER, "shares": 500, "value_usd": 50000},
    ])

    moves = institutional_holdings.get_recent_moves(conn)

    assert not any(m["fund"] == FUND_NAME for m in moves)


def test_recent_moves_includes_a_real_change_but_not_an_unchanged_position(conn):
    fund = _make_test_fund(conn)
    _save(conn, fund["id"], date(2026, 3, 31), date(2026, 5, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Moved Co", "ticker": TICKER, "shares": 1000, "value_usd": 100000},
        {"cusip": "TESTCUSIP2", "issuer_name": "Steady Co", "ticker": "__STEADYTICK__", "shares": 200, "value_usd": 20000},
    ], total=1000000)
    _save(conn, fund["id"], date(2026, 6, 30), date(2026, 8, 1), [
        {"cusip": "TESTCUSIP1", "issuer_name": "Moved Co", "ticker": TICKER, "shares": 1500, "value_usd": 150000},
        {"cusip": "TESTCUSIP2", "issuer_name": "Steady Co", "ticker": "__STEADYTICK__", "shares": 200, "value_usd": 20000},
    ], total=1000000)

    moves = institutional_holdings.get_recent_moves(conn)
    fund_moves = [m for m in moves if m["fund"] == FUND_NAME]

    assert len(fund_moves) == 1  # the steady, unchanged position is left out
    assert fund_moves[0]["ticker"] == TICKER
    assert fund_moves[0]["change"] == "Increased"
    assert fund_moves[0]["change_pct"] == 50.0
