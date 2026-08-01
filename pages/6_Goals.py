"""
Goals
=====================
Two sections, matching goals.py's own two layers:

  - Goal Library: a reference table of every goal AVAILABLE to track -
    browse-only, nothing to click here. See goals.GOAL_LIBRARY.

  - Tracked Goals: the actual working dashboard - goals you've chosen
    to track, each with a Target Value/Comparison you set and a
    Current Value computed fresh from your real trade history every
    time this page loads (see goals.current_value()), plus a Met/Not
    Met/In Progress Status. Add as many as you want; nothing about the
    page needs to change when you do, since every tracked goal's number
    comes from the exact same lookup (goal's own "metric" key -> a
    function in goals.METRIC_FUNCS) - see goals.py's own module
    docstring for the full reasoning.

Scoped to Statistical (trade-derived) goals for now - see goals.py's
own note on why the data model already has room for a "Process/
Discipline" or "Manual Input" category later without changing this
page's mechanics.
"""

import streamlit as st

import auth
import charting
import database
import goals
import nav

st.set_page_config(page_title="Goals", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("goals")

st.title("Goals")

conn = database.get_connection()


def format_value(value, goal, timeframe):
    """Formats a metric's raw number for display - $/%/ratio/count,
    matching goal["unit"]. Account Growth is the one goal whose unit
    depends on WHICH timeframe was picked (see goals.GOAL_LIBRARY's own
    note on it), so that's handled as a special case here rather than
    every goal needing a per-timeframe unit."""
    if value is None:
        return "—"
    if goal["key"] == "account_growth":
        return f"{value:,.2f}%" if timeframe == "Monthly" else f"${value:,.2f}"
    unit = goal["unit"]
    if unit == "$":
        return f"${value:,.2f}"
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "x":
        return f"{value:,.2f}x"
    return f"{value:,.0f}"  # "trades" (streaks)


STATUS_COLORS = {"Met": charting.GOOD_COLOR, "Not Met": charting.CRITICAL_COLOR, "In Progress": charting.MUTED_COLOR}


def render_status(column, stat):
    if stat is None:
        column.write("—")
        return
    color = STATUS_COLORS[stat]
    column.markdown(f"<span style='color:{color};font-weight:700;'>{stat}</span>", unsafe_allow_html=True)


st.header("Goal Library")
st.caption(
    "Every goal available to track below - add a new one to goals.GOAL_LIBRARY "
    "any time (see that module's own docstring); nothing else on this page "
    "needs to change for it to show up here and become trackable."
)
st.dataframe(
    [
        {
            "Goal": g["name"], "Category": g["category"], "Timeframes": ", ".join(g["timeframes"]),
            "Data Source": g["data_source"], "Suggested Comparison": g["suggested_comparison"],
        }
        for g in goals.GOAL_LIBRARY
    ],
    width="stretch", hide_index=True,
)

st.divider()

st.header("Tracked Goals")
st.caption(
    "Current Value is always computed fresh from your real trade history "
    "(and, for Account Growth, your Jan 1 baseline and live prices) - never "
    "a stored number. \"In Progress\" only applies to Daily/Weekly/Monthly "
    "goals - there's still time left in the period for the number to move; "
    "a Rolling or All-Time goal that isn't Met is simply Not Met."
)

with st.spinner("Computing current values..."):
    context = goals.build_context(conn)

tracked = database.get_tracked_goals(conn)

if not tracked:
    st.info("No goals tracked yet - add one below.")
else:
    col_widths = [2, 1.3, 1, 1, 1.2, 1.2, 0.6]
    header_cols = st.columns(col_widths)
    for col, label in zip(header_cols, ["Goal", "Timeframe", "Target", "Comparison", "Current", "Status", ""]):
        col.markdown(f"**{label}**")

    for tg in tracked:
        goal = goals.GOAL_BY_KEY.get(tg["goal_key"])
        row_cols = st.columns(col_widths)

        if goal is None:
            # The code changed since this row was added (a goal_key
            # that no longer exists in GOAL_LIBRARY) - shown plainly
            # rather than crashing, with Remove as the only real option
            # since there's no definition left to compute anything from.
            row_cols[0].write(f"⚠️ Unknown goal ({tg['goal_key']})")
            if row_cols[6].button("✕", key=f"remove_{tg['id']}"):
                database.delete_tracked_goal(conn, tg["id"])
                st.rerun()
            continue

        value = goals.current_value(goal, tg["timeframe"], context)
        stat = goals.status(value, tg["target_value"], tg["comparison"], tg["timeframe"])

        row_cols[0].write(goal["name"])
        row_cols[1].write(tg["timeframe"])
        row_cols[2].write(format_value(tg["target_value"], goal, tg["timeframe"]))
        row_cols[3].write(tg["comparison"])
        row_cols[4].write(format_value(value, goal, tg["timeframe"]))
        render_status(row_cols[5], stat)
        if row_cols[6].button("✕", key=f"remove_{tg['id']}"):
            database.delete_tracked_goal(conn, tg["id"])
            st.rerun()

st.divider()

st.subheader("Add a Goal")
# Deliberately NOT an st.form - the Timeframe dropdown's OWN options
# depend on which Goal is picked (only that goal's valid timeframes -
# see goals.GOAL_LIBRARY), which needs an immediate rerun on every
# Goal change to stay in sync. A form only reruns on submit, which
# would leave Timeframe showing the PREVIOUS goal's options until
# Add Goal was clicked.
add_cols = st.columns([2, 1.3, 1, 1, 0.8])
goal_names = [g["name"] for g in goals.GOAL_LIBRARY]
chosen_name = add_cols[0].selectbox("Goal", goal_names, key="add_goal_name")
chosen_goal = next(g for g in goals.GOAL_LIBRARY if g["name"] == chosen_name)

chosen_timeframe = add_cols[1].selectbox("Timeframe", chosen_goal["timeframes"], key="add_goal_timeframe")
target_value = add_cols[2].number_input("Target Value", value=0.0, step=0.1, format="%.2f", key="add_goal_target")
comparison_options = [">=", "<=", "="]
comparison = add_cols[3].selectbox(
    "Comparison", comparison_options,
    index=comparison_options.index(chosen_goal["suggested_comparison"]),
    key="add_goal_comparison",
)

add_cols[4].write("")  # vertical spacer so the button lines up with the inputs, not their labels
if add_cols[4].button("Add Goal", type="primary"):
    database.add_tracked_goal(conn, chosen_goal["key"], chosen_timeframe, target_value, comparison)
    st.success(f"Added {chosen_name} ({chosen_timeframe}).")
    st.rerun()
