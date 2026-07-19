"""
Import Trades
=====================
A browser-based alternative to running import_trades.py from a terminal -
export your trade history from Fidelity or Schwab, then drop the CSV
file here instead of running the script from a terminal. Useful for
keeping the shortlist current on a day you don't have this PC open.

Which brokerage exported the file is auto-detected from its header row
(see analyze_trades.detect_csv_source()) - you never have to say which
one it is.

IMPORTANT DISTINCTION: this only updates the shared database (the same
one the Dashboard, Trade Analyzer, Shortlist, and Logbook pages all read
from) - it does NOT touch your local Trade Tracker Template 2026.xlsx
file, since that file only exists on this PC and this page might be
opened from your phone or any other device reaching the hosted app.
Running import_trades.py locally is still what updates both the
database AND that Excel file - use this page just to keep the database
(and everything built on top of it) current from anywhere.
"""

import tempfile
from pathlib import Path

import streamlit as st

import auth
import database
import nav

st.set_page_config(page_title="Import Trades", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Import Trades")

st.title("Import Trades")

st.write(
    "Export your trade history from Fidelity (the single-account export) "
    "or Schwab (Transaction History), then drop the CSV file below - the "
    "brokerage is detected automatically."
)
st.caption(
    "Note: this updates the shared database only - it won't touch your "
    "local Trade Tracker Template 2026.xlsx file, since that file only "
    "exists on your PC. Run import_trades.py locally if you also want "
    "the Excel tracker updated."
)

uploaded_file = st.file_uploader("Fidelity or Schwab CSV export", type="csv")

if uploaded_file is not None:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = Path(tmp.name)

    try:
        conn = database.get_connection()
        with st.spinner("Importing transactions..."):
            new_count = database.import_transactions(conn, tmp_path)
            trade_count = database.rebuild_trades(conn)
    finally:
        tmp_path.unlink(missing_ok=True)

    st.success(
        f"Imported {new_count} new transaction row(s). "
        f"{trade_count} completed stock trade(s) total in the database."
    )
