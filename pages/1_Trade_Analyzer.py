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

st.set_page_config(page_title="Trade Analyzer", layout="wide")

if not auth.check_password():
    st.stop()

st.title("Trade Analyzer")


def load_trades():
    conn = database.get_connection()
    return database.get_trades(conn)


def trade_label(trade):
    sign = "+" if trade["profit_loss"] >= 0 else ""
    return (
        f"{trade['symbol']}: {trade['entry_date']:%m/%d/%Y} to "
        f"{trade['date']:%m/%d/%Y} ({sign}${trade['profit_loss']:,.2f})"
    )


def fact_tile(column, label, value, color=None):
    style = f"color:{color};" if color else ""
    column.markdown(
        f"""
        <div style="text-align:center;">
            <div style="font-size:0.85rem;color:{charting.MUTED_COLOR};">{label}</div>
            <div style="font-size:1.3rem;font-weight:600;{style}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


trades = load_trades()

if not trades:
    st.info("No trades found yet. Run import_trades.py first to populate the database.")
    st.stop()

trades_sorted = sorted(trades, key=lambda t: t["date"], reverse=True)

selected_index = st.selectbox(
    "Choose a trade", options=range(len(trades_sorted)),
    format_func=lambda i: trade_label(trades_sorted[i]),
)
trade = trades_sorted[selected_index]

outcome_color = charting.GOOD_COLOR if trade["profit_loss"] >= 0 else charting.CRITICAL_COLOR
pct_change = (trade["sell_price"] / trade["buy_price"] - 1) * 100

cols = st.columns(5)
fact_tile(cols[0], "Entry", f"${trade['buy_price']:,.2f}")
fact_tile(cols[1], "Exit", f"${trade['sell_price']:,.2f}")
fact_tile(cols[2], "Shares", f"{trade['quantity']:,.0f}")
fact_tile(cols[3], "P/L", f"${trade['profit_loss']:,.2f}", outcome_color)
fact_tile(cols[4], "% Change", f"{pct_change:,.2f}%", outcome_color)

st.divider()

timeframe_label = st.radio("Timeframe", options=list(charting.TIMEFRAMES.keys()), index=1, horizontal=True)
interval, default_padding, min_padding, max_padding = charting.TIMEFRAMES[timeframe_label]

control_cols = st.columns([3, 1])
padding_days = control_cols[0].slider(
    "Days of context before/after the trade",
    min_value=min_padding, max_value=max_padding, value=default_padding,
)
settings = charting.render_settings_toolbar(control_cols[1])

display_start = trade["entry_date"] - timedelta(days=padding_days)
display_end = trade["date"] + timedelta(days=padding_days)

# Fetch extra history before display_start so the longest selected
# moving average already has a real window of data at the left edge
# of the chart, instead of only "warming up" partway through it.
max_ma_period = max(settings["ma_periods"], default=0)
lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
fetch_start = display_start - timedelta(days=lookback_days)

with st.spinner(f"Fetching {timeframe_label.lower()} price history for {trade['symbol']}..."):
    history = charting.fetch_history(
        trade["symbol"], fetch_start, display_start, display_end, interval, settings["ma_periods"])

if history.empty:
    st.warning(
        f"No price data found for {trade['symbol']} in this date range. "
        "It may be delisted, or Yahoo Finance may not have data for it."
    )
    st.stop()

overlay_history = None
if settings["overlay_symbol"]:
    with st.spinner(f"Fetching overlay data for {settings['overlay_symbol']}..."):
        overlay_history = charting.fetch_history(
            settings["overlay_symbol"], fetch_start, display_start, display_end, interval, [])
    if overlay_history.empty:
        st.warning(f"No price data found for overlay ticker {settings['overlay_symbol']}. Showing chart without it.")
        overlay_history = None

entry_point = {
    "entry_date": trade["entry_date"], "buy_price": trade["buy_price"],
    "exit_date": trade["date"], "sell_price": trade["sell_price"],
}
fig = charting.build_figure(trade["symbol"], history, entry_point, settings, overlay_history)
st.plotly_chart(fig, use_container_width=True)
