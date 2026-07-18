"""
Shortlist
=====================
Your currently-open positions (bought but not yet sold) - the daily
routine page. Pick a ticker, review its chart, and write down your
thoughts for today. Every night, an automated job (see
nightly_archive.py) snapshots the chart and archives it together with
whatever you wrote here into that ticker's permanent Logbook.

A position shows up here as soon as it's in the database with more
buys than sells for that symbol - see database.get_open_positions().
Since this app only learns about your trades when you import a CSV
(no live broker feed), the shortlist is only as current as your most
recent import (Import Trades page, or import_trades.py locally).
"""

from datetime import date, datetime, timedelta

import streamlit as st
import yfinance as yf

import auth
import charting
import database

st.set_page_config(page_title="Shortlist", layout="wide")

if not auth.check_password():
    st.stop()

st.title("Shortlist")


def load_positions():
    conn = database.get_connection()
    return database.get_open_positions(conn)


def position_label(position):
    return (
        f"{position['symbol']}: {position['quantity']:,.0f} shares @ "
        f"avg ${position['avg_price']:,.2f} (opened {position['entry_date']:%m/%d/%Y})"
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


positions = load_positions()

if not positions:
    st.info(
        "No open positions right now. A ticker shows up here as soon as "
        "an imported buy hasn't been matched to a sell yet."
    )
    st.stop()

selected_index = st.selectbox(
    "Choose an open position", options=range(len(positions)),
    format_func=lambda i: position_label(positions[i]),
)
position = positions[selected_index]
symbol = position["symbol"]

with st.spinner(f"Fetching current price for {symbol}..."):
    recent = yf.Ticker(symbol).history(period="5d")

current_price = recent["Close"].iloc[-1] if not recent.empty else None
unrealized_pl = None
unrealized_color = None
if current_price is not None:
    unrealized_pl = (current_price - position["avg_price"]) * position["quantity"]
    unrealized_color = charting.GOOD_COLOR if unrealized_pl >= 0 else charting.CRITICAL_COLOR

cols = st.columns(5)
fact_tile(cols[0], "Entry (avg)", f"${position['avg_price']:,.2f}")
fact_tile(cols[1], "Entry Date", f"{position['entry_date']:%m/%d/%Y}")
fact_tile(cols[2], "Shares", f"{position['quantity']:,.0f}")
fact_tile(cols[3], "Current Price", f"${current_price:,.2f}" if current_price is not None else "N/A")
fact_tile(cols[4], "Unrealized P/L",
          f"${unrealized_pl:,.2f}" if unrealized_pl is not None else "N/A", unrealized_color)

st.divider()

timeframe_label = st.radio("Timeframe", options=list(charting.TIMEFRAMES.keys()), index=1, horizontal=True)
interval, default_padding, min_padding, max_padding = charting.TIMEFRAMES[timeframe_label]

control_cols = st.columns([3, 1])
padding_days = control_cols[0].slider(
    "Days of context before the entry",
    min_value=min_padding, max_value=max_padding, value=default_padding,
)
settings = charting.render_settings_toolbar(control_cols[1])

display_start = position["entry_date"] - timedelta(days=padding_days)
display_end = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=1)

max_ma_period = max(settings["ma_periods"], default=0)
lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
fetch_start = display_start - timedelta(days=lookback_days)

with st.spinner(f"Fetching {timeframe_label.lower()} price history for {symbol}..."):
    history = charting.fetch_history(
        symbol, fetch_start, display_start, display_end, interval, settings["ma_periods"])

if history.empty:
    st.warning(
        f"No price data found for {symbol} in this date range. "
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

entry_point = {"entry_date": position["entry_date"], "buy_price": position["avg_price"]}
fig = charting.build_figure(symbol, history, entry_point, settings, overlay_history)
st.plotly_chart(fig, use_container_width=True)

st.divider()
st.subheader("Today's Journal")

conn = database.get_connection()
today = date.today()
existing_entry = database.get_logbook_entry(conn, symbol, today)
existing_notes = existing_entry["notes"] if existing_entry else ""

notes = st.text_area("Your thoughts on this trade today", value=existing_notes or "", height=150)

if st.button("Save"):
    database.upsert_logbook_entry(conn, symbol, today, notes=notes)
    st.success(
        "Saved. Tonight's automated archive will attach a chart snapshot "
        "to this entry in the Logbook."
    )
