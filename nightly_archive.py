"""
Nightly Archive
=====================
Snapshots every currently-open position's chart and archives it -
together with whatever journal notes were written for today via the
Shortlist page - into that ticker's permanent Logbook.

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


def main():
    conn = database.get_connection()
    positions = database.get_open_positions(conn)
    today = date.today()
    display_end = datetime.combine(today, datetime.min.time()) + timedelta(days=1)

    print(f"Found {len(positions)} open position(s) to archive.")

    for position in positions:
        symbol = position["symbol"]
        entry_point = {"entry_date": position["entry_date"], "buy_price": position["avg_price"]}

        display_start = position["entry_date"] - timedelta(days=PADDING_DAYS)
        max_ma_period = max(charting.DEFAULT_SETTINGS["ma_periods"], default=0)
        lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD["1d"]
        fetch_start = display_start - timedelta(days=lookback_days)

        history = charting.fetch_history(
            symbol, fetch_start, display_start, display_end, "1d",
            charting.DEFAULT_SETTINGS["ma_periods"])

        if history.empty:
            print(f"  {symbol}: no price data found, skipping.")
            continue

        fig = charting.build_figure(symbol, history, entry_point, charting.DEFAULT_SETTINGS)
        png_bytes = fig.to_image(format="png")

        database.upsert_logbook_entry(
            conn, symbol, today, chart_image=png_bytes, archived_at=datetime.now())
        print(f"  {symbol}: archived ({len(png_bytes)} bytes).")

    print("Done.")


if __name__ == "__main__":
    main()
