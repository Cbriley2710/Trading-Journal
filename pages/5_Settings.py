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

These are just the DEFAULTS - any individual open position can still
override any of these for itself in the MA Stop Rule table (Open
Positions page); a position that hasn't customized a field falls back
to whatever's saved here.
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
    "Used by every open position that hasn't set its own override in the "
    "MA Stop Rule table on the Open Positions page."
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
