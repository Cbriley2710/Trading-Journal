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

This is a second "page" of the dashboard app - Streamlit automatically
turns any file placed in a pages/ folder next to dashboard.py into its
own page, listed in the sidebar. Nothing needs to be registered by
hand.
"""

from datetime import timedelta

import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import auth
import database

GOOD_COLOR = "#0ca30c"
CRITICAL_COLOR = "#d03b3b"
MUTED_COLOR = "#898781"

# How many extra calendar days of price history to show before/after
# the trade itself, so the chart has real context instead of just the
# trade's own window (this matters a lot for same-day trades, which
# would otherwise be a single candle).
PADDING_DAYS = 15

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
            <div style="font-size:0.85rem;color:{MUTED_COLOR};">{label}</div>
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

outcome_color = GOOD_COLOR if trade["profit_loss"] >= 0 else CRITICAL_COLOR
pct_change = (trade["sell_price"] / trade["buy_price"] - 1) * 100

cols = st.columns(5)
fact_tile(cols[0], "Entry", f"${trade['buy_price']:,.2f}")
fact_tile(cols[1], "Exit", f"${trade['sell_price']:,.2f}")
fact_tile(cols[2], "Shares", f"{trade['quantity']:,.0f}")
fact_tile(cols[3], "P/L", f"${trade['profit_loss']:,.2f}", outcome_color)
fact_tile(cols[4], "% Change", f"{pct_change:,.2f}%", outcome_color)

st.divider()

start = trade["entry_date"] - timedelta(days=PADDING_DAYS)
end = trade["date"] + timedelta(days=PADDING_DAYS)

with st.spinner(f"Fetching price history for {trade['symbol']}..."):
    history = yf.Ticker(trade["symbol"]).history(start=start, end=end, interval="1d")

if history.empty:
    st.warning(
        f"No price data found for {trade['symbol']} in this date range. "
        "It may be delisted, or Yahoo Finance may not have data for it."
    )
    st.stop()

# yfinance returns timezone-aware dates; the trade dates from the
# database are plain (timezone-less), so this lines them up on the
# same chart.
history.index = history.index.tz_localize(None)

fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=history.index,
    open=history["Open"], high=history["High"],
    low=history["Low"], close=history["Close"],
    name=trade["symbol"],
))
fig.add_trace(go.Scatter(
    x=[trade["entry_date"], trade["date"]],
    y=[trade["buy_price"], trade["sell_price"]],
    mode="lines+markers",
    line=dict(color=outcome_color, width=2, dash="dot"),
    marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
    name="Entry / Exit",
    hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
))
fig.update_layout(
    height=500,
    margin=dict(t=30, b=10),
    xaxis_rangeslider_visible=False,
    yaxis_title="Price ($)",
    plot_bgcolor="#fcfcfb",
    paper_bgcolor="#fcfcfb",
    showlegend=False,
)
st.plotly_chart(fig, use_container_width=True)
