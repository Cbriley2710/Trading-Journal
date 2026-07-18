"""
Charting
=====================
The shared chart-building code behind every price chart in this project -
Trade Analyzer, the Shortlist page, and the nightly archive script all call
into this module, so a chart looks and behaves the same everywhere and a
fix/improvement only has to happen in one place.

An "entry point" in this file means a dict describing where a position was
opened (and, if the trade is already closed, where it was exited):
    {"entry_date": datetime, "buy_price": float,
     "exit_date": datetime or None, "sell_price": float or None}
Trade Analyzer passes both (a closed trade); the Shortlist page and the
nightly archive script pass entry-only dicts (exit_date/sell_price = None),
since those positions haven't been sold yet.
"""

import streamlit as st
import yfinance as yf
import plotly.graph_objects as go

GOOD_COLOR = "#0ca30c"
CRITICAL_COLOR = "#d03b3b"
MUTED_COLOR = "#898781"

# Default colors offered for each moving average you type in, assigned in
# this fixed order (1st MA gets blue, 2nd gets aqua, etc.) - just a
# starting point, since the interactive toolbar also lets you override any
# of them with your own color picker.
CATEGORICAL_PALETTE = [
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
]

# Timeframes offered, and the default/min/max calendar-day "padding" to
# fetch before/after the trade at each one - a coarser timeframe needs much
# more padding to show a meaningful number of bars around the trade (15
# days of padding is plenty zoomed into daily candles, but would barely
# show 2 extra candles on a monthly chart).
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

# Used by the nightly archive script, which has no interactive toolbar to
# pull settings from - a plain, consistent snapshot for every ticker.
DEFAULT_SETTINGS = {
    "chart_type": "Candlestick",
    "price_scale": "Linear",
    "up_color": GOOD_COLOR,
    "down_color": CRITICAL_COLOR,
    "line_color": None,
    "ma_periods": [20, 50],
    "ma_colors": {20: CATEGORICAL_PALETTE[0], 50: CATEGORICAL_PALETTE[1]},
    "overlay_symbol": None,
    "overlay_color": None,
}


def parse_ma_periods(text):
    """Turns something like "20, 50, 200" into [20, 50, 200], ignoring
    blanks, non-numbers, zero/negative numbers, and duplicates."""
    periods = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and int(part) not in periods:
            periods.append(int(part))
    return sorted(periods)


def fetch_history(symbol, fetch_start, display_start, display_end, interval, ma_periods):
    """
    Fetches price history from `fetch_start` (which includes extra lookback
    for moving averages) through `display_end`, computes any requested
    moving averages, then trims back down to `display_start` so the chart
    only shows the intended window. Returns an empty DataFrame if no data
    was found (symbol invalid/delisted, or Yahoo Finance has no data for
    the range) - callers are responsible for handling that case.
    """
    history = yf.Ticker(symbol).history(start=fetch_start, end=display_end, interval=interval)
    if history.empty:
        return history

    # yfinance returns timezone-aware dates; the rest of this project's
    # dates are plain (timezone-less), so this lines them up.
    history.index = history.index.tz_localize(None)

    for period in ma_periods:
        history[f"MA{period}"] = history["Close"].rolling(period).mean()

    return history[history.index >= display_start]


def render_settings_toolbar(container):
    """
    Renders the "Chart Settings" popover (chart type, colors, price scale,
    moving averages, overlay ticker) and returns a settings dict shaped
    like DEFAULT_SETTINGS above. Used by any page that wants the same
    interactive Chart Settings experience Trade Analyzer introduced.
    """
    with container.popover("Chart Settings", use_container_width=True):
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

    return {
        "chart_type": chart_type,
        "price_scale": price_scale,
        "up_color": up_color,
        "down_color": down_color,
        "line_color": line_color,
        "ma_periods": ma_periods,
        "ma_colors": ma_colors,
        "overlay_symbol": overlay_symbol,
        "overlay_color": overlay_color,
    }


