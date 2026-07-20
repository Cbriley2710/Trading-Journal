"""
Archiving
=====================
Builds and saves one day's chart snapshot into a ticker's permanent
Logbook entry. Shared by nightly_archive.py (the scheduled nightly
job, which archives every open position and watchlist ticker) and
daily_report.py (so pressing "Generate & Email Report" for today
archives fresh charts for everything right now instead of only using
whatever's already there - doing everything the nightly job would
have done, on demand).
"""

from datetime import datetime

import charting
import database


def archive_ticker(conn, symbol, entry_date, buy_price, entry_label, today, as_of, direction="LONG", stop_loss=None):
    """Builds and archives one ticker's chart snapshot for today. Returns
    True if it was archived, False if no price data was found."""
    png_bytes = charting.build_archive_snapshot(
        symbol, entry_date, buy_price, entry_label, as_of, direction=direction, stop_loss=stop_loss)
    if png_bytes is None:
        print(f"  {symbol}: no price data found, skipping.")
        return False

    database.upsert_logbook_entry(
        conn, symbol, today, chart_image=png_bytes, archived_at=datetime.now())
    print(f"  {symbol}: archived ({len(png_bytes)} bytes).")
    return True


def archive_all(conn, today):
    """Archives every open position's and every watchlist ticker's chart
    for `today`. Returns the set of symbols archived as open positions,
    so a ticker that's both an open position and on a watchlist isn't
    processed twice with a less meaningful "Added" marker overwriting
    the real "Entry" one."""
    as_of = datetime.combine(today, datetime.min.time())

    positions = database.get_open_positions(conn)
    print(f"Found {len(positions)} open position(s) to archive.")
    archived_symbols = set()
    for position in positions:
        is_short = position["direction"] == "SHORT"
        archive_ticker(
            conn, position["symbol"], position["entry_date"], position["avg_price"],
            "Short Entry" if is_short else "Entry", today, as_of, direction=position["direction"],
            stop_loss=database.get_stop_loss(conn, position["symbol"]))
        archived_symbols.add(position["symbol"])

    watchlist = database.get_watchlist(conn)
    watchlist = [w for w in watchlist if w["symbol"] not in archived_symbols]
    print(f"Found {len(watchlist)} watchlist ticker(s) to archive.")
    for entry in watchlist:
        archive_ticker(conn, entry["symbol"], entry["added_at"], None, "Added", today, as_of)
        archived_symbols.add(entry["symbol"])

    return archived_symbols
