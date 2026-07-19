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
"""

import streamlit as st

import auth
import database
import nav

st.set_page_config(page_title="Logbook", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Logbook")

st.title("Logbook")

conn = database.get_connection()
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
