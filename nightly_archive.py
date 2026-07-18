"""
Nightly Archive
=====================
Snapshots every currently-open position's chart AND every manually-added
Watchlist ticker's chart, archiving each - together with whatever journal
notes were written for today via the Shortlist page - into that ticker's
permanent Logbook.

NOT a Streamlit page - a plain script, meant to be run once a night by
a scheduled GitHub Actions workflow (see
.github/workflows/nightly_archive.yml), since Streamlit Community Cloud
has no scheduler of its own. Can also be run manually any time (e.g.
`python nightly_archive.py`) to archive today's snapshot on demand.

Uses a fixed default chart (charting.DEFAULT_SETTINGS - a plain
candlestick with 20/50-period moving averages) since there's no
interactive user here to pull custom settings from; the interactive
Chart Settings toolbar on Trade Analyzer/Shortlist is a separate,
unrelated view of the same underlying price data.
"""

from datetime import date, datetime, timedelta

import charting
import database

# A fixed, generous lookback so every archived snapshot shows real
# context around the position, not just the last day or two.
PADDING_DAYS = 30


def archive_ticker(conn, symbol, entry_date, buy_price, entry_label, today, display_end):
    """Builds and archives one ticker's chart snapshot for today. Returns
    True if it was archived, False if no price data was found."""
    entry_point = {"entry_date": entry_date, "buy_price": buy_price} if buy_price is not None \
        else {"entry_date": entry_date}

    display_start = entry_date - timedelta(days=PADDING_DAYS)
    max_ma_period = max(charting.DEFAULT_SETTINGS["ma_periods"], default=0)
    lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD["1d"]
    fetch_start = display_start - timedelta(days=lookback_days)

    history = charting.fetch_history(
        symbol, fetch_start, display_start, display_end, "1d",
        charting.DEFAULT_SETTINGS["ma_periods"])

    if history.empty:
        print(f"  {symbol}: no price data found, skipping.")
        return False

    if "buy_price" not in entry_point:
        entry_point = dict(entry_point, buy_price=charting.price_near_date(history, entry_date))

    fig = charting.build_figure(
        symbol, history, entry_point, charting.DEFAULT_SETTINGS, entry_label=entry_label)
    png_bytes = fig.to_image(format="png")

    database.upsert_logbook_entry(
        conn, symbol, today, chart_image=png_bytes, archived_at=datetime.now())
    print(f"  {symbol}: archived ({len(png_bytes)} bytes).")
    return True


def main():
    conn = database.get_connection()
    today = date.today()
    display_end = datetime.combine(today, datetime.min.time()) + timedelta(days=1)

    positions = database.get_open_positions(conn)
    print(f"Found {len(positions)} open position(s) to archive.")
    archived_symbols = set()
    for position in positions:
        archive_ticker(
            conn, position["symbol"], position["entry_date"], position["avg_price"],
            "Entry", today, display_end)
        archived_symbols.add(position["symbol"])

    watchlist = database.get_watchlist(conn)
    # Skip anything already archived as an open position tonight, so a
    # ticker that happens to be both isn't processed twice with a less
    # meaningful "Added" marker overwriting the real "Entry" one.
    watchlist = [w for w in watchlist if w["symbol"] not in archived_symbols]
    print(f"Found {len(watchlist)} watchlist ticker(s) to archive.")
    for entry in watchlist:
        archive_ticker(conn, entry["symbol"], entry["added_at"], None, "Added", today, display_end)

    print("Done.")


if __name__ == "__main__":
    main()
