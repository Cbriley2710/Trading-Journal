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

import database

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

# The hover crosshair - a light, subtle dotted line so it reads as a
# cursor aid, not another data series competing with the candles/MAs.
SPIKE_COLOR = "rgba(255, 255, 255, 0.4)"
SPIKE_DASH = "dot"

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

# Timeframes offered, and the default VISIBLE calendar-day window when
# the chart first opens (Hourly 5 days, Daily 120, Weekly ~2 years,
# Monthly a year) - a coarser timeframe needs much more of a window to
# show a meaningful number of bars. There's no user-adjustable slider for
# this anymore - scroll-to-zoom on the chart itself replaces it. This is
# NOT how much data gets fetched - see FETCH_BUFFER_MULTIPLIER below -
# just what's shown by default before you zoom/pan.
TIMEFRAMES = {
    "Hourly": ("1h", 5),
    "Daily": ("1d", 120),
    "Weekly": ("1wk", 720),
    "Monthly": ("1mo", 365),
}

# The reverse lookup - which human-readable label goes with each yfinance
# interval string - used by the live summary line above the chart.
INTERVAL_LABELS = {interval: label for label, (interval, _days) in TIMEFRAMES.items()}

# Actual fetched history is this many times wider than the default
# visible window above, so there's real data to scroll/zoom into on
# either side rather than hitting a hard, empty edge immediately.
FETCH_BUFFER_MULTIPLIER = 3

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

# The archived Logbook snapshot always shows this many of the most
# recent TRADING days ending today, regardless of when it's generated
# (a Save-button click, or the nightly fallback job) - a fixed window
# so entries are visually comparable day to day, not whatever
# timeframe/padding the interactive chart happened to be set to at that
# moment. Converted to calendar days using the same trading-day ratio
# as LOOKBACK_DAYS_PER_PERIOD.
ARCHIVE_VISIBLE_TRADING_DAYS = 110

# Extra blank space added after today's candle, as a fraction of
# ARCHIVE_VISIBLE_TRADING_DAYS, so today sits at roughly 3/4 of the way
# across the chart instead of jammed against the right edge. 1/3 of the
# real candles gives about 25% extra blank width - e.g. 110 real candles
# plus ~37 blank ones puts today at 110/147, about 75%.
ARCHIVE_RIGHT_MARGIN_FRACTION = 1 / 3


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


def _compute_rangebreaks(history, interval):
    """
    Returns Plotly x-axis "rangebreaks" that hide non-trading gaps -
    weekends, holidays, and (for hourly data) overnight hours - so
    candles/bars sit next to each other instead of leaving a visible
    blank stretch on the chart for every day the market was closed.

    Weekends are hidden for every interval. For daily bars, any other
    missing weekday (a holiday) is found by comparing the full business-day
    range against the dates actually present in `history` - computed fresh
    from the real data each time, rather than maintaining a holiday
    calendar by hand. For hourly bars, the hours outside roughly 9:30am-4pm
    are hidden too, since a trading day is only open part of each day.
    """
    breaks = [dict(bounds=["sat", "mon"])]

    if interval == "1d" and len(history) > 1:
        all_weekdays = pd.bdate_range(history.index.min(), history.index.max())
        missing = all_weekdays.difference(history.index)
        if len(missing) > 0:
            # Plotly/kaleido's JSON serialization can't handle pandas
            # Timestamp objects directly - plain datetimes only.
            breaks.append(dict(values=list(missing.to_pydatetime())))
    elif interval == "1h":
        breaks.append(dict(bounds=[16, 9.5], pattern="hour"))

    return breaks


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


def fetch_latest_price(symbol):
    """
    Returns the most recent closing price for a symbol (a plain, current
    "what's it worth right now" lookup - not a full history fetch), or
    None if Yahoo Finance has no recent data for it. Used anywhere that
    just needs today's price: the Shortlist page's unrealized P/L, and
    the Open Positions page's equity/heat calculations.

    Also returns None if the lookup itself blows up (a network hiccup,
    Yahoo rate-limiting, etc.) - every caller already handles None as
    "no price available right now," which beats crashing a whole page
    over one symbol's failed lookup.
    """
    try:
        recent = yf.Ticker(symbol).history(period="5d")
    except Exception:
        return None
    if recent.empty:
        return None
    return recent["Close"].iloc[-1]


