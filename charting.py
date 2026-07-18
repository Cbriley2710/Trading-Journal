"""
Charting
=====================
The shared chart-building code behind every price chart in this project -
Trade Analyzer, the Shortlist page, and the nightly archive script all call
into this module, so a chart looks and behaves the same everywhere and a
fix/improvement only has to happen in one place.

VISUAL STYLE: modeled on a DeepVue chart screenshot the user shared - a
dark theme, muted blue/pink candles, a volume panel, a watermark, and
colored right-edge price badges. The exact colors below were sampled
directly from that screenshot (not run through this project's usual
color-accessibility validator, since matching a specific reference image
was the point here, not picking new colors from scratch).

An "entry point" in this file means a dict describing where a position was
opened (and, if the trade is already closed, where it was exited):
    {"entry_date": datetime, "buy_price": float,
     "exit_date": datetime or None, "sell_price": float or None}
Trade Analyzer passes both (a closed trade); the Shortlist page and the
nightly archive script pass entry-only dicts (exit_date/sell_price = None),
since those positions haven't been sold yet.
"""

from datetime import timedelta

import pandas as pd
import streamlit as st
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# These stay separate from the chart's own candle colors below - they're
# used for *trade outcome* meaning elsewhere (win/loss stat tiles,
# entry/exit markers), which is a different thing from which way one
# day's candle moved. Don't repaint these to match the candle colors.
GOOD_COLOR = "#0ca30c"
CRITICAL_COLOR = "#d03b3b"
MUTED_COLOR = "#898781"

# The chart's own dark theme, sampled from the DeepVue screenshot.
CHART_BACKGROUND = "#111214"
GRIDLINE_COLOR = "#23262b"
CHART_TEXT_COLOR = "#d1d4dc"
WATERMARK_COLOR = "rgba(255, 255, 255, 0.05)"
BADGE_TEXT_COLOR = "#ffffff"

# Candlestick colors - a muted sky-blue for an up day, muted salmon-pink
# for a down day, instead of the more common green/red.
UP_CANDLE_COLOR = "#8ccbf7"
DOWN_CANDLE_COLOR = "#f19a9b"

# Volume bars use a more conventional dark green/red, matching the
# screenshot - DeepVue draws volume differently from its own candles.
VOLUME_UP_COLOR = "#1e6b28"
VOLUME_DOWN_COLOR = "#ba1b21"

# Default colors offered for each moving average you type in, assigned in
# this fixed order (1st MA gets blue, 2nd gets green, etc.) - just a
# starting point, since the interactive toolbar also lets you override any
# of them with your own color picker. The first four are sampled directly
# from the screenshot's own moving-average lines; the rest continue with
# this project's existing dark-mode categorical colors.
CATEGORICAL_PALETTE = [
    "#2375f4", "#009246", "#f89f1b", "#afebf2",
    "#9085e9", "#e66767", "#d55181", "#d95926",
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
    "up_color": UP_CANDLE_COLOR,
    "down_color": DOWN_CANDLE_COLOR,
    "line_color": None,
    "ma_periods": [20, 50],
    "ma_colors": {20: CATEGORICAL_PALETTE[0], 50: CATEGORICAL_PALETTE[1]},
    "overlay_symbol": None,
    "overlay_color": None,
}

# The archived Logbook snapshot always shows this many TRADING days of
# history before the entry/added date, regardless of when it's
# generated (a Save-button click, or the nightly fallback job) - a
# fixed window so entries are visually comparable day to day, not
# whatever timeframe/padding the interactive chart happened to be set
# to at that moment. Converted to calendar days using the same
# trading-day ratio as LOOKBACK_DAYS_PER_PERIOD.
ARCHIVE_PADDING_TRADING_DAYS = 180


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


def price_near_date(history, target_date):
    """
    Returns the closing price on the first available trading day on or
    after `target_date` (falling back to the last available close if
    `target_date` is after every row in `history`). Used to place a
    marker for a watchlist ticker, which has no real trade price of its
    own to plot - just the date it was added to the list.
    """
    on_or_after = history[history.index >= target_date]
    if not on_or_after.empty:
        return on_or_after["Close"].iloc[0]
    return history["Close"].iloc[-1]


