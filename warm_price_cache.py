"""
Warm Price Cache
=====================
Pre-fetches daily price history for every currently-tracked symbol
(every open position, every watchlist ticker - see
database.get_tracked_symbols()) into the persistent price_cache table
(see charting.warm_price_cache_for_symbol()) - run once a day, shortly
after market close, by a scheduled GitHub Actions workflow (see
.github/workflows/warm_price_cache.yml). Can also be run manually any
time (e.g. `python warm_price_cache.py`) to warm the cache on demand.

WHY THIS EXISTS, SEPARATE FROM nightly_archive.py: that job runs near
midnight (not right after close - see its own workflow's comment on
cron/DST), which is often already well into, or past, a typical evening
Journal Session. This runs earlier (~4:30pm ET), so by the time you
actually sit down to journal, every ticker's chart data is already
warm - the FIRST view of each ticker each day, which used to always
pay for a live Yahoo Finance fetch, now usually doesn't.

Deliberately does NOT also archive a Logbook snapshot or send the
Daily Report - nightly_archive.py already does that (near midnight,
capturing the full day's final close). Building a chart PNG (see
charting.render_png()) is real, comparatively expensive work; doing it
again here too, a few hours before nightly_archive.py does it anyway,
would just be duplicate effort for no benefit.
"""

import charting
import database


def main():
    conn = database.get_connection()
    symbols = database.get_tracked_symbols(conn)
    print(f"Warming price cache for {len(symbols)} symbol(s)...")
    for symbol in sorted(symbols):
        cached = charting.warm_price_cache_for_symbol(symbol)
        print(f"  {symbol}: {'cached' if cached else 'no price data found, skipped'}.")
    print("Done.")


if __name__ == "__main__":
    main()
