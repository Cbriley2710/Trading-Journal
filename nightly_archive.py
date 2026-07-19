"""
Nightly Archive
=====================
A fallback safety net: snapshots every currently-open position's chart
AND every manually-added Watchlist ticker's chart, archiving each -
together with whatever journal notes were written for today via the
Shortlist page - into that ticker's permanent Logbook.

The Shortlist page's Save button already does this immediately when
you save a journal entry, using the exact same
charting.build_archive_snapshot() this script calls - so this script's
job is really just to catch any ticker you didn't get around to saving
a journal entry for on a given day, not the primary way archiving
happens anymore.

NOT a Streamlit page - a plain script, meant to be run once a night by
a scheduled GitHub Actions workflow (see
.github/workflows/nightly_archive.yml), since Streamlit Community Cloud
has no scheduler of its own. Can also be run manually any time (e.g.
`python nightly_archive.py`) to archive today's snapshot on demand.
"""

from datetime import date, datetime

import charting
import database


def archive_ticker(conn, symbol, entry_date, buy_price, entry_label, today, as_of, direction="LONG"):
    """Builds and archives one ticker's chart snapshot for today. Returns
    True if it was archived, False if no price data was found."""
    png_bytes = charting.build_archive_snapshot(symbol, entry_date, buy_price, entry_label, as_of, direction=direction)
    if png_bytes is None:
        print(f"  {symbol}: no price data found, skipping.")
        return False

    database.upsert_logbook_entry(
        conn, symbol, today, chart_image=png_bytes, archived_at=datetime.now())
    print(f"  {symbol}: archived ({len(png_bytes)} bytes).")
    return True


def main():
    conn = database.get_connection()
    today = date.today()
    as_of = datetime.combine(today, datetime.min.time())

    positions = database.get_open_positions(conn)
    print(f"Found {len(positions)} open position(s) to archive.")
    archived_symbols = set()
    for position in positions:
        is_short = position["direction"] == "SHORT"
        archive_ticker(
            conn, position["symbol"], position["entry_date"], position["avg_price"],
            "Short Entry" if is_short else "Entry", today, as_of, direction=position["direction"])
        archived_symbols.add(position["symbol"])

    watchlist = database.get_watchlist(conn)
    # Skip anything already archived as an open position tonight, so a
    # ticker that happens to be both isn't processed twice with a less
    # meaningful "Added" marker overwriting the real "Entry" one.
    watchlist = [w for w in watchlist if w["symbol"] not in archived_symbols]
    print(f"Found {len(watchlist)} watchlist ticker(s) to archive.")
    for entry in watchlist:
        archive_ticker(conn, entry["symbol"], entry["added_at"], None, "Added", today, as_of)

    print("Done.")


if __name__ == "__main__":
    main()