def build_ohlc_summary(history, symbol, interval_label):
    """
    A one-line OHLC summary shown above the chart, DeepVue-style, e.g.
    "MU - 1D   O 822.52   H 903.96   L 804.00   C 848.95   V 63.4M
    Chg -4.25   Chg% -0.50%". Uses the last two rows of `history` (for
    the day-over-day change) - no separate fetch needed. Returns an
    empty string if there's no data.
    """
    if history.empty:
        return ""

    last = history.iloc[-1]
    prev_close = history["Close"].iloc[-2] if len(history) > 1 else last["Close"]
    change = last["Close"] - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0

    volume = last["Volume"]
    if volume >= 1e9:
        volume_str = f"{volume / 1e9:.1f}B"
    elif volume >= 1e6:
        volume_str = f"{volume / 1e6:.1f}M"
    elif volume >= 1e3:
        volume_str = f"{volume / 1e3:.1f}K"
    else:
        volume_str = f"{volume:,.0f}"

    sign = "+" if change >= 0 else ""
    return (
        f"{symbol} · {interval_label}   "
        f"O {last['Open']:,.2f}   H {last['High']:,.2f}   L {last['Low']:,.2f}   C {last['Close']:,.2f}   "
        f"V {volume_str}   Chg {sign}{change:,.2f}   Chg% {sign}{change_pct:,.2f}%"
    )


def render_png(fig):
    """
    Renders a Plotly figure to PNG bytes via kaleido. Tries the plain,
    ordinary render first - on a machine that already has a working
    Chrome install (kaleido finds it automatically), that's all this
    ever does.

    Only if that fails does this fall back to explicitly pointing
    kaleido at a system-installed Chromium - installed via apt, either
    from packages.txt (Streamlit Community Cloud) or a dedicated step
    in the GitHub Actions workflow (nightly_archive.py's run), both of
    which let apt handle Chromium's own system-library dependencies
    correctly. This deliberately avoids letting kaleido download its
    own standalone copy, which turned out to be actively harmful during
    development: once downloaded, kaleido preferred that copy over an
    already-working system browser, and the downloaded build failed to
    even launch, on both Windows (a missing runtime dependency) and a
    Linux server (missing shared libraries a minimal container doesn't
    have) - two different failures with the same root cause.
    """
    try:
        return fig.to_image(format="png")
    except Exception:
        import shutil
        import kaleido

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        if not chromium_path:
            raise

        fig_dict = fig.to_dict()
        return kaleido.calc_fig_sync(
            fig_dict, opts={"format": "png"}, kopts={"path": chromium_path})


def build_archive_snapshot(symbol, entry_date, buy_price, entry_label, as_of):
    """
    Builds the fixed-format chart image archived into a ticker's Logbook:
    always ARCHIVE_PADDING_TRADING_DAYS of history before `entry_date`
    through `as_of`, with DEFAULT_SETTINGS - the same window/style no
    matter when it's generated (a Save-button click during the day, or
    the nightly fallback job), so entries are visually comparable day to
    day rather than reflecting whatever the interactive chart happened
    to be showing. Returns PNG bytes, or None if no price data was found.
    """
    padding_days = ARCHIVE_PADDING_TRADING_DAYS * LOOKBACK_DAYS_PER_PERIOD["1d"]
    display_start = entry_date - timedelta(days=padding_days)
    display_end = as_of + timedelta(days=1)

    max_ma_period = max(DEFAULT_SETTINGS["ma_periods"], default=0)
    lookback_days = max_ma_period * LOOKBACK_DAYS_PER_PERIOD["1d"]
    fetch_start = display_start - timedelta(days=lookback_days)

    history = fetch_history(symbol, fetch_start, display_start, display_end, "1d", DEFAULT_SETTINGS["ma_periods"])
    if history.empty:
        return None

    entry_point = {"entry_date": entry_date, "buy_price": buy_price} if buy_price is not None \
        else {"entry_date": entry_date, "buy_price": price_near_date(history, entry_date)}

    fig = build_figure(symbol, history, entry_point, DEFAULT_SETTINGS, entry_label=entry_label)
    return render_png(fig)


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
            up_color = candle_cols[0].color_picker("Bullish candle", value=UP_CANDLE_COLOR)
            down_color = candle_cols[1].color_picker("Bearish candle", value=DOWN_CANDLE_COLOR)
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


def build_figure(symbol, history, entry_point, settings, overlay_history=None, entry_label="Entry"):
    """
    Builds the go.Figure for a price chart: candlestick or line, moving
    averages, an entry marker (plus an exit marker and connecting line if
    the trade is already closed), a volume panel, and an optional overlay
    ticker shown as % change. Returns the figure - callers render it with
    st.plotly_chart.

    `entry_label` names that marker - "Entry" for a real trade (the
    default), or something like "Added" for a watchlist ticker with no
    actual trade behind it.
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

    has_overlay = overlay_history is not None and not overlay_history.empty
    # The overlay (% change) view and volume don't mix meaningfully on the
    # same chart - volume is specific to the primary symbol's own shares
    # traded, so the volume panel only appears when there's no overlay.
    show_volume = not has_overlay

    if show_volume:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.75, 0.25], vertical_spacing=0.03,
        )
    else:
        fig = make_subplots(rows=1, cols=1)

    if has_overlay:
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
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=overlay_history.index, y=overlay_pct, mode="lines",
            line=dict(color=settings["overlay_color"], width=2, dash="dash"),
            name=settings["overlay_symbol"],
            hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
        ), row=1, col=1)
        for period in ma_periods:
            if f"MA{period}" not in history.columns:
                continue  # fetch_history was called with a different set of periods
            ma_pct = (history[f"MA{period}"] / baseline - 1) * 100
            fig.add_trace(go.Scatter(
                x=history.index, y=ma_pct, mode="lines",
                line=dict(color=ma_colors[period], width=1.5),
                name=f"{period}-period MA",
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ), row=1, col=1)

        if is_closed:
            exit_pct = (sell_price / baseline - 1) * 100
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[entry_pct, exit_pct],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[entry_pct], mode="markers",
                marker=dict(size=14, symbol="triangle-up", color=outcome_color),
                name=entry_label, showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ), row=1, col=1)
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
            ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=history.index, y=history["Close"], mode="lines",
                line=dict(color=settings["line_color"], width=2),
                name=symbol, showlegend=False,
            ), row=1, col=1)
        for period in ma_periods:
            if f"MA{period}" not in history.columns:
                continue  # fetch_history was called with a different set of periods
            fig.add_trace(go.Scatter(
                x=history.index, y=history[f"MA{period}"], mode="lines",
                line=dict(color=ma_colors[period], width=1.5),
                name=f"{period}-period MA",
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ), row=1, col=1)

        if is_closed:
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[buy_price, sell_price],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=["triangle-up", "triangle-down"], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[buy_price], mode="markers",
                marker=dict(size=14, symbol="triangle-up", color=outcome_color),
                name=entry_label, showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ), row=1, col=1)
        yaxis_title = "Price ($)"

        # Volume panel - bars colored the same up/down as the candles
        # would be conventionally (green/red), which is deliberately
        # different from this chart's own blue/pink candle colors,
        # matching the DeepVue reference.
        volume_colors = [
            VOLUME_UP_COLOR if close >= open_ else VOLUME_DOWN_COLOR
            for open_, close in zip(history["Open"], history["Close"])
        ]
        fig.add_trace(go.Bar(
            x=history.index, y=history["Volume"],
            marker_color=volume_colors,
            name="Volume", showlegend=False,
            hovertemplate="%{x|%b %d, %Y}: %{y:,.0f}<extra></extra>",
        ), row=2, col=1)
        volume_ma = history["Volume"].rolling(50, min_periods=1).mean()
        fig.add_trace(go.Scatter(
            x=history.index, y=volume_ma, mode="lines",
            line=dict(color=CHART_TEXT_COLOR, width=1.5),
            name="Avg Volume", showlegend=False,
            hovertemplate="%{x|%b %d, %Y}: %{y:,.0f}<extra></extra>",
        ), row=2, col=1)

    # A large, faint watermark of the symbol behind the price panel.
    fig.add_annotation(
        text=symbol, xref="x domain", yref="y domain", x=0.5, y=0.5,
        showarrow=False, font=dict(size=72, color=WATERMARK_COLOR),
    )

    # Right-edge colored badges: current price + each moving average's
    # latest value, in that line's own color - a distinctive DeepVue touch.
    if not has_overlay:
        last_price = history["Close"].iloc[-1]
        fig.add_annotation(
            x=1, xref="x domain", y=last_price, yref="y",
            text=f"{last_price:,.2f}", showarrow=False, xanchor="left",
            font=dict(color=BADGE_TEXT_COLOR, size=11),
            bgcolor=outcome_color, borderpad=3,
        )
        for period in ma_periods:
            col_name = f"MA{period}"
            if col_name not in history.columns:
                continue
            last_ma = history[col_name].iloc[-1]
            if pd.isna(last_ma):
                continue
            fig.add_annotation(
                x=1, xref="x domain", y=last_ma, yref="y",
                text=f"{last_ma:,.2f}", showarrow=False, xanchor="left",
                font=dict(color=BADGE_TEXT_COLOR, size=11),
                bgcolor=ma_colors[period], borderpad=3,
            )

    fig.update_layout(
        height=560 if show_volume else 500,
        margin=dict(t=30, b=10, r=55),
        yaxis_title=yaxis_title,
        yaxis_type="log" if (settings["price_scale"] == "Log" and not has_overlay) else "linear",
        plot_bgcolor=CHART_BACKGROUND,
        paper_bgcolor=CHART_BACKGROUND,
        font=dict(color=CHART_TEXT_COLOR),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    fig.update_xaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False, rangeslider_visible=False)
    fig.update_yaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False)
    if show_volume:
        fig.update_yaxes(title_text="Volume", row=2, col=1)

    return fig