def style_simple_chart(fig, yaxis_title, height=350):
    """
    Applies this app's shared dark look to a plain bar/line figure -
    the Dashboard's summary charts and the Open Positions charts all
    use this (the full price charts have their own styling inside
    build_figure()). Returns the same figure, styled, so it can be
    passed straight into st.plotly_chart(). Until this existed, the
    exact same styling block was copy-pasted around both pages.
    """
    fig.update_layout(
        height=height,
        margin=dict(t=10, b=45),
        yaxis_title=yaxis_title,
        plot_bgcolor=CHART_BACKGROUND,
        paper_bgcolor=CHART_BACKGROUND,
        font=dict(color=CHART_TEXT_COLOR),
    )
    fig.update_xaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False)
    return fig


def render_png(fig, width=1400, scale=2):
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

    `width` and `scale` are passed straight to kaleido: without them,
    Plotly falls back to a bare 700px-wide render, which looks blurry
    once Streamlit stretches it to fill a much wider page. `scale=2`
    renders at double that pixel density (like a "retina" image) on
    top of the wider width, so it stays sharp at typical page widths.
    """
    try:
        return fig.to_image(format="png", width=width, scale=scale)
    except Exception:
        import shutil
        import kaleido

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        if not chromium_path:
            raise

        fig_dict = fig.to_dict()
        return kaleido.calc_fig_sync(
            fig_dict, opts={"format": "png", "width": width, "scale": scale},
            kopts={"path": chromium_path})


def build_archive_snapshot(symbol, entry_date, buy_price, entry_label, as_of, direction="LONG"):
    """
    Builds the fixed-format chart image archived into a ticker's Logbook:
    always the most recent ARCHIVE_VISIBLE_TRADING_DAYS ending at `as_of`,
    in the same dark DeepVue style no matter when it's generated (a
    Save-button click during the day, or the nightly fallback job), so
    entries are visually comparable day to day rather than reflecting
    whatever the interactive chart happened to be showing. A bit of
    blank space is added after today's candle (ARCHIVE_RIGHT_MARGIN_
    FRACTION) so it sits around 3/4 of the way across instead of jammed
    against the right edge. The one thing NOT frozen to a default is
    moving averages - those come from your saved Chart Settings
    (database.get_chart_preferences()), the same ones the interactive
    chart shows, so an archived snapshot never falls out of sync with
    what you've actually configured. `direction` ("LONG" or "SHORT")
    decides which way the entry marker points - see build_figure().
    Returns PNG bytes, or None if no price data was found.
    """
    conn = database.get_connection()
    saved_prefs = database.get_chart_preferences(conn)
    ma_periods = parse_ma_periods(saved_prefs["ma_text"])
    ma_colors = {
        period: saved_prefs["ma_colors"].get(str(period), CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)])
        for i, period in enumerate(ma_periods)
    }
    settings = {**DEFAULT_SETTINGS, "ma_periods": ma_periods, "ma_colors": ma_colors}

    visible_days = ARCHIVE_VISIBLE_TRADING_DAYS * LOOKBACK_DAYS_PER_PERIOD["1d"]
    display_start = as_of - timedelta(days=visible_days)
    display_end = as_of + timedelta(days=1)

    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * LOOKBACK_DAYS_PER_PERIOD["1d"]
    fetch_start = display_start - timedelta(days=lookback_days)

    history = fetch_history(symbol, fetch_start, display_start, display_end, "1d", settings["ma_periods"])
    if history.empty:
        return None

    entry_point = {"entry_date": entry_date, "buy_price": buy_price, "direction": direction} if buy_price is not None \
        else {"entry_date": entry_date, "buy_price": price_near_date(history, entry_date), "direction": direction}

    margin_days = visible_days * ARCHIVE_RIGHT_MARGIN_FRACTION
    visible_range = (display_start, display_end + timedelta(days=margin_days))

    fig, _fit_payload = build_figure(
        symbol, history, entry_point, settings, entry_label=entry_label, visible_range=visible_range)
    return render_png(fig)


def render_settings_toolbar(container, key_prefix):
    """
    Renders the "Chart Settings" popover (chart type, colors, price scale,
    moving averages, overlay ticker) and returns a settings dict shaped
    like DEFAULT_SETTINGS above. Used by any page that wants the same
    interactive Chart Settings experience Trade Analyzer introduced.

    `key_prefix` must be unique to the caller (e.g. "position", "watchlist",
    "trade_analyzer") - Shortlist's Open Positions and Watchlist sections
    each render their own copy of this toolbar on the same page, and
    every widget needs a distinct key or Streamlit raises a
    StreamlitDuplicateElementId error.

    Moving averages are the one setting that's saved permanently (see
    database.get_chart_preferences()/save_chart_preferences()) - typing
    in a new set of periods, or changing a color, saves it right away,
    so it's still there next time the app is opened, on any device,
    until it's changed again. Everything else here (chart type, candle
    colors, price scale, overlay ticker) stays session-only, as before.
    """
    conn = database.get_connection()
    saved_prefs = database.get_chart_preferences(conn)

    with container.popover("Chart Settings", width="stretch"):
        chart_type = st.radio(
            "Chart Type", ["Candlestick", "Line"], horizontal=True, key=f"{key_prefix}_chart_type")
        price_scale = st.radio(
            "Price Scale", ["Linear", "Log"], horizontal=True, key=f"{key_prefix}_price_scale")

        if chart_type == "Candlestick":
            candle_cols = st.columns(2)
            up_color = candle_cols[0].color_picker(
                "Bullish candle", value=UP_CANDLE_COLOR, key=f"{key_prefix}_up_color")
            down_color = candle_cols[1].color_picker(
                "Bearish candle", value=DOWN_CANDLE_COLOR, key=f"{key_prefix}_down_color")
            line_color = None
        else:
            up_color = down_color = None
            line_color = st.color_picker(
                "Line color", value=CATEGORICAL_PALETTE[0], key=f"{key_prefix}_line_color")

        ma_text = st.text_input(
            "Moving Averages (comma-separated periods)", value=saved_prefs["ma_text"],
            placeholder="e.g. 9, 21, 50", key=f"{key_prefix}_ma_text_input",
        )
        ma_periods = parse_ma_periods(ma_text)

        ma_colors = {}
        if ma_periods:
            ma_color_cols = st.columns(len(ma_periods))
            for i, period in enumerate(ma_periods):
                default_color = saved_prefs["ma_colors"].get(
                    str(period), CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)])
                ma_colors[period] = ma_color_cols[i].color_picker(
                    f"{period}-period", value=default_color, key=f"{key_prefix}_ma_color_{period}",
                )

        current_colors = {str(period): color for period, color in ma_colors.items()}
        if ma_text != saved_prefs["ma_text"] or current_colors != saved_prefs["ma_colors"]:
            database.save_chart_preferences(conn, ma_text, ma_colors)

        overlay_symbol = st.text_input(
            "Overlay Ticker (optional)", value="", placeholder="e.g. SPY, QQQ",
            key=f"{key_prefix}_overlay_symbol",
        ).strip().upper()
        overlay_color = None
        if overlay_symbol:
            overlay_color = st.color_picker(
                "Overlay color", value=CATEGORICAL_PALETTE[4], key=f"{key_prefix}_overlay_color")
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


def build_figure(symbol, history, entry_point, settings, overlay_history=None, entry_label="Entry", interval="1d",
                  visible_range=None):
    """
    Builds the go.Figure for a price chart: candlestick or line, moving
    averages, an entry marker (plus an exit marker and connecting line if
    the trade is already closed), a volume panel, and an optional overlay
    ticker shown as % change. Returns `(fig, fit_payload)` - `fig` gets
    rendered via render_interactive_chart() (or, for a static image,
    render_png()); `fit_payload` is a plain-JSON record of the real
    price/volume data render_interactive_chart()'s zoom handler needs to
    refit the y-axis as you scroll/pan (callers that don't need
    interactivity, like build_archive_snapshot(), can ignore it).

    `entry_label` names that marker - "Entry" for a real trade (the
    default), or something like "Added" for a watchlist ticker with no
    actual trade behind it. `interval` (the same string passed to
    fetch_history) decides which non-trading-day gaps get hidden - see
    _compute_rangebreaks(). `visible_range`, if given, is an
    (start, end) tuple setting the chart's initial zoomed-in view - useful
    when `history` itself covers a much wider window than should be shown
    by default (see FETCH_BUFFER_MULTIPLIER), so there's real data to
    scroll/zoom into on either side without an immediate empty edge.
    """
    entry_date = entry_point["entry_date"]
    buy_price = entry_point["buy_price"]
    exit_date = entry_point.get("exit_date")
    sell_price = entry_point.get("sell_price")
    is_closed = exit_date is not None and sell_price is not None

    # For a LONG trade the entry event is a buy and the exit is a sell,
    # so the entry marker is an up-triangle. For a SHORT trade it's the
    # other way around: the entry event is the short SALE (an up-front
    # sell) and the exit is buying it back to cover - so the triangle
    # directions swap. `buy_price`/`sell_price` keep their names either
    # way (see match_trades_fifo() in analyze_trades.py for why), but
    # which one lines up with entry_date vs exit_date flips too.
    direction = entry_point.get("direction", "LONG")
    entry_symbol = "triangle-down" if direction == "SHORT" else "triangle-up"
    exit_symbol = "triangle-up" if direction == "SHORT" else "triangle-down"

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

    # A plain-JSON (not Plotly's own, sometimes binary-encoded, figure
    # JSON) record of what the price/volume axes should fit at any given
    # visible time window - built directly from this data, not by trying
    # to parse Plotly's serialized traces back apart in JavaScript.
    # render_interactive_chart()'s zoom handler uses this to recompute
    # the y-axis range as you scroll/pan through time.
    fit_payload = {"price": [], "volume": []}

    # Every bar's own OHLC/volume/change numbers, plain-JSON - this is
    # what feeds the live summary line shown above the chart in
    # render_interactive_chart(): as the crosshair moves, the summary
    # updates to whichever bar the cursor is nearest, without trying to
    # parse Plotly's own compact-encoded trace data back apart in JS.
    # Chg/Chg% are close-over-close from the previous bar.
    daily_change = history["Close"].diff()
    daily_change_pct = history["Close"].pct_change() * 100
    def _clean(series):
        return [None if pd.isna(v) else float(v) for v in series]
    fit_payload["daily"] = {
        "x": [ts.isoformat() for ts in history.index],
        "open": _clean(history["Open"]),
        "high": _clean(history["High"]),
        "low": _clean(history["Low"]),
        "close": _clean(history["Close"]),
        "volume": _clean(history["Volume"]),
        "chg": _clean(daily_change),
        "pct": _clean(daily_change_pct),
    }
    fit_payload["meta"] = {
        "symbol": symbol,
        "interval_label": INTERVAL_LABELS.get(interval, interval),
    }

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
        buy_pct = (buy_price / baseline - 1) * 100

        fit_payload["price"].append({
            "x": [ts.isoformat() for ts in history.index],
            "lo": primary_pct.tolist(), "hi": primary_pct.tolist(),
        })
        fit_payload["price"].append({
            "x": [ts.isoformat() for ts in overlay_history.index],
            "lo": overlay_pct.tolist(), "hi": overlay_pct.tolist(),
        })

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
            sell_pct = (sell_price / baseline - 1) * 100
            entry_value, exit_value = (sell_pct, buy_pct) if direction == "SHORT" else (buy_pct, sell_pct)
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[entry_value, exit_value],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=[entry_symbol, exit_symbol], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[buy_pct], mode="markers",
                marker=dict(size=14, symbol=entry_symbol, color=outcome_color),
                name=entry_label, showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: %{y:.2f}%<extra></extra>",
            ), row=1, col=1)
        yaxis_title = "% Change from start of chart"

    else:
        fit_payload["price"].append({
            "x": [ts.isoformat() for ts in history.index],
            "lo": history["Low"].tolist(), "hi": history["High"].tolist(),
        })
        fit_payload["volume"].append({
            "x": [ts.isoformat() for ts in history.index],
            "lo": history["Volume"].tolist(), "hi": history["Volume"].tolist(),
        })

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
            entry_value, exit_value = (sell_price, buy_price) if direction == "SHORT" else (buy_price, sell_price)
            fig.add_trace(go.Scatter(
                x=[entry_date, exit_date], y=[entry_value, exit_value],
                mode="lines+markers",
                line=dict(color=outcome_color, width=2, dash="dot"),
                marker=dict(size=14, symbol=[entry_symbol, exit_symbol], color=outcome_color),
                name="Entry / Exit", showlegend=False,
                hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
            ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=[entry_date], y=[buy_price], mode="markers",
                marker=dict(size=14, symbol=entry_symbol, color=outcome_color),
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
        margin=dict(t=30, b=35, r=55),
        yaxis_title=yaxis_title,
        yaxis_type="log" if (settings["price_scale"] == "Log" and not has_overlay) else "linear",
        plot_bgcolor=CHART_BACKGROUND,
        paper_bgcolor=CHART_BACKGROUND,
        font=dict(color=CHART_TEXT_COLOR),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        # Without this, a click-drag (and on many browsers, a two-finger
        # trackpad swipe) defaults to drawing a rectangular zoom-select
        # box instead of panning - "pan" is what actually lets you slide
        # back and forth through time.
        dragmode="pan",
        # "closest" (rather than a unified "x") ties each hover event to
        # a single nearest point - what the crosshair and hover side
        # panel below are both built around.
        hovermode="closest",
    )
    # A light dotted crosshair that follows the cursor (not snapped to
    # the nearest candle) across the full height/width of the chart -
    # showspikes/spikemode="across" is what draws the actual lines;
    # SPIKE_COLOR/SPIKE_DASH are shared by both axes so the vertical and
    # horizontal lines match.
    fig.update_xaxes(
        gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False, rangeslider_visible=False,
        rangebreaks=_compute_rangebreaks(history, interval),
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor=SPIKE_COLOR, spikethickness=1, spikedash=SPIKE_DASH,
    )
    if visible_range is not None:
        fig.update_xaxes(range=list(visible_range))
    # Locking the y-axis (price/volume scale) means scrolling or dragging
    # on the chart only ever moves through time - it can never distort
    # the price scale, which is what makes trackpad scroll-to-zoom feel
    # like "see more/fewer days" instead of "zoom in/out on everything."
    fig.update_yaxes(
        gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False, fixedrange=True,
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor=SPIKE_COLOR, spikethickness=1, spikedash=SPIKE_DASH,
    )
    if show_volume:
        fig.update_yaxes(title_text="Volume", row=2, col=1)

    # No floating tooltip box over the chart - the crosshair plus the
    # live summary line above the chart replace it. "none" (rather than
    # "skip") still fires the hover EVENTS that summary line depends
    # on; it only hides Plotly's own popup label. This deliberately
    # clears the hovertemplates set on individual traces above, since a
    # trace's hovertemplate would otherwise take precedence.
    for trace in fig.data:
        trace.hovertemplate = None
        trace.hoverinfo = "none"

    return fig, fit_payload


_INTERACTIVE_CHART_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    /* Without this reset, the browser's default body margin makes the
       page a few pixels taller/wider than the iframe box around it,
       which triggers unwanted scrollbars - and the horizontal one then
       overlaps the bottom of the chart, right where the x-axis date
       labels are. */
    html, body { margin: 0; padding: 0; overflow: hidden; background: __CHART_BACKGROUND__; }
</style>
</head>
<body>
<!-- The live OHLC summary line - same format as the old static caption
     ("MU - Daily  O ... H ... L ... C ... V ... Chg ... Chg% ..."), but
     it now lives INSIDE this component so the hover script below can
     update it to whichever bar the crosshair is over. -->
<div id="__DIV_ID__-summary" style="font-family:sans-serif; font-size:0.85rem; font-weight:600;
     color:__CHART_TEXT_COLOR__; padding:6px 10px 0 10px; white-space:nowrap;">&nbsp;</div>
<div id="__DIV_ID__" style="width:100%;"></div>
<script src="https://cdn.plot.ly/plotly-2.35.3.min.js"></script>
<script>
(function() {
    var figure = __FIG_JSON__;
    // A separate, plain-JSON record of the real price/volume data (not
    // parsed back out of Plotly's own figure JSON, which uses a compact
    // binary array encoding that isn't reliably indexable from here) -
    // see build_figure()'s fit_payload for how this is built.
    var fitPayload = __FIT_JSON__;
    var config = {scrollZoom: true, displayModeBar: false, responsive: true};

    // The min/max of one series actually within [xmin, xmax], or null if
    // nothing in it falls in that window.
    function seriesExtent(series, xmin, xmax) {
        var lo = Infinity, hi = -Infinity;
        for (var i = 0; i < series.x.length; i++) {
            var t = new Date(series.x[i]).getTime();
            if (t < xmin || t > xmax) continue;
            var a = series.lo[i], b = series.hi[i];
            if (a !== null && a !== undefined && !isNaN(a) && a < lo) lo = a;
            if (b !== null && b !== undefined && !isNaN(b) && b > hi) hi = b;
        }
        if (lo === Infinity) return null;
        return [lo, hi];
    }

    // Recomputes the price axis (and the volume axis, separately) to
    // fit exactly what's visible in [xmin, xmax] - this is the part
    // Plotly's own scroll-zoom doesn't do on its own.
    function fitYAxes(gd, xmin, xmax) {
        var relayout = {};
        [["price", "yaxis"], ["volume", "yaxis2"]].forEach(function(pair) {
            var seriesList = fitPayload[pair[0]];
            if (!seriesList || !seriesList.length) return;
            var lo = Infinity, hi = -Infinity;
            seriesList.forEach(function(series) {
                var ext = seriesExtent(series, xmin, xmax);
                if (!ext) return;
                if (ext[0] < lo) lo = ext[0];
                if (ext[1] > hi) hi = ext[1];
            });
            if (lo === Infinity) return;
            var pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.08 || 1;
            relayout[pair[1] + ".range"] = [lo - pad, hi + pad];
            relayout[pair[1] + ".autorange"] = false;
        });
        if (Object.keys(relayout).length) {
            Plotly.relayout(gd, relayout);
        }
    }

    // Turns any timestamp string into one canonical "YYYY-MM-DD HH:MM"
    // key by pure text manipulation - deliberately NOT via new Date(),
    // because JavaScript parses a date-only string ("2026-07-17", how
    // Plotly reports a daily bar's x in its hover event) as UTC but a
    // datetime string ("2026-07-17T00:00:00", how fitPayload's ISO
    // strings look) as LOCAL time, so the two never produce the same
    // epoch value even though they describe the same bar - which is
    // exactly the mismatch that kept the summary line from updating.
    function barKey(x) {
        var s = String(x).replace("T", " ");
        if (s.length === 10) s += " 00:00";
        return s.slice(0, 16);
    }

    // Maps each bar's canonical key to its index in fitPayload.daily,
    // so the summary line can look up the bar under the cursor.
    var dailyByKey = {};
    var lastIndex = null;
    if (fitPayload.daily) {
        fitPayload.daily.x.forEach(function(iso, i) {
            dailyByKey[barKey(iso)] = i;
            lastIndex = i;
        });
    }

    var monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    // Reads the date parts straight out of the ISO string (again, no
    // new Date() - see barKey above for why parsing is a trap here).
    function formatDate(iso) {
        var s = String(iso);
        var month = parseInt(s.slice(5, 7), 10);
        var day = parseInt(s.slice(8, 10), 10);
        return monthNames[month - 1] + " " + day + ", " + s.slice(0, 4);
    }

    function formatPrice(v) {
        if (v === null || v === undefined) return "N/A";
        return v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    function formatVolume(v) {
        if (v === null || v === undefined) return "N/A";
        if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
        if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
        if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
        return Math.round(v).toLocaleString();
    }

    // Rewrites the summary line above the chart for one bar's data -
    // the date plus O/H/L/C/V, and Chg/Chg% colored green or red.
    function showDailyInfo(index) {
        var el = document.getElementById("__DIV_ID__-summary");
        if (index === null || index === undefined) {
            el.innerHTML = "&nbsp;";
            return;
        }
        var d = fitPayload.daily;
        var meta = fitPayload.meta || {symbol: "", interval_label: ""};
        var parts = [
            meta.symbol + " · " + meta.interval_label,
            formatDate(d.x[index]),
            "O " + formatPrice(d.open[index]),
            "H " + formatPrice(d.high[index]),
            "L " + formatPrice(d.low[index]),
            "C " + formatPrice(d.close[index]),
            "V " + formatVolume(d.volume[index]),
        ];
        var html = parts.join("&nbsp;&nbsp; ");
        var chg = d.chg[index], pct = d.pct[index];
        if (chg !== null && chg !== undefined && pct !== null && pct !== undefined) {
            var sign = chg >= 0 ? "+" : "";
            var color = chg >= 0 ? "__GOOD_COLOR__" : "__CRITICAL_COLOR__";
            html += "&nbsp;&nbsp; <span style='color:" + color + ";'>"
                + "Chg " + sign + formatPrice(chg)
                + "&nbsp;&nbsp; Chg% " + sign + pct.toFixed(2) + "%</span>";
        }
        el.innerHTML = html;
    }

    Plotly.newPlot("__DIV_ID__", figure.data, figure.layout, config).then(function(gd) {
        // Starts on the most recent bar until you actually hover.
        showDailyInfo(lastIndex);

        gd.on("plotly_hover", function(eventData) {
            if (!eventData.points || !eventData.points.length) return;
            var key = barKey(eventData.points[0].x);
            if (dailyByKey.hasOwnProperty(key)) {
                showDailyInfo(dailyByKey[key]);
            }
        });

        gd.on("plotly_unhover", function() {
            showDailyInfo(lastIndex);
        });

        // Debounced: calling Plotly.relayout() synchronously on every
        // single scroll-wheel tick fights with Plotly's own in-progress
        // zoom/pan handling (a rapid sequence of scroll events each
        // triggering another relayout mid-gesture), which is what made
        // scrolling/panning feel stuck instead of smooth. Waiting for a
        // brief pause in the events avoids interrupting the gesture.
        var pending = null;
        gd.on("plotly_relayout", function(eventData) {
            if (eventData["xaxis.range[0]"] !== undefined && eventData["xaxis.range[1]"] !== undefined) {
                var xmin = new Date(eventData["xaxis.range[0]"]).getTime();
                var xmax = new Date(eventData["xaxis.range[1]"]).getTime();
                if (pending) clearTimeout(pending);
                pending = setTimeout(function() {
                    fitYAxes(gd, xmin, xmax);
                }, 120);
            } else if (eventData["xaxis.autorange"]) {
                if (pending) clearTimeout(pending);
                var update = {"yaxis.autorange": true};
                if (gd.layout.yaxis2) { update["yaxis2.autorange"] = true; }
                Plotly.relayout(gd, update);
            }
        });
    });
})();
</script>
</body>
</html>
"""


def render_interactive_chart(fig, fit_payload):
    """
    Renders a chart via a small embedded Plotly.js component instead of
    st.plotly_chart, so a custom zoom/pan handler can refit the price
    (and volume) axis to whatever's actually visible - something
    Plotly's built-in scroll-zoom doesn't do on its own (it only
    rescales the existing range proportionally; it doesn't recompute a
    fresh min/max from the currently-visible data). The y-axes are
    already fixedrange (see build_figure()) so direct mouse/scroll
    interaction can't move them on its own - only this script's own
    computed relayout calls do, whenever the visible time window changes.

    `fit_payload` is the second value build_figure() returns alongside
    the figure itself.
    """
    import json
    import uuid

    div_id = f"chart-{uuid.uuid4().hex[:8]}"
    # +40 leaves room for the live OHLC summary line above the chart.
    height = (fig.layout.height or 500) + 40

    html = (
        _INTERACTIVE_CHART_HTML
        .replace("__CHART_BACKGROUND__", CHART_BACKGROUND)
        .replace("__CHART_TEXT_COLOR__", CHART_TEXT_COLOR)
        .replace("__GOOD_COLOR__", GOOD_COLOR)
        .replace("__CRITICAL_COLOR__", CRITICAL_COLOR)
        .replace("__DIV_ID__", div_id)
        .replace("__FIG_JSON__", fig.to_json())
        .replace("__FIT_JSON__", json.dumps(fit_payload))
    )
    st.iframe(html, height=height)
