"""
SnapTrade Daily Sync
=====================
Pulls fresh trade activity from your connected Fidelity account (see
snaptrade_sync.py) automatically, once a day, right after the market
closes - so the database stays current even on a day you never open
the app or click "Sync Now" on the Import Trades page.

This is a SEPARATE job from nightly_archive.py on purpose - that one
runs near midnight and handles chart archiving plus the Daily Report
email, an unrelated end-of-day housekeeping task. This job's timing
matters for a different reason (catching today's trades as soon as
SnapTrade actually has them), so it gets its own schedule instead of
being bundled in.

NOT a Streamlit page - a plain script, meant to be run by a scheduled
GitHub Actions workflow (see .github/workflows/snaptrade_sync.yml).
Can also be run manually any time (e.g. `python snaptrade_daily_sync.py`),
though outside the 4pm Eastern hour it'll just print a message and do
nothing - see main() below.

WHY THE HOUR CHECK: GitHub Actions cron schedules only run in UTC, and
4:01pm US Eastern is a DIFFERENT UTC time depending on whether Eastern
Daylight or Standard Time is in effect (see timeutil.py for why this
whole project reads the real Eastern clock instead of a fixed offset).
Rather than accept being up to an hour early/late the way
nightly_archive.py's fixed-UTC schedule does (fine for its fuzzy
"sometime around midnight" target), the workflow fires TWICE - once
for each possible UTC offset - and this script only actually does
anything during the 4pm Eastern hour, so exactly one of those two
firings ends up doing real work, regardless of the time of year.
"""

from datetime import timedelta

import database
import timeutil


def main():
    now = timeutil.now_eastern()
    if now.hour != 16:
        print(f"It's {now:%H:%M} Eastern, not the 4pm hour - skipping (this runs twice a "
              "day to cover both possible UTC offsets, see this file's own notes).")
        return

    conn = database.get_connection()
    end_date = now.date()
    # A week-wide window, not just "since yesterday" - cheap (one API
    # call per connected account) and safe against a missed or failed
    # run, since the shared insert helper already skips anything
    # that's already stored (see database._insert_transactions()).
    start_date = end_date - timedelta(days=7)

    new_count = database.import_transactions_snaptrade(conn, start_date, end_date)
    trade_count = database.rebuild_trades(conn)
    print(
        f"Imported {new_count} new transaction row(s) from SnapTrade. "
        f"{trade_count} completed stock trade(s) total in the database."
    )


if __name__ == "__main__":
    main()
