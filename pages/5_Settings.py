"""
Settings
=====================
A place for discretionary numbers that are more convenient to tune here
than to have hardcoded in the app's code - starting with the defaults
behind the MA Stop Rule on the Open Positions page (see ma_strategy.py):
which moving average to track a position against, how many closes on
the wrong side of it count as a sell signal, how far that average needs
to clear cost basis before a trailing stop takes over, and the distance
thresholds behind its "approaching"/"extended" warnings.

These numbers are global, used by every position - the Open Positions
page only lets you choose Off/Manual/Auto per ticker, not override the
MA period or thresholds individually (see database.get_position_ma_
settings(), which still supports a per-position override at the data
layer if a future UI ever wants to expose one).

Also here: relative column widths for the Open Positions table's
Positions & Stop-Loss grid (see database.get_open_positions_column_
widths()) - that table is built from plain st.columns() rows rather
than a data_editor grid (needed for the O/M/A mode buttons to be real
buttons), which means there's no native drag-to-resize; this is the
adjustable alternative.
"""

import streamlit as st

import auth
import database
import nav

st.set_page_config(page_title="Settings", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Settings")

st.title("Settings")

conn = database.get_connection()
settings = database.get_strategy_settings(conn)

st.header("MA Stop Rule Defaults")
st.caption(
    "Used by every open position - the Open Positions page only lets you "
    "choose Off/Manual/Auto per ticker; these numbers apply globally."
)

col1, col2 = st.columns(2)
ma_period = col1.number_input(
    "Moving average period (days)", min_value=2, step=1,
    value=settings["ma_period"], key="settings_ma_period",
)
closes_threshold = col2.number_input(
    "Closes against trend to trigger a sell signal", min_value=1, step=1,
    value=settings["closes_threshold"], key="settings_closes_threshold",
)

col3, col4, col5 = st.columns(3)
unlock_pct = col3.number_input(
    "Unlock % (MA must clear cost basis by this much before Auto trails the stop)",
    min_value=0.0, step=0.5, format="%.1f", value=settings["unlock_pct"], key="settings_unlock_pct",
)
approach_pct = col4.number_input(
    "Approaching % (within this distance of the MA)",
    min_value=0.0, step=0.1, format="%.1f", value=settings["approach_pct"], key="settings_approach_pct",
)
extended_pct = col5.number_input(
    "Extended % (this far or more from the MA)",
    min_value=0.0, step=0.5, format="%.1f", value=settings["extended_pct"], key="settings_extended_pct",
)

if st.button("Save Defaults"):
    database.save_strategy_settings(conn, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct)
    st.success("Saved.")
    st.rerun()

st.divider()

st.header("Open Positions Column Widths")
st.caption(
    "Relative widths for the Positions & Stop-Loss table - not pixels, just "
    "proportions of the row (e.g. doubling a number doubles that column's "
    "share of the width). \"Mode Buttons\" applies to all three O/M/A "
    "buttons at once, since they're always the same width as each other."
)

widths = database.get_open_positions_column_widths(conn)

width_col1, width_col2, width_col3 = st.columns(3)
new_widths = {
    "ticker": width_col1.number_input("Ticker", min_value=0.1, step=0.1, value=widths["ticker"], key="width_ticker"),
    "entry_date": width_col2.number_input("Entry Date", min_value=0.1, step=0.1, value=widths["entry_date"], key="width_entry_date"),
    "shares": width_col3.number_input("Shares", min_value=0.1, step=0.1, value=widths["shares"], key="width_shares"),
    "avg_price": width_col1.number_input("Avg Price", min_value=0.1, step=0.1, value=widths["avg_price"], key="width_avg_price"),
    "current_price": width_col2.number_input("Current Price", min_value=0.1, step=0.1, value=widths["current_price"], key="width_current_price"),
    "unrealized_pl": width_col3.number_input("Unrealized P/L", min_value=0.1, step=0.1, value=widths["unrealized_pl"], key="width_unrealized_pl"),
    "stop_loss": width_col1.number_input("Stop Loss", min_value=0.1, step=0.1, value=widths["stop_loss"], key="width_stop_loss"),
    "mode": width_col2.number_input("Mode Buttons (O/M/A)", min_value=0.1, step=0.05, value=widths["mode"], key="width_mode"),
    "ma_signal": width_col3.number_input("MA Signal", min_value=0.1, step=0.1, value=widths["ma_signal"], key="width_ma_signal"),
}

if st.button("Save Column Widths"):
    database.save_open_positions_column_widths(conn, new_widths)
    st.success("Saved.")
    st.rerun()

st.divider()

st.header("Background Image")
st.caption(
    "Upload a picture to use as the app's background, on every page - "
    "see nav.render_top_nav(), which every page already calls. A dark "
    "overlay is applied on top of it automatically, since every chart, "
    "table, and block of text on this app assumes a dark background "
    "underneath it - without that, a bright photo would make plenty of "
    "existing text unreadable no matter what the picture shows."
)

current_background = database.get_background_image(conn)
if current_background:
    st.image(current_background["bytes"], caption="Current background", width=300)

uploaded_background = st.file_uploader(
    "Upload a background image", type=["png", "jpg", "jpeg"], key="background_upload")

background_cols = st.columns(2)
if background_cols[0].button("Save Background", disabled=uploaded_background is None):
    database.save_background_image(conn, uploaded_background.getvalue(), uploaded_background.type)
    nav.clear_background_cache()
    st.success("Background saved.")
    st.rerun()

if background_cols[1].button("Remove Background", disabled=not current_background):
    database.clear_background_image(conn)
    nav.clear_background_cache()
    st.success("Background removed - back to the default look.")
    st.rerun()
