"""
SnapTrade Daily Sync
=====================
Pulls fresh trade activity from your connected Fidelity account (see
snaptrade_sync.py) automatically, so the database stays current even
on a day you never open the app or click "Sync Now" on the Import
Trades page.

This is a SEPARATE job from nightly_archive.py on purpose - that one
runs near midnight and handles chart archiving plus the Daily Report
email, an unrelated end-of-day housekeeping task.

NOT a Streamlit page - a plain script, meant to be run by a scheduled
GitHub Actions workflow (see .github/workflows/snaptrade_sync.yml).
Can also be run manually any time (e.g. `python snaptrade_daily_sync.py`).

NO HOUR CHECK ON PURPOSE (there used to be one - see git history if
curious): the original design tried to fire this workflow twice a day
(20:01 and 21:01 UTC, bracketing 4:01pm US Eastern across both EST and
EDT) and only do real work if the script's own clock read the 4pm
Eastern hour, rejecting the other firing as "the wrong one." In
practice, GitHub Actions' scheduled triggers can run HOURS late during
busy periods - confirmed here: two runs meant to land at 4:01pm ET
actually fired at 7:15pm and 8:09pm ET, so the hour check rejected
BOTH of them, and the sync silently never actually ran. Same trade-off
nightly_archive.py already accepts for its own schedule: just do the
work whenever the scheduler actually fires, however late that turns
out to be, rather than trying to reject "mistimed" runs - the
dedup-safe insert (see database._insert_transactions()) means running
this more than once, or later than planned, is always harmless.
"""

from datetime import timedelta

import database
import timeutil


def main():
    conn = database.get_connection()
    end_date = timeutil.today_eastern()
    # A week-wide window, not just "since yesterday" - cheap (one API
    # call per connected account) and safe against a missed or delayed
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
