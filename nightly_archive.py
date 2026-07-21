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

NOT a Streamlit page - a plain script, meant to be run once a night by
a scheduled GitHub Actions workflow (see
.github/workflows/nightly_archive.yml), since Streamlit Community Cloud
has no scheduler of its own. Can also be run manually any time (e.g.
`python nightly_archive.py`) to archive today's snapshot on demand.
"""

import archiving
import daily_report
import database
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
    today = timeutil.today_eastern()

    archiving.archive_all(conn, today)
    send_daily_report_fallback(conn, today)

    print("Done.")


if __name__ == "__main__":
    main()
