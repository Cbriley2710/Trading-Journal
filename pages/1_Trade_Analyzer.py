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

# Default colors offered for each moving average you type in, assigned
# in this fixed order (1st MA gets blue, 2nd gets aqua, etc.) - just a
# starting point, since each one also gets its own color picker in the
# Chart Settings toolbar so you can override any of them.
CATEGORICAL_PALETTE = [
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
]

# Timeframes offered, and the default/min/max calendar-day "padding"
# to fetch before/after the trade at each one (shown as an adjustable
# slider below, not fixed) - a coarser timeframe needs much more
# padding to show a meaningful number of bars around the trade (15
# days of padding is plenty zoomed into daily candles, but would
# barely show 2 extra candles on a monthly chart).
TIMEFRAMES = {
    "Hourly": ("1h", 5, 1, 30),
    "Daily": ("1d", 15, 5, 120),
    "Weekly": ("1wk", 60, 15, 365),
    "Monthly": ("1mo", 365, 90, 1825),
}

# How many extra calendar days of history to fetch BEFORE the visible
# window, per moving-average period, so the longest selected average
# already has a full window of real data by the time the chart starts
# (otherwise its line would only "warm up" partway through the chart).
# Rough calendar-days-per-bar for each timeframe, with some buffer for
# weekends/holidays/off-hours.
LOOKBACK_DAYS_PER_PERIOD = {"1h": 0.25, "1d": 1.6, "1wk": 8, "1mo": 32}

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


def parse_ma_periods(text):
    """Turns something like "20, 50, 200" into [20, 50, 200], ignoring
    blanks, non-numbers, zero/negative numbers, and duplicates."""
    periods = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and int(part) not in periods:
            periods.append(int(part))
    return sorted(periods)


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

timeframe_label = st.radio("Timeframe", options=list(TIMEFRAMES.keys()), index=1, horizontal=True)
interval, default_padding, min_padding, max_padding = TIMEFRAMES[timeframe_label]

control_cols = st.columns([3, 1])
padding_days = control_cols[0].slider(
    "Days of context before/after the trade",
    min_value=min_padding, max_value=max_padding, value=default_padding,
)

with control_cols[1].popover("Chart Settings", use_container_width=True):
    chart_type = st.radio("Chart Type", ["Candlestick", "Line"], horizontal=True)
    price_scale = st.radio("Price Scale", ["Linear", "Log"], horizontal=True)

    if chart_type == "Candlestick":
        candle_cols = st.columns(2)
        up_color = candle_cols[0].color_picker("Bullish candle", value=GOOD_COLOR)
        down_color = candle_cols[1].color_picker("Bearish candle", value=CRITICAL_COLOR)
        line_color = None
    else:
        up_color = down_color = None
        line_color = st.color_picker("Line color", value=CATEGORICAL_PALETTE[0])

    ma_text = st.text_input(
        "Moving Averages (comma-separated periods)", value="",
        placeholder="e.g. 9, 21, 50",
    )
    ma_periods = parse_ma_periods(ma_text)

    ma_colors = {}
    if ma_periods:
        ma_color_cols = st.columns(len(ma_periods))
        for i, period in enumerate(ma_periods):
            default_color = CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)]
            ma_colors[period] = ma_color_cols[i].color_picker(
                f"{period}-period", value=default_color, key=f"ma_color_{period}",
            )

    overlay_symbol = st.text_input(
        "Overlay Ticker (optional)", value="", placeholder="e.g. SPY, QQQ",
    ).strip().upper()
    overlay_color = None
    if overlay_symbol:
        overlay_color = st.color_picker("Overlay color", value=CATEGORICAL_PALETTE[4])
        st.caption(
            "With an overlay, both tickers are shown as % change from the "
            "start of the chart, not raw price - comparing two different "
            "stocks' actual dollar prices on the same axis wouldn't mean "
            "anything, since they're not on the same scale."
        )

display_start = trade["entry_date"] - timedelta(days=padding_days)
display_end = trade["date"] + timedelta(days=padding_days)

# Fetch extra history before display_start so the longest selected
# moving average already has a real window of data at the left edge
# of the chart, instead of only "warming up" partway through it.
max_ma_period = max(ma_periods, default=0)
lookback_days = max_ma_period * LOOKBACK_DAYS_PER_PERIOD[interval]
fetch_start = display_start - timedelta(days=lookback_days)

with st.spinner(f"Fetching {timeframe_label.lower()} price history for {trade['symbol']}..."):
    history = yf.Ticker(trade["symbol"]).history(start=fetch_start, end=display_end, interval=interval)

