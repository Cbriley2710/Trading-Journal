"""
Nightly Archive
=====================
A fallback safety net: snapshots every currently-open position's chart
AND every manually-added Watchlist ticker's chart, archiving each -
together with whatever journal notes were written for today via the
Shortlist page - into that ticker's permanent Logbook (see
archiving.py). Afterward, also sends the Daily Report PDF (see
daily_report.py) if it hasn't already been generated and emailed for
today.

Both the chart archiving and the report-sending follow the same
pattern: the Shortlist page's Save button (for charts) and the
Logbook page's "Generate & Email Report" button (for the report -
which itself re-archives everything for today too, see
daily_report.generate_and_send_report()) already do this immediately
when used - this script's job is really just to catch whatever didn't
get done by hand on a given day, not the primary way either one
happens.

Also runs symbol_archive_report.py's storage-management pass - PDFing,
emailing, and deleting the Logbook of any watchlist-only ticker that's
been removed from every watchlist for over a week (real trades are
never touched) - this one has no manual on-demand equivalent, it's
purely automatic.

NOT a Streamlit page - a plain script, meant to be run once a night by
a scheduled GitHub Actions workflow (see
.github/workflows/nightly_archive.yml), since Streamlit Community Cloud
has no scheduler of its own. Can also be run manually any time (e.g.
`python nightly_archive.py`) to archive today's snapshot on demand.
"""

import archiving
import daily_report
import database
import symbol_archive_report
import timeutil


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
    # expected_last_trading_day(), NOT today_eastern() - this job's own
    # GitHub Actions cron fires ~midnight-to-1am Eastern, which on a
    # plain weekday has already rolled over into a brand new calendar
    # day that hasn't traded yet. today_eastern() here used to archive
    # (and email the Daily Report for) that not-yet-started day instead
    # of the trading day that actually just closed - see
    # expected_last_trading_day()'s own docstring for the full story.
    today = timeutil.expected_last_trading_day()

    # If `today` resolved to literally today (any time from 9:30am
    # Eastern on - see expected_last_trading_day()), that does NOT mean
    # today's session is actually over: GitHub Actions' scheduling
    # delay for this job is unpredictable enough that it's landed well
    # into market hours before (10:48am ET one day, confirmed in
    # production) - see timeutil.today_market_has_closed()'s own
    # docstring for the real bug this caused (every tracked ticker
    # archived using that morning's still-forming candle as if it were
    # final). Skipping here just means there's nothing new to do YET -
    # a later run (hopefully after close) picks it up instead.
    if today == timeutil.today_eastern() and not timeutil.today_market_has_closed():
        print(
            f"Today's session hasn't closed yet ({timeutil.now_eastern():%I:%M %p} ET) - "
            "nothing new to archive, skipping until a later run."
        )
    else:
        # archive_all() already guards each individual ticker (see
        # archiving.py) - this outer try/except is a second layer, for
        # anything that could fail before/between those per-ticker guards
        # (e.g. the initial get_open_positions()/get_watchlist() calls),
        # so send_daily_report_fallback() below always gets a chance to run
        # even in that case.
        try:
            # skip_if_already_archived=True: this job runs every night
            # regardless of whether today ended up being a new trading day
            # (weekends included) - once a trading day's charts are already
            # correctly archived, a later run targeting that SAME day (e.g.
            # Saturday and Sunday night both still resolve to Friday - see
            # expected_last_trading_day()) has nothing new to do and would
            # just be spending Yahoo Finance calls/render time re-creating
            # an identical image.
            archiving.archive_all(conn, today, skip_if_already_archived=True)
        except Exception as exc:
            print(f"Chart archiving failed unexpectedly: {exc}")

    send_daily_report_fallback(conn, today)

    try:
        symbol_archive_report.archive_and_delete_stale_watchlist_symbols(conn)
    except Exception as exc:
        print(f"Stale-watchlist archiving failed unexpectedly: {exc}")

    # The "discard after midnight" half of the price cache's lifecycle
    # (see database.clear_stale_price_cache() and warm_price_cache.py) -
    # run last, once today's real archiving work is done, so a problem
    # here never affects that. Uses expected_last_trading_day(), not
    # `today` above - see clear_stale_price_cache()'s own docstring for
    # why comparing against the literal calendar day was wiping out a
    # perfectly good weekend cache.
    database.clear_stale_price_cache(conn, timeutil.expected_last_trading_day())

    print("Done.")


if __name__ == "__main__":
    main()
