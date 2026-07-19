"""
Nightly Archive
=====================
A fallback safety net: snapshots every currently-open position's chart
AND every manually-added Watchlist ticker's chart, archiving each -
together with whatever journal notes were written for today via the
Shortlist page - into that ticker's permanent Logbook. Afterward, also
sends the Daily Report PDF (see daily_report.py) if it hasn't already
been generated and emailed for today.

Both the chart archiving and the report-sending follow the same
pattern: the Shortlist page's Save button (for charts) and the
Logbook page's "Generate & Email Report" button (for the report)
already do this immediately when used - this script's job is really
just to catch whatever didn't get done by hand on a given day, not the
primary way either one happens.

NOT a Streamlit page - a plain script, meant to be run once a night by
a scheduled GitHub Actions workflow (see
.github/workflows/nightly_archive.yml), since Streamlit Community Cloud
has no scheduler of its own. Can also be run manually any time (e.g.
`python nightly_archive.py`) to archive today's snapshot on demand.
"""

from datetime import date, datetime

import charting
import daily_report
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


def send_daily_report_fallback(conn, today):
    """
    Generates and emails the Daily Report for `today` if nothing is
    already recorded for it in database.daily_reports (i.e. the
    Logbook page's "Generate & Email Report" button wasn't used today) -
    wrapped so a problem here (bad email secrets, an SMTP hiccup) never
    affects the chart-archiving work main() already did above it.
    """
    if database.get_daily_report_status(conn, today):
        print("Daily report already generated and emailed for today - skipping.")
        return

    print("Daily report not yet sent today - generating and emailing it now.")
    try:
        success, message = daily_report.generate_and_send_report(conn, today)
        print(f"  {message}")
    except Exception as exc:
        print(f"  Daily report failed unexpectedly: {exc}")


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

    send_daily_report_fallback(conn, today)

    print("Done.")


if __name__ == "__main__":
    main()