if not history.empty:
    # yfinance returns timezone-aware dates; the trade dates from the
    # database are plain (timezone-less), so this lines them up on the
    # same chart.
    history.index = history.index.tz_localize(None)

    for period in ma_periods:
        history[f"MA{period}"] = history["Close"].rolling(period).mean()

    # Now that the moving averages are computed (using the extra
    # lookback), trim back down to just the window we actually want to
    # show on the chart.
    history = history[history.index >= display_start]

if history.empty:
    st.warning(
        f"No price data found for {trade['symbol']} in this date range. "
        "It may be delisted, or Yahoo Finance may not have data for it."
    )
    st.stop()

overlay_history = None
if overlay_symbol:
    with st.spinner(f"Fetching overlay data for {overlay_symbol}..."):
        overlay_history = yf.Ticker(overlay_symbol).history(
            start=fetch_start, end=display_end, interval=interval)
    if overlay_history.empty:
        st.warning(f"No price data found for overlay ticker {overlay_symbol}. Showing chart without it.")
        overlay_history = None
    else:
        overlay_history.index = overlay_history.index.tz_localize(None)
        overlay_history = overlay_history[overlay_history.index >= display_start]

fig = go.Figure()

if overlay_history is not None:
    # Two different stocks' raw dollar prices aren't on the same scale,
    # so comparing them only makes sense as % change from a shared
    # starting point - this replaces the candlestick/absolute-price
    # view entirely while an overlay is active.
    baseline = history["Close"].iloc[0]
    primary_pct = (history["Close"] / baseline - 1) * 100
    overlay_baseline = overlay_history["Close"].iloc[0]
    overlay_pct = (overlay_history["Close"] / overlay_baseline - 1) * 100
    entry_pct = (trade["buy_price"] / baseline - 1) * 100
    exit_pct = (trade["sell_price"] / baseline - 1) * 100

    fig.add_trace(go.Scatter(
        x=history.index, y=primary_pct, mode="lines",
        line=dict(color=line_color or CATEGORICAL_PALETTE[0], width=2),
        name=trade["symbol"],
        hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=overlay_history.index, y=overlay_pct, mode="lines",
        line=dict(color=overlay_color, width=2, dash="dash"),
        name=overlay_symbol,
        hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
    ))
    for period in ma_periods:
        ma_pct = (history[f"MA{period}"] / baseline - 1) * 100
        fig.add_trace(go.Scatter(
            x=history.index, y=ma_pct, mode="lines",
            line=dict(color=ma_colors[period], width=1.5),
            name=f"{period}-period MA",
            hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=[trade["entry_date"], trade["date"]],
        y=[entry_pct, exit_pct],
        mode="lines+markers",
        line=dict(color=outcome_color, width=2, dash="dot"),
        marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
        name="Entry / Exit",
        showlegend=False,
        hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
    ))
    yaxis_title = "% Change from start of chart"
else:
    if chart_type == "Candlestick":
        fig.add_trace(go.Candlestick(
            x=history.index,
            open=history["Open"], high=history["High"],
            low=history["Low"], close=history["Close"],
            name=trade["symbol"],
            increasing_line_color=up_color, increasing_fillcolor=up_color,
            decreasing_line_color=down_color, decreasing_fillcolor=down_color,
            showlegend=False,
        ))
    else:
        fig.add_trace(go.Scatter(
            x=history.index,
            y=history["Close"],
            mode="lines",
            line=dict(color=line_color, width=2),
            name=trade["symbol"],
            showlegend=False,
        ))
    for period in ma_periods:
        fig.add_trace(go.Scatter(
            x=history.index,
            y=history[f"MA{period}"],
            mode="lines",
            line=dict(color=ma_colors[period], width=1.5),
            name=f"{period}-period MA",
            hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=[trade["entry_date"], trade["date"]],
        y=[trade["buy_price"], trade["sell_price"]],
        mode="lines+markers",
        line=dict(color=outcome_color, width=2, dash="dot"),
        marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
        name="Entry / Exit",
        showlegend=False,
        hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
    ))
    yaxis_title = "Price ($)"

fig.update_layout(
    height=500,
    margin=dict(t=30, b=10),
    xaxis_rangeslider_visible=False,
    yaxis_title=yaxis_title,
    yaxis_type="log" if (price_scale == "Log" and overlay_history is None) else "linear",
    plot_bgcolor="#fcfcfb",
    paper_bgcolor="#fcfcfb",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
)
st.plotly_chart(fig, use_container_width=True)
