"""
Trade Analyzer
=====================
Pick one of your past completed trades and see its ticker on a price
chart, with the entry (buy) and exit (sell) marked - so you can look
back at what the stock was actually doing around a trade, not just the
numbers in the tracker.

Price history comes from `yfinance` - a free library that pulls
historical prices from Yahoo Finance, no account or API key needed.
It's an unofficial wrapper around Yahoo's own data (not a paid,
guaranteed-uptime service), which is fine for personal use but means a
symbol occasionally has gaps or fails to return data - handled below
with a plain message rather than a crash.

The actual chart-building (candlesticks, moving averages, colors,
ticker overlay) lives in charting.py, shared with the Shortlist page
and the nightly archive script - this file just wires up trade
selection and the fact tiles around it.

This is a second "page" of the dashboard app - Streamlit automatically
turns any file placed in a pages/ folder next to dashboard.py into its
own page, listed in the sidebar. Nothing needs to be registered by
hand.
"""

from datetime import timedelta

import streamlit as st

import auth
import charting
import database
import nav
import ui

st.set_page_config(page_title="Trade Analyzer", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Trade Analyzer")

st.title("Trade Analyzer")


def load_trades():
    conn = database.get_connection()
    return database.get_trades(conn)


def trade_label(trade):
    sign = "+" if trade["profit_loss"] >= 0 else ""
    short_tag = " (Short)" if trade["direction"] == "SHORT" else ""
    return (
        f"{trade['symbol']}{short_tag}: {trade['entry_date']:%m/%d/%Y} to "
        f"{trade['date']:%m/%d/%Y} ({sign}${trade['profit_loss']:,.2f})"
    )


def fact_tile(column, label, value, color=None):
    """This page's slightly smaller variant of the shared stat tile."""
    ui.stat_tile(column, label, value, color, size="1.3rem")


trades = load_trades()

if not trades:
    st.info("No trades found yet. Run import_trades.py first to populate the database.")
    st.stop()

trades_sorted = sorted(trades, key=lambda t: t["date"], reverse=True)

entry_dates = [t["entry_date"].date() for t in trades_sorted]
min_entry_date, max_entry_date = min(entry_dates), max(entry_dates)

date_range = st.date_input(
    "Filter by entry date", value=(min_entry_date, max_entry_date),
    min_value=min_entry_date, max_value=max_entry_date,
)
# date_input in range mode returns a single date until both ends have
# been picked - only filter once we actually have a (start, end) pair,
# so the list doesn't collapse to nothing while a user is mid-pick.
if isinstance(date_range, tuple) and len(date_range) == 2:
    range_start, range_end = date_range
    trades_sorted = [t for t in trades_sorted if range_start <= t["entry_date"].date() <= range_end]

if not trades_sorted:
    st.info("No trades with an entry date in this range.")
    st.stop()

selected_index = st.selectbox(
    "Choose a trade", options=range(len(trades_sorted)),
    format_func=lambda i: trade_label(trades_sorted[i]),
)
trade = trades_sorted[selected_index]

is_short = trade["direction"] == "SHORT"
# For a short, "sell_price" is the short sale (the entry event) and
# "buy_price" is the cover (the exit event) - the opposite pairing
# from a long trade. See match_trades_lifo() in analyze_trades.py.
entry_price, exit_price = (trade["sell_price"], trade["buy_price"]) if is_short \
    else (trade["buy_price"], trade["sell_price"])

outcome_color = charting.GOOD_COLOR if trade["profit_loss"] >= 0 else charting.CRITICAL_COLOR
# Expressed as % of entry price, using the actual (correctly-signed)
# profit_loss rather than a raw price ratio - a profitable short would
# otherwise show a misleading negative % (cover price fell below the
# entry price, which is the whole point of a profitable short).
pct_change = (trade["profit_loss"] / (entry_price * trade["quantity"])) * 100 if entry_price else 0.0

cols = st.columns(5)
fact_tile(cols[0], "Short Entry" if is_short else "Entry", f"${entry_price:,.2f}")
fact_tile(cols[1], "Cover" if is_short else "Exit", f"${exit_price:,.2f}")
fact_tile(cols[2], "Shares", f"{trade['quantity']:,.0f}")
fact_tile(cols[3], "P/L", f"${trade['profit_loss']:,.2f}", outcome_color)
fact_tile(cols[4], "% Change", f"{pct_change:,.2f}%", outcome_color)

st.divider()

timeframe_label = st.radio("Timeframe", options=list(charting.TIMEFRAMES.keys()), index=1, horizontal=True)
interval, padding_days = charting.TIMEFRAMES[timeframe_label]

control_cols = st.columns([4, 1])
settings = charting.render_settings_toolbar(control_cols[1], "trade_analyzer")
control_cols[0].caption("Scroll on the chart to zoom in/out through time; drag or swipe to pan.")

# The chart opens showing just this default window (visible_start to
# visible_end), but fetches a much wider one (wide_start to wide_end -
# see FETCH_BUFFER_MULTIPLIER) so scrolling/zooming out on the chart
# reveals real history instead of hitting an empty edge immediately.
visible_start = trade["entry_date"] - timedelta(days=padding_days)
visible_end = trade["date"] + timedelta(days=padding_days)

fetch_padding_days = padding_days * charting.FETCH_BUFFER_MULTIPLIER
wide_start = trade["entry_date"] - timedelta(days=fetch_padding_days)
wide_end = trade["date"] + timedelta(days=fetch_padding_days)

# Fetch extra history before wide_start so the longest selected moving
# average already has a real window of data at the left edge of the
# fetched range, instead of only "warming up" partway through it.
max_ma_period = max(settings["ma_periods"], default=0)
lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
fetch_start = wide_start - timedelta(days=lookback_days)

with st.spinner(f"Fetching {timeframe_label.lower()} price history for {trade['symbol']}..."):
    history = charting.fetch_history(
        trade["symbol"], fetch_start, wide_start, wide_end, interval, settings["ma_periods"])

if history.empty:
    st.warning(charting.history_error_message(history, trade["symbol"]))
    st.stop()

overlay_history = None
if settings["overlay_symbol"]:
    with st.spinner(f"Fetching overlay data for {settings['overlay_symbol']}..."):
        overlay_history = charting.fetch_history(
            settings["overlay_symbol"], fetch_start, wide_start, wide_end, interval, [])
    if overlay_history.empty:
        st.warning(
            charting.history_error_message(overlay_history, settings["overlay_symbol"])
            + " Showing chart without it."
        )
        overlay_history = None

# (The OHLC summary line now lives inside the chart component itself,
# where it updates live as the crosshair moves - see charting.py.)

entry_point = {
    "entry_date": trade["entry_date"], "buy_price": trade["buy_price"],
    "exit_date": trade["date"], "sell_price": trade["sell_price"],
    "direction": trade["direction"],
}

conn = database.get_connection()
saved_drawings = database.get_drawings(conn, trade["symbol"])

fig, fit_payload = charting.build_figure(
    trade["symbol"], history, entry_point, settings, overlay_history, interval=interval,
    visible_range=(visible_start, visible_end), drawings=saved_drawings)
current_drawings = charting.render_interactive_chart(fig, fit_payload, saved_drawings, key="trade_analyzer")

# Only writes to the database when something's actually different from
# what's saved - see pages/2_Shortlist.py's own render_price_chart()
# for the same pattern.
if current_drawings != saved_drawings:
    database.save_drawings(conn, trade["symbol"], current_drawings)
