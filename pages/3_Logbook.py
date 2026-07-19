"""
Logbook
=====================
The permanent, day-by-day archive behind every ticker that's ever been
on your Shortlist - review exactly how a trade's chart looked and what
you were thinking, one day at a time, even long after the trade has
closed.

Each day's entry is written here by nightly_archive.py (a chart
snapshot + whatever you wrote in that day's Shortlist journal box) -
see database.get_logbook_entries(). A day with no notes or no archived
image yet (e.g. today, before tonight's archive run) still shows up,
just with whichever piece is missing left blank.

This page also has a "Daily Report" section - one PDF covering every
list, emailed to a mailing list. See daily_report.py. If you don't
click Generate yourself, nightly_archive.py sends it automatically as
a fallback once the night's archiving is done, the same "manual action
is primary, the nightly script is the fallback" pattern already used
for per-ticker chart archiving.
"""

from datetime import date

import streamlit as st

import auth
import daily_report
import database
import nav

st.set_page_config(page_title="Logbook", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Logbook")

st.title("Logbook")

conn = database.get_connection()

st.header("Daily Report")
st.caption(
    "One PDF covering every list (each ticker's archived chart + notes for "
    "the day you pick), emailed to your configured recipients. Choosing "
    "today's date first archives a fresh chart for every open position and "
    "watchlist ticker, so the report doesn't depend on tonight's automated "
    "archive run having happened yet. If you don't generate it yourself, "
    "that automated run sends it for you as a fallback."
)
report_cols = st.columns([1, 2])
report_date = report_cols[0].date_input("Report date", value=date.today(), key="report_date")

already_sent_at = database.get_daily_report_status(conn, report_date)
if already_sent_at:
    report_cols[1].caption(f"Already generated and emailed for this date, at {already_sent_at:%I:%M %p}.")
else:
    report_cols[1].caption("Not generated yet for this date.")

if st.button("Generate & Email Report"):
    is_today = report_date == date.today()
    spinner_text = (
        "Archiving fresh charts and building/sending the report..." if is_today
        else "Building the PDF and sending it..."
    )
    with st.spinner(spinner_text):
        success, message = daily_report.generate_and_send_report(conn, report_date, archive_first=is_today)
    if success:
        st.success(message)
    else:
        st.error(message)

st.divider()

symbols = database.get_logbook_symbols(conn)

if not symbols:
    st.info(
        "No logbook entries yet. Write a journal entry for an open "
        "position on the Shortlist page, then check back after tonight's "
        "automated archive run."
    )
    st.stop()

symbol = st.selectbox("Choose a ticker", options=symbols)
entries = database.get_logbook_entries(conn, symbol)

st.caption(f"{len(entries)} day(s) logged for {symbol}.")

for entry in entries:
    st.subheader(f"{entry['entry_date']:%A, %B %d, %Y}")

    if entry["chart_image"]:
        st.image(entry["chart_image"], width="stretch")
    else:
        st.caption("No chart archived for this day yet - archives happen overnight.")

    st.write(entry["notes"] if entry["notes"] else "_No notes recorded for this day._")
    st.divider()
