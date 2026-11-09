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

Also warms each symbol's next earnings date into earnings_cache (see
charting.warm_earnings_cache_for_symbol()), which feeds the Journal
Session's "earnings within 10 days" status-bar badge.

WHY IT RUNS EARLY (~4:30pm ET): so by the time you actually sit down to
journal, every ticker's chart data is already warm - the FIRST view of
each ticker each day, which used to always pay for a live Yahoo Finance
fetch, now usually doesn't.

Also does the cache's cleanup step once warming is done: any cached
history not tagged with the current expected trading day (a day's
now-stale cache, or a symbol no longer tracked at all) is deleted - see
database.clear_stale_price_cache().
"""

import time

import charting
import database
import timeutil

# A short pause between each symbol's fetch, so warming the cache for
# every tracked symbol back-to-back doesn't read as a burst of rapid
# requests to Yahoo Finance and trip its rate limiting - which would
# otherwise fail the whole batch at once instead of just one symbol.
PAUSE_BETWEEN_SYMBOLS_SECONDS = 1


def main():
    conn = database.get_connection()
    symbols = database.get_tracked_symbols(conn)
    print(f"Warming price cache for {len(symbols)} symbol(s)...")
    for symbol in sorted(symbols):
        result = charting.warm_price_cache_for_symbol(symbol)
        print(f"  {symbol}: {result}")
        time.sleep(PAUSE_BETWEEN_SYMBOLS_SECONDS)
        earnings_result = charting.warm_earnings_cache_for_symbol(symbol)
        print(f"  {symbol} earnings: {earnings_result}")
        time.sleep(PAUSE_BETWEEN_SYMBOLS_SECONDS)
    # Uses expected_last_trading_day(), not today's calendar date - see
    # clear_stale_price_cache()'s own docstring for why comparing against
    # the literal calendar day wiped out a perfectly good weekend cache.
    database.clear_stale_price_cache(conn, timeutil.expected_last_trading_day())
    print("Done.")


if __name__ == "__main__":
    main()