def build_figure(symbol, history, entry_point, settings, overlay_history=None):
    """
    Builds the go.Figure for a price chart: candlestick or line, moving
    averages, an entry marker (plus an exit marker and connecting line if
    the trade is already closed), and an optional overlay ticker shown as
    % change. Returns the figure - callers render it with st.plotly_chart.
    """
    entry_date = entry_point["entry_date"]
    buy_price = entry_point["buy_price"]
    exit_date = entry_point.get("exit_date")
    sell_price = entry_point.get("sell_price")
    is_closed = exit_date is not None and sell_price is not None

    ma_periods = settings["ma_periods"]
    ma_colors = settings["ma_colors"]

    if is_closed:
        outcome_color = GOOD_COLOR if sell_price >= buy_price else CRITICAL_COLOR
    else:
        outcome_color = CATEGORICAL_PALETTE[0]

    fig = go.Figure()

    if overlay_history is not None and not overlay_history.empty:
        # Two different stocks' raw dollar prices aren't on the same scale,
        # so comparing them only makes sense as % change from a shared
        # starting point - this replaces the candlestick/absolute-price
        # view entirely while an overlay is active.
        baseline = history["Close"].iloc[0]
        primary_pct = (history["Close"] / baseline - 1) * 100
        overlay_baseline = overlay_history["Close"].iloc[0]
        overlay_pct = (overlay_history["Close"] / overlay_baseline - 1) * 100
        entry_pct = (buy_price / baseline - 1) * 100

        fig.add_trace(go.Scatter(
            x=history.index, y=primary_pct, mode="lines",
            line=dict(color=settings["line_color"] or CATEGORICAL_PALETTE[0], width=2),
            name=symbol,
            hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=overlay_history.index, y=overlay_pct, mode="lines",
            line=dict(color=settings["overlay_color"], width=2, dash="dash"),
            name=settings["overlay_symbol"],
            hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
        ))
        for period in ma_periods:
            if f"MA{period}" not in history.columns:
                continue  # fetch_history was called with a different set of periods
            ma_pct = (history[f"MA{period}"] / baseline - 1) * 100
            fig.add_trace(go.Scatter(
                x=history.index, y=ma_pct, mode="lines",
                line=dict(color=ma_colors[period], width=1.5),
                name=f"{period}-period MA",
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ))

        if is_closed:
            exit_pct = (sell_price / baseline - 1) * 100
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[entry_pct, exit_pct],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[entry_pct], mode="markers",
                marker=dict(size=14, symbol="triangle-up", color=outcome_color),
                name="Entry", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ))
        yaxis_title = "% Change from start of chart"

    else:
        if settings["chart_type"] == "Candlestick":
            fig.add_trace(go.Candlestick(
                x=history.index,
                open=history["Open"], high=history["High"],
                low=history["Low"], close=history["Close"],
                name=symbol,
                increasing_line_color=settings["up_color"], increasing_fillcolor=settings["up_color"],
                decreasing_line_color=settings["down_color"], decreasing_fillcolor=settings["down_color"],
                showlegend=False,
            ))
        else:
            fig.add_trace(go.Scatter(
                x=history.index, y=history["Close"], mode="lines",
                line=dict(color=settings["line_color"], width=2),
                name=symbol, showlegend=False,
            ))
        for period in ma_periods:
            if f"MA{period}" not in history.columns:
                continue  # fetch_history was called with a different set of periods
            fig.add_trace(go.Scatter(
                x=history.index, y=history[f"MA{period}"], mode="lines",
                line=dict(color=ma_colors[period], width=1.5),
                name=f"{period}-period MA",
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ))

        if is_closed:
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[buy_price, sell_price],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ))
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[buy_price], mode="markers",
                marker=dict(size=14, symbol="triangle-up", color=outcome_color),
                name="Entry", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ))
        yaxis_title = "Price ($)"

    fig.update_layout(
        height=500,
        margin=dict(t=30, b=10),
        xaxis_rangeslider_visible=False,
        yaxis_title=yaxis_title,
        yaxis_type="log" if (settings["price_scale"] == "Log" and overlay_history is None) else "linear",
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig
