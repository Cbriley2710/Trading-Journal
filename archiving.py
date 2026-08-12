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

import time
from datetime import datetime

import charting
import database
import timeutil

# A short pause after each ticker that actually needed a fresh chart
# (skipped when skip_if_already_archived already short-circuited it -
# that path makes no Yahoo Finance call at all, so there's nothing to
# pace), matching warm_price_cache.py's own PAUSE_BETWEEN_SYMBOLS_
# SECONDS pattern for the same reason: nightly_archive.yml runs this
# whole module TWICE back-to-back in one job (the user's own account,
# then immediately the friend's), with no gap between them - a real
# incident where the user's own run left Yahoo Finance's rate limit
# still cooling down going into the friend's run, and every one of the
# friend's charts silently failed to archive that night as a result
# (see charting.fetch_history()'s own note on the matching retry fix).
PAUSE_BETWEEN_TICKERS_SECONDS = 1


def archive_ticker(conn, symbol, entry_date, buy_price, entry_label, today, as_of, direction="LONG", stop_loss=None):
    """Builds and archives one ticker's chart snapshot for today. Returns
    True if it was archived, False if no price data was found."""
    png_bytes = charting.build_archive_snapshot(
        symbol, entry_date, buy_price, entry_label, as_of, direction=direction, stop_loss=stop_loss)
    if png_bytes is None:
        print(f"  {symbol}: no price data found, skipping.")
        return False

    database.upsert_logbook_entry(
        conn, symbol, today, chart_image=png_bytes, archived_at=timeutil.now_eastern())
    print(f"  {symbol}: archived ({len(png_bytes)} bytes).")
    return True


def archive_all(conn, today, skip_if_already_archived=False):
    """Archives every open position's and every watchlist ticker's chart
    for `today`. Returns the set of symbols archived as open positions,
    so a ticker that's both an open position and on a watchlist isn't
    processed twice with a less meaningful "Added" marker overwriting
    the real "Entry" one.

    `skip_if_already_archived`, if True, skips a ticker entirely (no
    Yahoo Finance call, no chart render) when it already has a chart
    image saved for `today` - used by nightly_archive.py, which reruns
    every night regardless of whether `today` (see
    timeutil.expected_last_trading_day()) actually advanced to a new
    trading day since its last run (e.g. Saturday and Sunday night both
    still resolve to Friday). daily_report.py's on-demand "Generate &
    Email Report" button deliberately leaves this False - pressing that
    button means "get me fresh charts right now," even if today was
    already archived earlier.
    """
    as_of = datetime.combine(today, datetime.min.time())

    positions = database.get_open_positions(conn)
    print(f"Found {len(positions)} open position(s) to archive.")
    archived_symbols = set()
    for position in positions:
        symbol = position["symbol"]
        if skip_if_already_archived and database.has_logbook_chart_image(conn, symbol, today):
            print(f"  {symbol}: already archived for {today}, skipping.")
            archived_symbols.add(symbol)
            continue
        # Wrapped per-ticker (not around the whole loop) so one bad/
        # delisted symbol only skips itself - it used to be possible for
        # a single failure here to abort archiving every OTHER position
        # too, and skip send_daily_report_fallback() right along with it,
        # since nothing below this function ran until it returned.
        try:
            is_short = position["direction"] == "SHORT"
            archive_ticker(
                conn, symbol, position["entry_date"], position["avg_price"],
                "Short Entry" if is_short else "Entry", today, as_of, direction=position["direction"],
                stop_loss=database.get_stop_loss(conn, symbol))
        except Exception as exc:
            print(f"  {symbol}: archiving failed unexpectedly ({exc}), skipping.")
        archived_symbols.add(symbol)
        time.sleep(PAUSE_BETWEEN_TICKERS_SECONDS)

    watchlist = database.get_watchlist(conn)
    watchlist = [w for w in watchlist if w["symbol"] not in archived_symbols]
    print(f"Found {len(watchlist)} watchlist ticker(s) to archive.")
    for entry in watchlist:
        symbol = entry["symbol"]
        if skip_if_already_archived and database.has_logbook_chart_image(conn, symbol, today):
            print(f"  {symbol}: already archived for {today}, skipping.")
            archived_symbols.add(symbol)
            continue
        try:
            archive_ticker(conn, symbol, entry["added_at"], None, "Added", today, as_of)
        except Exception as exc:
            print(f"  {symbol}: archiving failed unexpectedly ({exc}), skipping.")
        archived_symbols.add(symbol)
        time.sleep(PAUSE_BETWEEN_TICKERS_SECONDS)

    return archived_symbols
