"""
Goals
=====================
A dashboard of the goals you're actively tracking, one tile per goal
(Current Value + a Good/Warning/Alert badge - see goals.status_zone()).

Everything about ADDING or CONFIGURING a goal lives in the "Settings"
section at the bottom, collapsed by default so the main view stays a
clean dashboard:
  - Actively Tracked: the goals above, with their own editable Warning
    Level / Alert Level thresholds (same "number_input that saves and
    reruns on change" pattern as Stop Loss on the Open Positions page).
  - Available Goals: everything in goals.GOAL_LIBRARY not currently
    tracked - check its box to start tracking it.

See goals.py's own module docstring for the metric-function
architecture this page is just a thin display over.
"""

import streamlit as st

import auth
import database
import goals
import nav

st.set_page_config(page_title="Goals", page_icon="🎯", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("goals")

st.title("Goals")

conn = database.get_connection()


def format_value(value, goal):
    """Formats a metric's raw number for display - every current goal
    is a "%" unit; kept as a small dispatch (not a bare f-string inline)
    so a future non-% goal has one obvious place to add its own case."""
    if value is None:
        return "—"
    if goal["unit"] == "%":
        return f"{value:,.1f}%"
    return f"{value:,.2f}"


STATUS_BADGE_COLOR = {"Good": "green", "Warning": "yellow", "Alert": "red"}


with st.spinner("Computing current values..."):
    context = goals.build_context(conn)

tracked = database.get_tracked_goals(conn)

# --- Dashboard ---------------------------------------------------------
if not tracked:
    st.info("No goals tracked yet - open Settings below to add one.")
else:
    rows = [tracked[i:i + 4] for i in range(0, len(tracked), 4)]
    for row in rows:
        cols = st.columns(4)
        for col, tg in zip(cols, row):
            goal = goals.GOAL_BY_KEY.get(tg["goal_key"])
            with col.container(border=True):
                if goal is None:
                    # The code changed since this row was added (a
                    # goal_key no longer in GOAL_LIBRARY) - shown
                    # plainly instead of crashing; Settings is where
                    # it gets removed.
                    st.markdown(f"**⚠️ Unknown goal** ({tg['goal_key']})")
                    continue
                value = goals.current_value(goal, tg["timeframe"], context)
                zone = goals.status_zone(value, tg["warning_level"], tg["alert_level"], goal["direction"])
                st.markdown(f"**{goal['name']}**")
                st.markdown(f"<div style='font-size:1.8rem;font-weight:700;'>{format_value(value, goal)}</div>",
                             unsafe_allow_html=True)
                if zone is not None:
                    st.badge(zone, color=STATUS_BADGE_COLOR[zone])
                elif tg["warning_level"] is None or tg["alert_level"] is None:
                    st.caption("Set Warning/Alert Level in Settings")
                else:
                    st.caption("No data yet")

st.divider()

# --- Settings ------------------------------------------------------------
with st.expander("Settings"):
    st.subheader("Actively Tracked")
    if not tracked:
        st.caption("Nothing tracked yet - check a box below to add one.")
    else:
        col_widths = [2, 1.2, 1.2, 1.2, 0.6]
        header_cols = st.columns(col_widths)
        for col, label in zip(header_cols, ["Goal", "Current", "Warning Level", "Alert Level", ""]):
            col.markdown(f"**{label}**")

        for tg in tracked:
            goal = goals.GOAL_BY_KEY.get(tg["goal_key"])
            row_cols = st.columns(col_widths)

            if goal is None:
                row_cols[0].write(f"⚠️ Unknown goal ({tg['goal_key']})")
                if row_cols[4].button("✕", key=f"remove_{tg['id']}"):
                    database.delete_tracked_goal(conn, tg["id"])
                    st.rerun()
                continue

            value = goals.current_value(goal, tg["timeframe"], context)
            row_cols[0].write(goal["name"])
            row_cols[1].write(format_value(value, goal))

            new_warning = row_cols[2].number_input(
                "Warning Level", value=tg["warning_level"], step=1.0, format="%.2f",
                key=f"warning_{tg['id']}", label_visibility="collapsed",
            )
            new_alert = row_cols[3].number_input(
                "Alert Level", value=tg["alert_level"], step=1.0, format="%.2f",
                key=f"alert_{tg['id']}", label_visibility="collapsed",
            )
            if new_warning != tg["warning_level"] or new_alert != tg["alert_level"]:
                database.update_tracked_goal(conn, tg["id"], new_warning, new_alert)
                st.rerun()

            if row_cols[4].button("✕", key=f"remove_{tg['id']}"):
                database.delete_tracked_goal(conn, tg["id"])
                st.rerun()

    st.divider()

    st.subheader("Available Goals")
    tracked_keys = {tg["goal_key"] for tg in tracked}
    available = [g for g in goals.GOAL_LIBRARY if g["key"] not in tracked_keys]
    if not available:
        st.caption("Every goal in the library is already tracked.")
    else:
        avail_widths = [2, 4, 0.8]
        header_cols = st.columns(avail_widths)
        for col, label in zip(header_cols, ["Goal", "Data Source", "Track"]):
            col.markdown(f"**{label}**")

        for goal in available:
            row_cols = st.columns(avail_widths)
            row_cols[0].write(goal["name"])
            row_cols[1].caption(goal["data_source"])
            if row_cols[2].checkbox("Track", key=f"track_{goal['key']}", label_visibility="collapsed"):
                database.add_tracked_goal(conn, goal["key"], goal["timeframes"][0])
                st.rerun()
