"""
Import Trades
=====================
Two ways to get trade history into the shared database (the same one
the Dashboard, Trade Analyzer, Shortlist, and Logbook pages all read
from):

  1. Connect Fidelity once through SnapTrade, then hit "Sync Now"
     whenever you want fresh data (or just wait - snaptrade_daily_sync.py
     also runs this automatically every day after market close). See
     snaptrade_sync.py for how the connection itself works.
  2. Export a CSV from Fidelity or Schwab and drop it below - a
     browser-based alternative to running import_trades.py from a
     terminal, useful for keeping things current on a day you don't
     have this PC open. Which brokerage exported the file is
     auto-detected from its header row (see analyze_trades.
     detect_csv_source()) - you never have to say which one it is.

Both write to the same `transactions` table and are duplicate-safe
against each other (see database._insert_transactions()), so there's
no harm in using both - SnapTrade doesn't replace the CSV path, it's
just less manual for the account it's connected to.

IMPORTANT DISTINCTION: neither of these touches your local Trade
Tracker Template 2026.xlsx file, since that file only exists on this
PC and this page might be opened from your phone or any other device
reaching the hosted app. Running import_trades.py locally is still
what updates both the database AND that Excel file.
"""

import tempfile
from datetime import timedelta
from pathlib import Path

import streamlit as st

import auth
import database
import nav
import snaptrade_sync
import timeutil

st.set_page_config(page_title="Import Trades", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Import Trades")

st.title("Import Trades")

st.subheader("Connect Fidelity (SnapTrade)")
st.caption(
    "Pulls trade history straight from Fidelity automatically, once "
    "connected - no manual export needed for that account. Connected "
    "read-only, on purpose: this can never place a trade or move "
    "money, even if it wanted to. SnapTrade refreshes trade history "
    "about once a day, so a trade from today typically won't show up "
    "here until tomorrow."
)

if not snaptrade_sync.is_configured():
    # Stage 1: the app's own SnapTrade API key isn't set up at all yet -
    # nothing else below can work without it.
    st.info(
        "SnapTrade isn't set up yet - add SNAPTRADE_CLIENT_ID and "
        "SNAPTRADE_CONSUMER_KEY to .streamlit/secrets.toml (see "
        "secrets.toml.example) to enable this."
    )
elif not snaptrade_sync.is_registered():
    # Stage 2: one-time step, run once ever - see snaptrade_sync.
    # register_user() for why this is needed even with a Personal API
    # key. The returned secret is only ever shown this once, so it's
    # displayed directly here for you to copy - same as any other
    # "shown once at creation" credential.
    st.warning("One more one-time step before you can connect Fidelity: register your SnapTrade user.")
    if st.button("Register with SnapTrade"):
        try:
            user_secret = snaptrade_sync.register_user()
        except Exception as e:
            st.error(f"Registration failed: {e}")
        else:
            st.success("Registered. Copy this value now - SnapTrade will not show it again:")
            st.code(user_secret)
            st.caption(
                "Save it as SNAPTRADE_USER_SECRET alongside your other "
                "SnapTrade secrets (local .streamlit/secrets.toml, this "
                "app's Streamlit Cloud secrets, and your GitHub repo "
                "secrets), then reload this page."
            )
else:
    # Stage 3: fully set up - show what's connected (if anything) and
    # the Connect/Sync buttons.
    try:
        connected_accounts = snaptrade_sync.list_connected_accounts()
    except Exception as e:
        # Most commonly because nothing's been connected yet at all (a
        # brand new SnapTrade account can error here instead of just
        # returning an empty list). Treat this as "no accounts", NOT as
        # "hide everything" - the Connect button below is exactly what
        # fixes this, so it needs to still show up.
        connected_accounts = []
        st.warning(f"Couldn't check connected accounts yet: {e}")

    if connected_accounts:
        account_labels = ", ".join(
            f"{a.get('institution_name') or 'Brokerage'} ({a.get('name') or a.get('number') or a['id']})"
            for a in connected_accounts
        )
        st.success(f"Connected: {account_labels}")
    else:
        st.write("No brokerage connected yet.")

    connect_col, sync_col = st.columns(2)

    if connect_col.button("Connect / Reconnect Fidelity"):
        try:
            portal_url = snaptrade_sync.get_connection_portal_url()
        except Exception as e:
            st.error(f"Couldn't open the connection portal: {e}")
        else:
            st.link_button("Open SnapTrade Connection Portal", portal_url)
            st.caption(
                "This link expires in 5 minutes - you'll log into "
                "Fidelity directly on Fidelity's own site, so your "
                "Fidelity password never passes through SnapTrade or "
                "this app."
            )

    if sync_col.button("Sync Now", disabled=not connected_accounts):
        # A year-wide window, not just "since last sync" - cheap (one
        # API call per connected account) and the shared insert helper
        # already skips anything already stored, so there's no harm in
        # re-checking further back than strictly necessary each time.
        end_date = timeutil.today_eastern()
        start_date = end_date - timedelta(days=365)
        try:
            with st.spinner("Fetching trade activity from SnapTrade..."):
                conn = database.get_connection()
                new_count = database.import_transactions_snaptrade(conn, start_date, end_date)
                trade_count = database.rebuild_trades(conn)
        except Exception as e:
            st.error(f"SnapTrade sync failed: {e}")
        else:
            st.success(
                f"Imported {new_count} new transaction row(s). "
                f"{trade_count} completed stock trade(s) total in the database."
            )

st.divider()
st.subheader("Import from a CSV export")
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
    except ValueError:
        # detect_csv_source() raises this when the file isn't a
        # recognizable Fidelity or Schwab export - show a plain message
        # instead of a crash screen. Nothing was imported.
        st.error(
            "That file doesn't look like a Fidelity or Schwab trade "
            "history export - its header row wasn't recognized. Make "
            "sure you exported Transaction/Account History as a CSV "
            "from your brokerage, then try again."
        )
    else:
        st.success(
            f"Imported {new_count} new transaction row(s). "
            f"{trade_count} completed stock trade(s) total in the database."
        )
    finally:
        tmp_path.unlink(missing_ok=True)
