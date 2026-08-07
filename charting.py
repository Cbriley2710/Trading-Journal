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

import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import database
import timeutil

# A real bidirectional custom component (see chart_component/index.html) -
# not a plain st.iframe embed, which can only receive data FROM Python.
# Drawing tools need the opposite direction too (what did you draw, so it
# can be saved), which only a properly declared component supports.
# `path` serves that folder's files as-is (no separate dev server or
# build step needed - just static files), so it works the same locally
# and once deployed.
_CHART_COMPONENT_DIR = Path(__file__).parent / "chart_component"
_chart_component = components.declare_component("trading_journal_chart", path=str(_CHART_COMPONENT_DIR))

# These stay separate from the chart's own candle colors below - they're
# used for *trade outcome* meaning elsewhere (win/loss stat tiles,
# entry/exit markers), which is a different thing from which way one
# day's candle moved. Don't repaint these to match the candle colors.
# Built-in defaults - see win_loss_color() below for the customizable
# version every call site in the app actually uses.
GOOD_COLOR = "#0ca30c"
CRITICAL_COLOR = "#d03b3b"
MUTED_COLOR = "#898781"


@st.cache_data(ttl=300, show_spinner=False)
def _load_trade_colors():
    """
    Cached read of the saved win/loss color override (see database.
    get_trade_colors()) - win_loss_color() below calls this on every
    use, so it's cached the same way _load_accent_color() caches its
    own read. Cached 5 minutes; clear_trade_colors_cache() is called
    right after a save/reset on the Settings page so the CURRENT
    session sees the change immediately.
    """
    conn = database.get_connection()
    return database.get_trade_colors(conn)


def clear_trade_colors_cache():
    """Clears the cached win/loss color read - call right after saving
    or resetting it on the Settings page (see _load_trade_colors())."""
    _load_trade_colors.clear()


def win_loss_color(is_win):
    """
    Returns the (possibly customized) color for a gain (is_win=True) or
    a loss (is_win=False) - the single thing every stat tile, chart
    marker, and bar in the app that colors by trade outcome should call,
    instead of picking between GOOD_COLOR/CRITICAL_COLOR directly. Falls
    back to those same built-in constants until a custom pair is saved
    on the Settings page (see database.get_trade_colors()/
    save_trade_colors()).
    """
    colors = _load_trade_colors()
    if is_win:
        return colors["good"] or GOOD_COLOR
    return colors["critical"] or CRITICAL_COLOR

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

# Sent to chart_component/index.html on every render (see its own
# applyTheme()) so its CSS/JS theme colors come from these Python
# constants instead of a second copy hand-kept-in-sync inside that
# static file - this project's actual single source of chart theme
# truth is this module, not the HTML.
_COMPONENT_THEME_COLORS = {
    "bg": CHART_BACKGROUND,
    "text": CHART_TEXT_COLOR,
    "grid": GRIDLINE_COLOR,
    "accent": CATEGORICAL_PALETTE[0],
    "good": GOOD_COLOR,
    "critical": CRITICAL_COLOR,
}


@st.cache_data(ttl=300, show_spinner=False)
def _load_accent_color():
    """
    Cached read of the saved chart accent color override (see database.
    get_accent_color()/pages/5_Settings.py) - _component_theme_colors()
    below calls this on every chart render, so it's cached the same way
    nav.py's own _load_background_image() caches the (also read on
    every page) background image. Cached 5 minutes; clear_accent_color_
    cache() is called right after a save/reset on the Settings page so
    the CURRENT session sees the change immediately.
    """
    conn = database.get_connection()
    return database.get_accent_color(conn)


def clear_accent_color_cache():
    """Clears the cached accent color read - call right after saving or
    resetting it on the Settings page (see _load_accent_color())."""
    _load_accent_color.clear()


def _component_theme_colors():
    """
    Colors sent to chart_component/index.html on every render (see
    render_interactive_chart() below). Only `accent` is ever different
    from _COMPONENT_THEME_COLORS' fixed defaults - it's the one part of
    this app's look that's user-customizable (Settings page), since
    native Streamlit widgets elsewhere can't be recolored per-session,
    only this custom component's own CSS can.
    """
    colors = dict(_COMPONENT_THEME_COLORS)
    accent = _load_accent_color()
    if accent:
        colors["accent"] = accent
    return colors

# Timeframes offered, and the default VISIBLE calendar-day window when
# the chart first opens (30 Min 5 days, Hourly 15, Daily 120, Weekly
# ~2 years, Monthly a year) - a coarser timeframe needs much more of a window to
# show a meaningful number of bars. There's no user-adjustable slider for
# this anymore - scroll-to-zoom on the chart itself replaces it. This is
# NOT how much data gets fetched - see FETCH_BUFFER_MULTIPLIER below -
# just what's shown by default before you zoom/pan.
TIMEFRAMES = {
    # 30 Min's padding is deliberately smaller than Hourly's - Yahoo
    # Finance hard-caps 30-minute data at 60 days back from TODAY (all
    # or nothing, not a graceful truncation - confirmed directly), and
    # this padding feeds into how far back the fetch window reaches
    # (see FETCH_BUFFER_MULTIPLIER/LOOKBACK_DAYS_PER_PERIOD below), so
    # too generous a number here makes 30-min charts go completely
    # empty for any trade older than just a week or two. Kept at 5
    # (rather than a wider default window) specifically to prioritize
    # how OLD a trade can still be and show something at all - 5 keeps
    # a trade up to ~39 days old inside that 60-day budget (~30 days at
    # 8, the value this was briefly bumped to and then reverted from).
    # Hourly has a much bigger 730-day budget, so 15 there is no risk.
    "30 Min": ("30m", 5),
    "Hourly": ("1h", 15),
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
LOOKBACK_DAYS_PER_PERIOD = {"30m": 0.125, "1h": 0.25, "1d": 1.6, "1wk": 8, "1mo": 32}

# How many blank candle-widths of room to leave after the last real bar
# when a chart first opens, so it doesn't sit jammed against the right
# edge - a fixed CANDLE count rather than a fixed number of days, since
# how many calendar days that works out to varies a lot by timeframe
# (10 daily candles is 2 weeks; 10 monthly candles is most of a year).
RIGHT_MARGIN_CANDLES = 10


def right_margin_timedelta(interval):
    """The extra calendar-day span RIGHT_MARGIN_CANDLES actually works
    out to for a given interval - add this to whatever a chart's own
    visible_end would otherwise be. Reuses LOOKBACK_DAYS_PER_PERIOD's
    existing calendar-days-per-bar figures rather than a second set of
    numbers that could drift out of sync with those."""
    return timedelta(days=RIGHT_MARGIN_CANDLES * LOOKBACK_DAYS_PER_PERIOD[interval])

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
    "ma_type": "SMA",
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
# ARCHIVE_VISIBLE_TRADING_DAYS, so today doesn't sit jammed right against
# the right edge. Was 1/3 (~2 empty months trailing off the chart) until
# the user asked for just a little breathing room instead, once that
# much blank space started showing up in tweeted chart images - 1/12
# gives about 8% extra width, e.g. 110 real candles plus ~9 blank ones
# puts today at 110/119, about 92%.
ARCHIVE_RIGHT_MARGIN_FRACTION = 1 / 12


def parse_ma_periods(text):
    """Turns something like "20, 50, 200" into [20, 50, 200], ignoring
    blanks, non-numbers, zero/negative numbers, and duplicates."""
    periods = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and int(part) not in periods:
            periods.append(int(part))
    return sorted(periods)


def warm_price_cache_for_symbol(symbol):
    """
    Fetches 5 years of daily history for `symbol` and stores it in the
    persistent price_cache table (see database.save_cached_price_history()),
    tagged with today's date. Called by warm_price_cache.py (a scheduled
    job, shortly after market close) for every tracked symbol, and
    immediately when a new ticker is added to a watchlist (see
    pages/2_Shortlist.py) - either way, the point is that by the time a
    chart actually gets opened, fetch_history() can serve it from here
    instead of a live Yahoo Finance call.

    5 years is comfortably wider than any realistic entry date this
    project's account history would have, so fetch_history()'s own
    `fetch_start` (which reaches further back than what's visible, for
    scroll buffer) is virtually always covered. If some rarer case ever
    needs further back than that, fetch_history() just falls back to a
    live fetch for that one case, same as before this cache existed.

    Skips writing anything if the fetch doesn't actually reach through
    today - a run before today's close (a manual test, or the scheduled
    job somehow firing early) would otherwise tag yesterday's data as
    "fresh for today," and _daily_history_from_cache() would then keep
    serving that stale snapshot for the rest of the day even after
    today's real close becomes available, with nothing left to correct
    it. Leaving the old cache row (or no row) alone here just means
    fetch_history() falls back to a live fetch, same as before this
    symbol was ever cached.

    Returns "ok" if it was cached. Otherwise returns a short reason why
    not, so callers can tell "Yahoo Finance itself couldn't be reached"
    apart from "the data just isn't ready yet" instead of lumping both
    into one generic failure (this used to be a plain True/False,
    which meant a rate-limit or network error showed the user the same
    misleading "not finalized yet" message as genuinely-incomplete
    data - see history_error_message() above for the same distinction
    fetch_history() already makes):
    - "rate_limited": Yahoo Finance is temporarily blocking requests.
    - "fetch_failed": some other network/API problem.
    - "no_data": Yahoo Finance has no data at all for this symbol
      (delisted, bad ticker, etc.).
    - "not_ready": the most recent trading day's close isn't in the
      data yet, or has incomplete Open/High/Low/Close values.
    Callers print/skip accordingly rather than treating any of these
    as fatal.
    """
    history = None
    for attempt in range(2):
        try:
            history = yf.Ticker(symbol).history(period="5y", interval="1d")
            break
        except YFRateLimitError:
            # A rate limit often clears within a couple seconds, so
            # one retry after a short pause is worth it before giving
            # up and asking the user to click the button again later.
            if attempt == 0:
                time.sleep(2)
                continue
            return "rate_limited"
        except Exception:
            return "fetch_failed"

    if history.empty:
        return "no_data"

    history.index = history.index.tz_localize(None)
    expected_day = timeutil.expected_last_trading_day()
    if history.index[-1].date() != expected_day:
        return "not_ready"

    # Yahoo Finance sometimes reports a day's Volume before that same
    # day's Open/High/Low/Close are actually finalized - a real gap
    # that used to make the interactive chart's most recent candle
    # fail to render (see chart_component/index.html's handling of a
    # null OHLC point). Refusing to cache a bar with that gap here
    # means today's row only ever gets written once Yahoo's own data
    # is actually complete, instead of permanently locking in an
    # incomplete snapshot as "fresh for today" the first time this
    # runs after a market close.
    last_bar = history.iloc[-1]
    if last_bar[["Open", "High", "Low", "Close"]].isna().any():
        return "not_ready"

    # Tagged with the TRADING DAY this fetch covers (expected_day),
    # not timeutil.today_eastern() - see expected_last_trading_day()'s
    # own docstring for why using the literal calendar day here used
    # to silently break this cache for days at a time whenever this
    # scheduled job's actual run landed after midnight Eastern.
    history_dict = {
        "dates": [d.date().isoformat() for d in history.index],
        "open": history["Open"].tolist(),
        "high": history["High"].tolist(),
        "low": history["Low"].tolist(),
        "close": history["Close"].tolist(),
        "volume": history["Volume"].tolist(),
    }
    conn = database.get_connection()
    database.save_cached_price_history(conn, symbol, history_dict, expected_day)
    return "ok"


def _daily_history_from_cache(symbol, needed_start):
    """
    Returns a daily-interval DataFrame (Open/High/Low/Close/Volume,
    shaped exactly like a live yfinance fetch) from the persistent
    price_cache table, or None if there's nothing usable there - no
    cached row at all, it doesn't cover the most recent expected
    trading day, or it doesn't reach back as far as `needed_start`
    (fetch_history()'s own `fetch_start`). A None return means "fall
    back to a live fetch," same as if this cache didn't exist.

    Compares against timeutil.expected_last_trading_day() rather than
    timeutil.today_eastern() - the cached row is tagged with the
    TRADING DAY it covers (see warm_price_cache_for_symbol()), which on
    a Saturday/Sunday is still Friday, not the literal weekend date.
    Comparing against literal "today" here would make a perfectly
    current weekend cache look stale and force a needless live fetch.

    Doesn't separately re-check that the cache includes the most
    recent close - warm_price_cache_for_symbol() already guarantees
    that by refusing to write anything unless its fetch actually
    reached through the expected trading day, so a matching
    `fetched_for_date` here always has that close in it. That split is
    what avoids re-litigating Yahoo's own publish timing on every
    chart load: a warm run before today's close just skips writing
    (leaving whatever's here, or nothing), rather than this function
    having to guess whether a same-day row is trustworthy.
    """
    conn = database.get_connection()
    cached = database.get_cached_price_history(conn, symbol)
    if cached is None or cached["fetched_for_date"] != timeutil.expected_last_trading_day():
        return None

    hist = cached["history"]
    if not hist["dates"]:
        return None

    df = pd.DataFrame({
        "Open": hist["open"], "High": hist["high"], "Low": hist["low"],
        "Close": hist["close"], "Volume": hist["volume"],
    }, index=pd.to_datetime(hist["dates"]))

    if df.index.min() > pd.Timestamp(needed_start):
        return None  # doesn't reach back far enough

    return df


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_history(symbol, fetch_start, display_start, display_end, interval, ma_periods, ma_type="SMA"):
    """
    Fetches price history from `fetch_start` (which includes extra lookback
    for moving averages) through `display_end`, computes any requested
    moving averages, then trims back down to `display_start` so the chart
    only shows the intended window. Returns an empty DataFrame if no data
    was found (symbol invalid/delisted, Yahoo Finance has no data for the
    range, or the request failed/got rate-limited) - callers are
    responsible for handling that case. When the empty DataFrame is due to
    a rate limit specifically, `.attrs["error"] = "rate_limited"` is set on
    it, so history_error_message() below can explain that accurately
    instead of implying the symbol itself is bad.

    `ma_type` is "SMA" (a plain rolling average - the default, and what
    ma_strategy.py's MA Stop Rule always asks for regardless of this
    app's chart display preference) or "EMA" (an exponential moving
    average, which weights recent closes more heavily instead of
    treating every day in the window equally).

    Cached for an hour (matching fetch_daily_closes()'s own reasoning):
    the fetched range is almost entirely PAST days, which never change,
    plus one still-moving "today" candle. Without this, every rerun of
    a page showing a chart - not just switching tickers, but ANY
    interaction on the same one (a drawing, a Chart Settings tweak, even
    just the Journal Session's own internal reruns) - re-fetched the
    exact same data from Yahoo Finance from scratch. That's what made
    stepping through a guided Journal Session feel slow: every single
    ticker paid a full live fetch, one at a time, with nothing reused
    even for a ticker you'd just been looking at seconds before.

    For the Daily interval specifically, this ALSO checks the
    persistent price_cache table (see warm_price_cache_for_symbol())
    before ever calling Yahoo Finance - warmed shortly after market
    close and whenever a new ticker is added to a watchlist, so even
    the very FIRST time this hour's st.cache_data is empty for a given
    symbol (e.g. the first chart you open in an evening Journal
    Session), there's still a good chance this doesn't hit the network
    at all. 30 Min/Hourly/Weekly/Monthly aren't pre-warmed - Daily is
    the default view (see TIMEFRAMES), so it covers the common case
    without the warming job needing to fetch several times as much per
    symbol.
    """
    history = None
    if interval == "1d":
        history = _daily_history_from_cache(symbol, fetch_start)

    if history is None:
        try:
            history = yf.Ticker(symbol).history(start=fetch_start, end=display_end, interval=interval)
        except YFRateLimitError:
            empty = pd.DataFrame()
            empty.attrs["error"] = "rate_limited"
            return empty
        except Exception:
            return pd.DataFrame()

        if history.empty:
            return history

        # yfinance returns timezone-aware dates; the rest of this project's
        # dates are plain (timezone-less), so this lines them up.
        history.index = history.index.tz_localize(None)
    else:
        # From the persistent cache, which always runs through today -
        # trim the top end down to what THIS call actually asked for
        # (display_end can be earlier, e.g. mid-history for a closed
        # trade in Trade Analyzer).
        history = history[history.index <= display_end]

    for period in ma_periods:
        if ma_type == "EMA":
            # adjust=False matches how every other charting platform
            # computes an EMA (a simple recursive weighted average) -
            # adjust=True (pandas' own default) instead re-weights the
            # whole series every time using all history since the start
            # of the fetched data, which shifts the EMA's values
            # depending on how far back `fetch_start` happened to reach.
            history[f"MA{period}"] = history["Close"].ewm(span=period, adjust=False).mean()
        else:
            history[f"MA{period}"] = history["Close"].rolling(period).mean()

    return history[history.index >= display_start]


def history_error_message(history, symbol):
    """
    A plain-language explanation for why fetch_history() came back
    empty for `symbol` - distinguishes Yahoo Finance rate-limiting
    (transient, worth retrying in a bit) from every other reason
    (probably a bad or delisted ticker), using the "error" tag
    fetch_history() attaches to the empty DataFrame via .attrs.
    """
    if history.attrs.get("error") == "rate_limited":
        return (
            f"Yahoo Finance is rate-limiting requests right now, so {symbol}'s "
            "price data couldn't be fetched. Wait a minute or two and try again."
        )
    return (
        f"No price data found for {symbol} in this date range. "
        "It may be delisted, or Yahoo Finance may not have data for it."
    )


def _compute_rangebreaks(history, interval):
    """
    Returns Plotly x-axis "rangebreaks" that hide non-trading gaps -
    weekends, holidays, and (for intraday data) overnight hours - so
    candles/bars sit next to each other instead of leaving a visible
    blank stretch on the chart for every day the market was closed.

    Weekends are hidden for every interval. For daily bars, any other
    missing weekday (a holiday) is found by comparing the full business-day
    range against the dates actually present in `history` - computed fresh
    from the real data each time, rather than maintaining a holiday
    calendar by hand. For intraday bars (Hourly, 30 Min), the hours
    outside roughly 9:30am-4pm are hidden too, since a trading day is
    only open part of each day.
    """
    breaks = [dict(bounds=["sat", "mon"])]

    if interval == "1d" and len(history) > 1:
        all_weekdays = pd.bdate_range(history.index.min(), history.index.max())
        missing = all_weekdays.difference(history.index)
        if len(missing) > 0:
            # Plotly/kaleido's JSON serialization can't handle pandas
            # Timestamp objects directly - plain datetimes only.
            breaks.append(dict(values=list(missing.to_pydatetime())))
    elif interval in ("1h", "30m"):
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


@st.cache_data(ttl=60, show_spinner=False)
def warm_earnings_cache_for_symbol(symbol):
    """
    Looks up `symbol`'s next earnings date via yfinance and stores it in
    the persistent earnings_cache table (see
    database.save_cached_next_earnings_date()). Called by
    warm_price_cache.py in the same once-a-day, paced loop that already
    warms price history for every tracked symbol, so the Journal
    Session's "earnings within 10 days" badge (see build_figure()) reads
    an already-warm value instead of making its own live Yahoo Finance
    call during the session.

    Returns "ok" (cached something, possibly a NULL "no known upcoming
    date"), "rate_limited", or "fetch_failed" - same shape as
    warm_price_cache_for_symbol() so the caller can print/skip
    accordingly instead of treating every non-success the same way.
    """
    earnings = None
    for attempt in range(2):
        try:
            earnings = yf.Ticker(symbol).get_earnings_dates(limit=12)
            break
        except YFRateLimitError:
            # Same one-retry-after-a-short-pause approach as
            # warm_price_cache_for_symbol() - a rate limit often clears
            # within a couple seconds.
            if attempt == 0:
                time.sleep(2)
                continue
            return "rate_limited"
        except Exception:
            return "fetch_failed"

    conn = database.get_connection()
    if earnings is None or earnings.empty:
        # Not every symbol has upcoming earnings data (ETFs, for
        # instance) - caching a NULL here (rather than leaving no row at
        # all) still means "don't show the badge", same as a real date
        # more than 10 days out would.
        database.save_cached_next_earnings_date(conn, symbol, None)
        return "ok"

    # get_earnings_dates() returns both past and future estimated
    # dates in one DataFrame, not necessarily in a convenient order -
    # so this just filters for anything today-or-later and takes the
    # soonest, rather than trusting row order.
    index = earnings.index.tz_localize(None) if earnings.index.tz is not None else earnings.index
    today = timeutil.today_eastern()
    upcoming = sorted(d.date() for d in index if d.date() >= today)
    database.save_cached_next_earnings_date(conn, symbol, upcoming[0] if upcoming else None)
    return "ok"


def next_earnings_badge_info(conn, symbol, within_days=10):
    """
    Returns {"date": "YYYY-MM-DD", "days_until": N} if `symbol` has a
    cached next earnings date within `within_days` days from now
    (inclusive, never in the past), or None otherwise - the None case
    covers "no cached date," "no known upcoming earnings," and "earnings
    are further out than within_days" all the same way, since all three
    mean the same thing to a caller: don't show the badge.
    """
    next_date = database.get_cached_next_earnings_date(conn, symbol)
    if next_date is None:
        return None
    days_until = (next_date - timeutil.today_eastern()).days
    if not (0 <= days_until <= within_days):
        return None
    return {"date": next_date.isoformat(), "days_until": days_until}


# Hypothetical account allocations the Plan Entry/Plan Stop boxes (see
# pages/2_Shortlist.py's render_journal_box()) show equity risk for -
# there's no shares/position-size input, so "equity % loss" is shown as
# a small table of "if this were N% of my account" scenarios instead of
# one real number tied to an actual position size.
PLAN_ALLOCATION_PCTS = (10, 20, 30)


def plan_risk_metrics(entry_price, stop_price):
    """
    Turns a watchlist ticker's planned entry/stop prices into risk
    numbers for the Plan Entry/Plan Stop boxes, or None if either price
    is missing or `entry_price` isn't positive (nothing meaningful to
    divide by).

    `price_loss_pct` is direction-agnostic - plain |entry - stop| /
    entry - since a watchlist idea has no LONG/SHORT of its own yet
    (unlike an open position), so this works whether the plan is "buy
    above, stop below" or the reverse, without needing a direction
    toggle.

    `equity_loss_pcts` is {allocation%: equity_loss%} for each of
    PLAN_ALLOCATION_PCTS - "if this position were N% of my account,
    this stop costs me equity_loss% of my whole account" - just
    price_loss_pct scaled by the allocation, no actual share count or
    account dollar value needed.
    """
    if entry_price is None or stop_price is None or entry_price <= 0:
        return None
    price_loss_pct = abs(entry_price - stop_price) / entry_price * 100
    return {
        "price_loss_pct": price_loss_pct,
        "equity_loss_pcts": {
            allocation: price_loss_pct * allocation / 100
            for allocation in PLAN_ALLOCATION_PCTS
        },
    }


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
    over one symbol's failed lookup. One retry on a rate limit (same
    reasoning as warm_price_cache_for_symbol() above - it often clears
    within a couple seconds) before giving up, since with no retry at
    all a single passing rate-limit blip on one symbol showed up as a
    permanent-looking "no current price available" for that symbol
    until the minute-long cache below expired.

    Cached for just a minute, not the hour fetch_history() uses - this
    IS the still-moving "right now" price, so it needs to stay fresh.
    Even a minute is enough to stop the Journal Session's Skip/Save &
    Next from re-fetching a position's current price on every unrelated
    rerun while you're still looking at the same ticker.
    """
    for attempt in range(2):
        try:
            recent = yf.Ticker(symbol).history(period="5d")
        except YFRateLimitError:
            if attempt == 0:
                time.sleep(2)
                continue
            return None
        except Exception:
            return None
        return None if recent.empty else recent["Close"].iloc[-1]


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_earnings_dates_in_range(symbol, start_date, end_date):
    """
    Every earnings-release date for `symbol` (past or future) that falls
    within [start_date, end_date] (plain dates, inclusive) - used by
    build_figure()'s earnings marker (see its own docstring), not the
    "within 10 days" badge (that's warm_earnings_cache_for_symbol()'s
    persistent next_earnings_date, a completely different, forward-only
    lookup). yfinance has no native "give me dates between X and Y"
    query, only a `limit` on how many of the most-recent-around-now
    records to return - 20 (versus warm_earnings_cache_for_symbol()'s
    12, which only ever needs the next upcoming one) gives roughly 5
    years of quarterly-earnings headroom either direction, plenty for
    any trade this app would realistically be reviewing.

    Returns [] on any failure (bad symbol, rate-limited, no data) - same
    "a chart never crashes over one lookup" convention as
    fetch_latest_price() above. Cached a full day, not the hour
    fetch_history() uses - earnings dates don't change intraday.
    """
    try:
        earnings = yf.Ticker(symbol).get_earnings_dates(limit=20)
    except Exception:
        return []
    if earnings is None or earnings.empty:
        return []
    index = earnings.index.tz_localize(None) if earnings.index.tz is not None else earnings.index
    return sorted(d.date() for d in index if start_date <= d.date() <= end_date)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_period_return_pct(symbol, period_start):
    """
    A benchmark's own % price change from period_start through today -
    used by goals.py's "Equity Growth vs SPY/QQQ" goals to see whether
    the account is beating the market over the same stretch. Reuses
    fetch_history() (no moving averages needed) rather than a fresh
    yfinance call, so this gets the same persistent Daily price_cache/
    hour-long st.cache_data benefits every chart already relies on -
    cached for an hour here too, same reasoning as fetch_history()'s
    own docstring (mostly past days that never change).

    Returns None if there's no price data for the range at all (bad
    symbol, rate-limited, market hasn't opened yet this period) -
    callers already treat None as "can't compute this goal right now."
    """
    # fetch_history() compares its display_start against a DatetimeIndex
    # internally, so this needs to be a real datetime, not the plain
    # date period_start comes in as (same conversion ma_strategy.py's
    # own fetch_history() call already needs, for the same reason).
    start = datetime.combine(period_start, datetime.min.time())
    end = datetime.combine(timeutil.today_eastern(), datetime.min.time())
    history = fetch_history(symbol, start, start, end, "1d", [])
    if history.empty:
        return None
    first_close = history["Close"].iloc[0]
    last_close = history["Close"].iloc[-1]
    return (last_close - first_close) / first_close * 100


def get_calculated_account_value(conn):
    """
    Today's account value, built up from the Jan 1 baseline (database.
    get_account_value()) plus everything that's happened since:
    deposits, closed-trade P/L, and every open position's unrealized
    P/L right now - rather than needing the real number typed in and
    kept up to date by hand. Returns None if no Jan 1 baseline has been
    set yet (database.set_account_value()).

    Extracted from what used to be dashboard.py's own inline
    calculation (its Account Performance section) so goals.py's
    "Account Growth" goal computes this exactly the same way, instead
    of a second copy of this math drifting apart from the original.
    Always anchored at Jan 1 specifically - not a general "value as of
    any date" function, since unrealized P/L only means anything for
    RIGHT NOW (a past date's open positions would need historical
    prices this app doesn't track - see project notes on why a true
    historical account-value curve isn't built yet).
    """
    jan1_balance = database.get_account_value(conn)
    if not jan1_balance:
        return None

    jan1_date = date(timeutil.today_eastern().year, 1, 1)
    deposits = database.get_deposits(conn)
    deposits_this_year = sum(d["amount"] for d in deposits if d["deposit_date"] >= jan1_date)
    realized_pl_this_year = database.get_realized_pl_since(conn, jan1_date)

    total_unrealized_pl_now = 0.0
    for position in database.get_open_positions(conn):
        current_price = fetch_latest_price(position["symbol"])
        if current_price is None:
            continue
        cost_basis = position["avg_price"] * position["quantity"]
        current_value = current_price * position["quantity"]
        is_short = position["direction"] == "SHORT"
        total_unrealized_pl_now += (cost_basis - current_value) if is_short else (current_value - cost_basis)

    return jan1_balance + deposits_this_year + realized_pl_this_year + total_unrealized_pl_now


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_daily_closes(symbol, start, end):
    """
    Returns a daily closing-price Series for `symbol` from `start` to `end`
    (inclusive), reindexed onto every single calendar day and forward-filled
    - so a weekend or holiday carries the prior trading day's close instead
    of leaving a gap. Used to mark a position to market day-by-day for the
    Dashboard's equity curve (see build_mark_to_market_curve() there),
    rather than crediting a trade's entire gain to the single day it was
    sold. Empty Series if Yahoo Finance has no data for this symbol/range.

    Cached for an hour: the Dashboard's equity curve window selector
    (1M/3M/6M/etc.) reruns this whole page on every click, and price
    history for any day that's already passed never changes - only
    today's still-moving price does, so an hour-old cache is a fine
    tradeoff against re-fetching the same symbol's history from Yahoo
    Finance on every single widget interaction.
    """
    try:
        history = yf.Ticker(symbol).history(start=start, end=end + timedelta(days=1))
    except Exception:
        return pd.Series(dtype=float)
    if history.empty:
        return pd.Series(dtype=float)
    history.index = history.index.tz_localize(None)
    daily_index = pd.date_range(start=start, end=end, freq="D")
    return history["Close"].reindex(daily_index).ffill()


@st.cache_data(ttl=86400, show_spinner=False)
def _symbol_splits(symbol):
    """
    All-time split history for `symbol`, straight from yfinance -
    split out from split_adjustment_factor() below so the actual
    Yahoo Finance call happens once per SYMBOL and is cached that way,
    not once per (symbol, since) pair. yf.Ticker(...).splits already
    returns a symbol's entire split history regardless of any date
    filter, so calling it fresh for every distinct `since` a caller
    passed (e.g. once per trade in dashboard.py's equity curve, one
    call per differing entry date) was repeating the identical fetch.
    """
    try:
        return yf.Ticker(symbol).splits
    except Exception:
        return pd.Series(dtype=float)


def split_adjustment_factor(symbol, since):
    """
    Returns the cumulative stock-split ratio for every split `symbol` has
    had strictly after `since`, up through today - 1.0 if there weren't
    any, so callers can always multiply by this unconditionally.

    Needed because yf.Ticker(...).history() always returns prices
    restated in TODAY's post-split share terms (Yahoo rewrites its whole
    price history so a chart never shows a fake jump right on the split
    date) - a real trade price recorded on `since` reflects the share
    count that existed THEN, not today's. SQQQ (a leveraged ETF prone to
    periodic reverse splits from value decay) is what surfaced this: a
    trade from before one of its reverse splits showed an almost
    100,000% one-day "gain" in the equity curve, purely from comparing a
    real, un-adjusted historical entry price against yfinance's already
    split-adjusted daily closes - not a real price move at all. Dividing
    a stored entry price by this factor (and multiplying quantity by it)
    converts it into the same today's-share-terms convention
    fetch_daily_closes() already returns, before the two get compared.

    The underlying fetch (_symbol_splits() above) is cached for a day
    (splits are rare, announced well in advance, and won't change
    mid-session) - the `since`-based filtering itself is cheap enough
    (a plain in-memory filter over one symbol's small split list) not
    to need its own cache.
    """
    splits = _symbol_splits(symbol)
    if splits.empty:
        return 1.0
    since_ts = pd.Timestamp(since)
    if since_ts.tzinfo is None and splits.index.tz is not None:
        since_ts = since_ts.tz_localize(splits.index.tz)
    later_splits = splits[splits.index > since_ts]
    return float(later_splits.prod()) if not later_splits.empty else 1.0


def style_simple_chart(fig, value_axis_title, height=350, horizontal=False):
    """
    Applies this app's shared dark look to a plain bar/line figure -
    the Dashboard's summary charts and the Open Positions charts all
    use this (the full price charts have their own styling inside
    build_figure()). Returns the same figure, styled, so it can be
    passed straight into st.plotly_chart(). Until this existed, the
    exact same styling block was copy-pasted around both pages.

    Set horizontal=True for a horizontal bar chart (orientation="h" on
    the trace itself) - the dollar/value axis is x instead of y there,
    so value_axis_title labels the x axis instead of the y axis.
    """
    fig.update_layout(
        height=height,
        margin=dict(t=10, b=45),
        plot_bgcolor=CHART_BACKGROUND,
        paper_bgcolor=CHART_BACKGROUND,
        font=dict(color=CHART_TEXT_COLOR),
    )
    if horizontal:
        fig.update_layout(xaxis_title=value_axis_title)
        # A bar's "outside" text label sits just past its own end - for
        # whichever bar is closest to the axis max, that label has no
        # room left and gets clipped at the plot's edge. Padding the
        # range a bit past the largest value leaves room for it.
        values = [v for trace in fig.data for v in (trace.x or []) if v is not None]
        if values:
            fig.update_xaxes(range=[min(0, min(values)), max(values) * 1.2])
    else:
        fig.update_layout(yaxis_title=value_axis_title)
    fig.update_xaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=GRIDLINE_COLOR, showgrid=True, zeroline=False)
    return fig


def render_png(fig, width=1200, scale=1.5):
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
    once Streamlit stretches it to fill a much wider page. Physical
    pixel width is width*scale (1800px here) - enough to stay sharp at
    typical page widths without the excess of the original 1400/2
    (2800px) combo, which was costing real Neon storage: every archived
    Logbook chart is a permanent BYTEA row that never goes away on its
    own, and at 641 charts already sitting at ~256KB average, image
    size directly drives how much runway the database's storage limit
    has left. This combo renders visually indistinguishable from the
    old one at normal viewing sizes while cutting file size by roughly
    a third - confirmed by comparing real renders side by side, not
    just resolution math.
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


def build_archive_snapshot(symbol, entry_date, buy_price, entry_label, as_of, direction="LONG", stop_loss=None):
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
    `stop_loss`, if given, draws the same thin grey stop line the
    interactive chart shows. Also includes this symbol's saved
    drawings (see database.get_drawings()), the same as the
    interactive chart. Returns PNG bytes, or None if no price data was
    found, OR if the most recent trading day's candle isn't actually in
    the fetched data yet - see the completeness check right after the
    fetch below.
    """
    conn = database.get_connection()
    saved_prefs = database.get_chart_preferences(conn)
    drawings = database.get_drawings(conn, symbol)
    ma_periods = parse_ma_periods(saved_prefs["ma_text"])
    ma_colors = {
        period: saved_prefs["ma_colors"].get(str(period), CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)])
        for i, period in enumerate(ma_periods)
    }
    settings = {
        **DEFAULT_SETTINGS, "ma_periods": ma_periods, "ma_colors": ma_colors,
        "ma_type": saved_prefs["ma_type"],
    }

    visible_days = ARCHIVE_VISIBLE_TRADING_DAYS * LOOKBACK_DAYS_PER_PERIOD["1d"]
    display_start = as_of - timedelta(days=visible_days)
    display_end = as_of + timedelta(days=1)

    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * LOOKBACK_DAYS_PER_PERIOD["1d"]
    fetch_start = display_start - timedelta(days=lookback_days)

    history = fetch_history(
        symbol, fetch_start, display_start, display_end, "1d", settings["ma_periods"], settings["ma_type"])
    if history.empty:
        return None

    # A real production bug: Yahoo Finance sometimes hasn't published a
    # trading day's finalized daily candle yet at the moment this runs
    # (right after close, or a holiday/weekend with no new candle at
    # all) - fetch_history() then silently returns data that only
    # reaches through the PREVIOUS trading day, with no error of any
    # kind. Without this check, that got baked into the Logbook as if
    # it were a complete "as of today" chart, permanently missing
    # today's candle - confirmed for real against a SNOW journal entry
    # whose archived chart was actually one day stale. Refusing here
    # (same as the "no data at all" case above) means the caller's
    # existing "couldn't build a chart" handling kicks in instead - for
    # the live Tweet flow, a clear message and no chart image; for the
    # nightly job, the entry is simply retried on its next run once
    # the real data is ready.
    if history.index[-1].date() != as_of.date():
        return None

    entry_point = {"entry_date": entry_date, "buy_price": buy_price, "direction": direction} if buy_price is not None \
        else {"entry_date": entry_date, "buy_price": price_near_date(history, entry_date), "direction": direction}

    margin_days = visible_days * ARCHIVE_RIGHT_MARGIN_FRACTION
    visible_range = (display_start, display_end + timedelta(days=margin_days))

    fig, _fit_payload = build_figure(
        symbol, history, entry_point, settings, entry_label=entry_label, visible_range=visible_range,
        stop_loss=stop_loss, drawings=drawings, include_summary_header=True)
    return render_png(fig)


def build_trade_review_snapshot(
    symbol, entry_date, exit_date, buy_price, sell_price, direction, timeframe_label, settings,
    view_range_override=None,
):
    """
    Builds a chart snapshot for the Trade Review Session (see
    pages/1_Trade_Analyzer.py), at whichever timeframe was on screen
    when "Save this timeframe" was clicked - a DIFFERENT shape from
    build_archive_snapshot() above, which is anchored to "today" and
    fixed to Daily for the nightly Logbook archive. This one is
    anchored to the TRADE's own entry/exit dates instead, at a
    caller-chosen timeframe (30 Min/Hourly/Daily/Weekly/Monthly - see
    TIMEFRAMES), since a review is about what happened around that
    specific trade, not a rolling window ending today. The visible-range/
    fetch-window math mirrors pages/1_Trade_Analyzer.py's own
    interactive chart exactly, just extracted so it can run once per
    timeframe instead of once per page load.

    `settings` is whatever the review session's Timeframe/Chart Settings
    controls currently show (MA periods/type, price scale) - the
    snapshot captures what's actually on screen, not a separately-saved
    preference set. Unlike build_archive_snapshot(), this doesn't
    include an overlay ticker - a review snapshot is about this trade's
    own price action, and keeping this simple avoids a second live fetch
    per snapshot. Saved drawings for the symbol ARE included, same as
    every other chart in this app. Returns PNG bytes, or None if no
    price data was found for this timeframe/window.

    `view_range_override`, if given, is a (visible_start, visible_end)
    pair of real datetimes - whatever the interactive chart's OWN
    x-axis range currently is (see render_interactive_chart()'s
    "view_range" in its returned dict) - used INSTEAD of the usual
    entry/exit-date-derived default below. This is what lets "Save this
    timeframe" (pages/1_Trade_Analyzer.py's Review Session) capture
    wherever you've actually panned/zoomed to, rather than always
    resetting to the default view.
    """
    conn = database.get_connection()
    drawings = database.get_drawings(conn, symbol)

    interval, padding_days = TIMEFRAMES[timeframe_label]
    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * LOOKBACK_DAYS_PER_PERIOD[interval]

    if view_range_override is not None:
        visible_start, visible_end = view_range_override
        # No FETCH_BUFFER_MULTIPLIER-wide fetch needed here - that
        # buffer exists to support panning/zooming a FRESH default view
        # on the interactive chart; this is reproducing an already-
        # chosen view for a static image, so only real moving-average
        # warm-up before visible_start is needed, nothing wider.
        fetch_start = visible_start - timedelta(days=lookback_days)
    else:
        visible_start = entry_date - timedelta(days=padding_days)
        # + right_margin_timedelta() so the last real candle doesn't sit
        # jammed against the right edge of the chart - see its own docstring.
        visible_end = exit_date + timedelta(days=padding_days) + right_margin_timedelta(interval)

        # Fetched (not necessarily DISPLAYED - see below) starting this
        # much further back, so the longest selected moving average
        # already has a full real warm-up window by the time
        # visible_start begins, instead of only "warming up" partway
        # through the visible chart.
        fetch_padding_days = padding_days * FETCH_BUFFER_MULTIPLIER
        wide_start = entry_date - timedelta(days=fetch_padding_days)
        fetch_start = wide_start - timedelta(days=lookback_days)

    # Trimmed to just outside the visible window here (a 1-day margin,
    # not all the way out to wide_start/FETCH_BUFFER_MULTIPLIER) - this
    # is a STATIC snapshot, never scrolled/panned like the interactive
    # chart, so it doesn't need the wider buffer that exists for that.
    # Keeping it anyway was a real bug: a candlestick trace holding much
    # more data than the figure's xaxis "range" actually shows, combined
    # with the rangebreaks that hide weekends/overnight hours, confused
    # Plotly/Kaleido's static rendering into leaving a large blank gap
    # (moving-average lines visible, candlesticks missing) on the left
    # side of the image - confirmed by comparing a wide-trimmed render
    # against this same tightly-trimmed one.
    history = fetch_history(
        symbol, fetch_start, visible_start - timedelta(days=1), visible_end + timedelta(days=1),
        interval, settings["ma_periods"], settings["ma_type"])
    if history.empty:
        return None

    entry_point = {
        "entry_date": entry_date, "buy_price": buy_price,
        "exit_date": exit_date, "sell_price": sell_price, "direction": direction,
    }
    entry_label = "Short Entry" if direction == "SHORT" else "Entry"

    fig, _fit_payload = build_figure(
        symbol, history, entry_point, settings, entry_label=entry_label, interval=interval,
        visible_range=(visible_start, visible_end), drawings=drawings, show_earnings_markers=True,
        include_summary_header=True)
    return render_png(fig)


def color_input(container, label, default_color, key):
    """
    A hex-code text box with a small color-swatch preview underneath,
    used everywhere in this toolbar instead of st.color_picker.
    st.color_picker's own picker panel (the gradient square/hue slider)
    renders as a floating overlay Streamlit doesn't count as "inside"
    whatever container it's nested in - so inside this toolbar's Chart
    Settings expander, clicking that panel closed the whole expander
    before the picked color ever took effect (a Streamlit platform
    quirk, not something fixable from here). A plain text box has no
    floating panel of its own, so it can't run into that.

    Not module-private (no longer has a leading underscore) - the
    Settings page reuses this same widget for the app's chart accent
    color (see get_accent_color()/_component_theme_colors() below)
    rather than building a second color-input pattern from scratch.
    """
    color = container.text_input(label, value=default_color, key=key, max_chars=7)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = default_color
    container.markdown(
        f'<div style="width:100%;height:0.6rem;border-radius:3px;'
        f'background:{color};margin-top:-0.6rem;"></div>',
        unsafe_allow_html=True,
    )
    return color


def render_settings_toolbar(container, key_prefix):
    """
    Renders the "Chart Settings" panel (chart type, colors, price scale,
    moving averages, overlay ticker) and returns a settings dict shaped
    like DEFAULT_SETTINGS above. Used by any page that wants the same
    interactive Chart Settings experience Trade Analyzer introduced.

    This is an expander, not a popover - a popover auto-closes on any
    click it considers "outside" itself, which caused the same kind of
    problem color_input() below is guarding against (see its docstring).
    An expander only closes when you click its own header again.

    `key_prefix` must be unique to the caller (e.g. "position", "watchlist",
    "trade_analyzer") - Shortlist's Open Positions and Watchlist sections
    each render their own copy of this toolbar on the same page, and
    every widget needs a distinct key or Streamlit raises a
    StreamlitDuplicateElementId error.

    Moving averages are the one setting that's saved permanently (see
    database.get_chart_preferences()/save_chart_preferences()) - typing
    in a new set of periods, changing a color, or switching SMA/EMA
    saves it right away, so it's still there next time the app is
    opened, on any device, until it's changed again. Everything else
    here (chart type, candle colors, price scale, overlay ticker) stays
    session-only, as before.
    """
    conn = database.get_connection()
    saved_prefs = database.get_chart_preferences(conn)

    with container.expander("Chart Settings"):
        chart_type = st.radio(
            "Chart Type", ["Candlestick", "Line"], horizontal=True, key=f"{key_prefix}_chart_type")
        price_scale = st.radio(
            "Price Scale", ["Linear", "Log"], horizontal=True, key=f"{key_prefix}_price_scale")

        if chart_type == "Candlestick":
            candle_cols = st.columns(2)
            up_color = color_input(candle_cols[0], "Bullish candle", UP_CANDLE_COLOR, f"{key_prefix}_up_color")
            down_color = color_input(candle_cols[1], "Bearish candle", DOWN_CANDLE_COLOR, f"{key_prefix}_down_color")
            line_color = None
        else:
            up_color = down_color = None
            line_color = color_input(st, "Line color", CATEGORICAL_PALETTE[0], f"{key_prefix}_line_color")

        ma_text = st.text_input(
            "Moving Averages (comma-separated periods)", value=saved_prefs["ma_text"],
            placeholder="e.g. 9, 21, 50", key=f"{key_prefix}_ma_text_input",
        )
        ma_periods = parse_ma_periods(ma_text)

        ma_type = st.radio(
            "Moving Average Type", ["SMA", "EMA"],
            index=["SMA", "EMA"].index(saved_prefs["ma_type"]),
            horizontal=True, key=f"{key_prefix}_ma_type",
        )

        ma_colors = {}
        if ma_periods:
            ma_color_cols = st.columns(len(ma_periods))
            for i, period in enumerate(ma_periods):
                default_color = saved_prefs["ma_colors"].get(
                    str(period), CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)])
                ma_colors[period] = color_input(
                    ma_color_cols[i], f"{period}-period", default_color, f"{key_prefix}_ma_color_{period}")

        current_colors = {str(period): color for period, color in ma_colors.items()}
        if (ma_text != saved_prefs["ma_text"] or current_colors != saved_prefs["ma_colors"]
                or ma_type != saved_prefs["ma_type"]):
            database.save_chart_preferences(conn, ma_text, ma_colors, ma_type)

        overlay_symbol = st.text_input(
            "Overlay Ticker (optional)", value="", placeholder="e.g. SPY, QQQ",
            key=f"{key_prefix}_overlay_symbol",
        ).strip().upper()
        overlay_color = None
        if overlay_symbol:
            overlay_color = color_input(st, "Overlay color", CATEGORICAL_PALETTE[4], f"{key_prefix}_overlay_color")
            st.caption(
                "The overlay ticker gets its own price scale on the left "
                "(the dashed line) - comparing two different stocks' dollar "
                "prices on the SAME scale wouldn't mean anything, since "
                "they're not likely to trade anywhere near each other."
            )

    return {
        "chart_type": chart_type,
        "price_scale": price_scale,
        "up_color": up_color,
        "down_color": down_color,
        "line_color": line_color,
        "ma_periods": ma_periods,
        "ma_colors": ma_colors,
        "ma_type": ma_type,
        "overlay_symbol": overlay_symbol,
        "overlay_color": overlay_color,
    }


def _format_trade_moment(dt):
    """Formats an entry/exit datetime for a marker's hover text - just
    the date if `dt` has no real time-of-day attached, otherwise the
    date AND the actual execution time. A CSV import (Fidelity/Schwab
    exports are date-only) or anything recorded before SnapTrade's real
    timestamps existed always lands on exact midnight - a real stock
    trade essentially never executes at literally 00:00:00, so that's
    used as the signal that no genuine time is known here, rather than
    tracking a separate has-time flag alongside every date."""
    if dt.time() == dtime(0, 0):
        return f"{dt:%b %d, %Y}"
    return f"{dt:%b %d, %Y %I:%M %p}"


def _format_price_for_header(value):
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:,.2f}"


def _format_volume_for_header(value):
    if value is None or pd.isna(value):
        return "N/A"
    if value >= 1e9:
        return f"{value / 1e9:.1f}B"
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}K"
    return f"{value:,.0f}"


def _summary_header_text(history, symbol, interval_label):
    """
    A frozen, one-line version of the interactive chart's own live
    "summary line" (see chart_component/index.html's showDailyInfo()) -
    same wording/format, but for the LAST real bar in `history` instead
    of whichever bar the cursor is hovering, since a static PNG has no
    cursor. Used for the archived Logbook/Trade Review/tweet images,
    which otherwise had no way to show what the chart itself is looking
    at (which day, and its OHLC) - only the interactive version had that.
    """
    last = history.iloc[-1]
    date_str = f"{last.name.strftime('%b')} {last.name.day}, {last.name.year}"
    parts = [
        f"{symbol} · {interval_label}",
        date_str,
        f"O {_format_price_for_header(last['Open'])}",
        f"H {_format_price_for_header(last['High'])}",
        f"L {_format_price_for_header(last['Low'])}",
        f"C {_format_price_for_header(last['Close'])}",
        f"V {_format_volume_for_header(last['Volume'])}",
    ]
    text = "    ".join(parts)

    if len(history) >= 2:
        prev_close = history.iloc[-2]["Close"]
        chg = last["Close"] - prev_close
        pct = (chg / prev_close) * 100 if prev_close else None
        if pct is not None and not pd.isna(pct):
            sign = "+" if chg >= 0 else ""
            color = GOOD_COLOR if chg >= 0 else CRITICAL_COLOR
            text += (
                f"    <span style='color:{color}'>"
                f"Chg {sign}{_format_price_for_header(chg)}"
                f"    Chg% {sign}{pct:.2f}%</span>"
            )
    return text


def build_figure(symbol, history, entry_point, settings, overlay_history=None, entry_label="Entry", interval="1d",
                  visible_range=None, stop_loss=None, drawings=None, bake_arrow_traces=True,
                  show_earnings_flag=False, conn=None, plan_entry_price=None, plan_stop_price=None,
                  show_earnings_markers=False, include_summary_header=False):
    """
    Builds the go.Figure for a price chart: candlestick or line, moving
    averages, an entry marker (plus an exit marker and connecting line if
    the trade is already closed), a volume panel, and an optional overlay
    ticker plotted against its own secondary price scale (see
    secondary_y in the subplot specs below - a straight dollar-for-dollar
    comparison against the primary symbol's scale wouldn't mean much,
    since two different stocks are rarely anywhere near the same price).
    Returns `(fig, fit_payload)` - `fig` gets rendered via
    render_interactive_chart() (or, for a static image, render_png());
    `fit_payload` is a plain-JSON record of the real price/volume/overlay
    data render_interactive_chart()'s zoom handler needs to refit each
    y-axis as you scroll/pan (callers that don't need interactivity, like
    build_archive_snapshot(), can ignore it).

    `entry_label` names that marker - "Entry" for a real trade (the
    default), or something like "Added" for a watchlist ticker with no
    actual trade behind it. `interval` (the same string passed to
    fetch_history) decides which non-trading-day gaps get hidden - see
    _compute_rangebreaks(). `visible_range`, if given, is an
    (start, end) tuple setting the chart's initial zoomed-in view - useful
    when `history` itself covers a much wider window than should be shown
    by default (see FETCH_BUFFER_MULTIPLIER), so there's real data to
    scroll/zoom into on either side without an immediate empty edge.
    `stop_loss`, if given, draws a thin grey line at that price - only
    open positions have one (see database.get_stop_loss()).

    `plan_entry_price`/`plan_stop_price`, if given, draw a green/red line
    the same way - a watchlist ticker's saved "trading plan for next
    time" (see database.get_logbook_entry()'s plan_entry_price/
    plan_stop_price and pages/2_Shortlist.py's render_journal_box()).
    Independent of each other - either can be set without the other.

    `include_summary_header`, if True, bakes a frozen version of the
    interactive chart's live OHLC "summary line" (symbol/date/O/H/L/C/
    V/Chg) into the figure itself, for the LAST bar in `history` - see
    _summary_header_text(). Only the static-image builders
    (build_archive_snapshot(), build_trade_review_snapshot()) pass True;
    the interactive chart leaves this False since it already shows this
    same information in its own live, cursor-driven summary line
    (chart_component/index.html's #summary div) - baking it into the
    figure too would just duplicate it.

    `drawings`, if given, is a symbol's saved line/rect/arrow_up/
    arrow_down shapes (see database.get_drawings()). Line/rect ones are
    always baked in here, as native Plotly shapes. Up/down arrow
    markers are baked in too, but ONLY when `bake_arrow_traces` is True
    (the default) - meant for the ARCHIVED snapshot image
    (build_archive_snapshot(), which has no JS runtime to add anything
    client-side). The interactive chart component draws its own arrow
    markers client-side from the raw `drawings` list (see
    chart_component/index.html), so its callers pass
    `bake_arrow_traces=False` - baking them in too would make every
    single arrow placed/erased change this figure's data, which forces
    an unnecessary full chart rebuild (and a fresh price-history fetch)
    on every click instead of just a lightweight client-side redraw.

    `show_earnings_flag`, if True, adds an "earnings within 10 days"
    badge to the chart component's status bar (see
    next_earnings_badge_info() and chart_component/index.html's
    showDailyInfo()) - only the guided Journal Session passes this,
    so free-browsing a chart elsewhere in the app doesn't show it.
    Requires `conn` when True (falls back to database.get_connection()
    if not given).

    `show_earnings_markers`, if True, is a DIFFERENT feature from
    `show_earnings_flag` above - not a status-bar warning about an
    UPCOMING release, but a small red circled "E" marker plotted
    directly on the chart (see fetch_earnings_dates_in_range()) for
    EVERY earnings release, past or future, that falls within the
    visible window - useful context when reviewing a closed trade for
    understanding a big price move. Only Trade Analyzer's single-trade
    view and Review Session pass this (see pages/1_Trade_Analyzer.py's
    render_trade_chart()) - the Journal Session's watchlist-planning
    chart doesn't, since it isn't reviewing a past trade.
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
    # way (see match_trades_lifo() in analyze_trades.py for why), but
    # which one lines up with entry_date vs exit_date flips too.
    direction = entry_point.get("direction", "LONG")
    entry_symbol = "triangle-down" if direction == "SHORT" else "triangle-up"
    exit_symbol = "triangle-up" if direction == "SHORT" else "triangle-down"

    ma_periods = settings["ma_periods"]
    ma_colors = settings["ma_colors"]

    if is_closed:
        outcome_color = win_loss_color(sell_price >= buy_price)
    else:
        outcome_color = CATEGORICAL_PALETTE[0]

    has_overlay = overlay_history is not None and not overlay_history.empty

    # A plain-JSON (not Plotly's own, sometimes binary-encoded, figure
    # JSON) record of what the price/volume/overlay axes should fit at
    # any given visible time window - built directly from this data, not
    # by trying to parse Plotly's serialized traces back apart in
    # JavaScript. render_interactive_chart()'s zoom handler uses this to
    # recompute each y-axis range as you scroll/pan through time.
    fit_payload = {"price": [], "volume": [], "overlay": []}

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
    if show_earnings_flag:
        earnings_conn = conn if conn is not None else database.get_connection()
        earnings_info = next_earnings_badge_info(earnings_conn, symbol)
        if earnings_info is not None:
            fit_payload["meta"]["earnings"] = earnings_info

    # secondary_y=True on row 1 only is what makes an overlay ticker's
    # own price scale possible below - row 2 (volume) never needs one.
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25], vertical_spacing=0.03,
        specs=[[{"secondary_y": True}], [{}]],
    )

    for drawing in (drawings or []):
        if drawing["type"] not in ("line", "rect"):
            continue  # arrow_up/arrow_down are handled separately below - not a real Plotly shape type
        shape_kwargs = dict(
            type=drawing["type"], x0=drawing["x0"], y0=drawing["y0"], x1=drawing["x1"], y1=drawing["y1"],
            line=dict(color=drawing["color"], width=drawing["width"]), opacity=drawing["opacity"],
        )
        if drawing["type"] == "rect":
            shape_kwargs["fillcolor"] = drawing["color"]
        fig.add_shape(row=1, col=1, **shape_kwargs)

    # Up/down arrow markers aren't a real Plotly shape type (only
    # line/rect/circle/path are), so they're plotted the same way as
    # the entry/exit markers elsewhere in this file: a Scatter trace
    # using triangle-up/triangle-down marker symbols. The interactive
    # chart component re-draws these itself as you add/move/erase them
    # (see chart_component/index.html) - rendering them here too is
    # what makes them show up in the non-interactive archived Logbook
    # snapshot (build_archive_snapshot()), which has no JS runtime to
    # draw anything client-side.
    if bake_arrow_traces:
        for arrow_symbol, drawing_type in (("triangle-up", "arrow_up"), ("triangle-down", "arrow_down")):
            arrows = [d for d in (drawings or []) if d["type"] == drawing_type]
            if not arrows:
                continue
            fig.add_trace(go.Scatter(
                x=[d["x0"] for d in arrows], y=[d["y0"] for d in arrows], mode="markers",
                marker=dict(
                    symbol=arrow_symbol, size=[d["width"] * 5 + 6 for d in arrows],
                    color=[d["color"] for d in arrows], opacity=[d["opacity"] for d in arrows],
                ),
                showlegend=False, name=f"__{drawing_type}",
            ), row=1, col=1)

    fit_payload["price"].append({
        "x": [ts.isoformat() for ts in history.index],
        "lo": history["Low"].tolist(), "hi": history["High"].tolist(),
    })
    fit_payload["volume"].append({
        "x": [ts.isoformat() for ts in history.index],
        "lo": history["Volume"].tolist(), "hi": history["Volume"].tolist(),
    })

    # Entry/exit/"Added" markers sit in a fixed band near the bottom of
    # the price panel (just above the volume panel) instead of at the
    # trade's actual price - they used to sit right on the candles at
    # whatever price the trade happened to be at, which could land
    # anywhere in the panel and get lost among the candles/MAs.
    #
    # This value is NOT fed into fit_payload - render_interactive_chart's
    # fitYAxes() deliberately ignores it and instead repositions the
    # marker trace(s) itself, dynamically, just below whatever the price
    # axis's range already ended up being from the real candles alone
    # (see repositionMarkers() there). Including this point in the fit
    # calculation was tried first and was a real bug: since it sits well
    # below the actual candles, the axis stretched down to fit it too,
    # squashing the real price action into a thin band at the top of the
    # chart instead of using the full height.
    #
    # This Python-computed value is still what's actually used for the
    # non-interactive archived Logbook snapshot (build_archive_snapshot),
    # which has no JS runtime to reposition anything after the fact -
    # scoped to `visible_range` (the window actually shown), not the
    # full fetched history (which reaches further back than what's
    # visible - see FETCH_BUFFER_MULTIPLIER), so it lands close to right
    # for whatever's actually on screen instead of some old, unrelated
    # low from months before the visible window.
    range_history = history
    if visible_range is not None:
        windowed = history[(history.index >= visible_range[0]) & (history.index <= visible_range[1])]
        if not windowed.empty:
            range_history = windowed
    marker_y = range_history["Low"].min() - (range_history["High"].max() - range_history["Low"].min()) * 0.08

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
        # sell_price - buy_price is the per-share profit regardless of
        # direction - for a LONG that's literally sell minus buy; for a
        # SHORT, "sell_price" is the up-front short sale and "buy_price"
        # is the later cover, so a cheaper cover than the original sale
        # (a profit) already comes out positive the same way.
        pl_per_share = sell_price - buy_price
        entry_value, exit_value = (sell_price, buy_price) if direction == "SHORT" else (buy_price, sell_price)
        fig.add_trace(go.Scatter(
            x=[entry_date, exit_date], y=[marker_y, marker_y],
            mode="lines+markers",
            line=dict(color=outcome_color, width=2, dash="dot"),
            marker=dict(size=14, symbol=[entry_symbol, exit_symbol], color=outcome_color),
            name="Entry / Exit", showlegend=False, meta="entry_exit_marker",
            text=[
                f"{entry_label}<br>{_format_trade_moment(entry_date)} · ${entry_value:,.2f}",
                f"Exit<br>{_format_trade_moment(exit_date)} · ${exit_value:,.2f}<br>"
                f"{'+' if pl_per_share >= 0 else ''}${pl_per_share:,.2f}/share",
            ],
            hovertemplate="%{text}<extra></extra>",
        ), row=1, col=1)
    else:
        hover_text = (
            f"Added to watchlist<br>{_format_trade_moment(entry_date)}" if entry_label == "Added"
            else f"{entry_label}<br>{_format_trade_moment(entry_date)} · ${buy_price:,.2f}"
        )
        fig.add_trace(go.Scatter(
            x=[entry_date], y=[marker_y], mode="markers",
            marker=dict(size=14, symbol=entry_symbol, color=outcome_color),
            name=entry_label, showlegend=False, meta="entry_exit_marker",
            text=[hover_text],
            hovertemplate="%{text}<extra></extra>",
        ), row=1, col=1)

    if show_earnings_markers:
        earnings_dates = fetch_earnings_dates_in_range(
            symbol, range_history.index.min().date(), range_history.index.max().date())
        if earnings_dates:
            # A band BELOW the entry/exit markers (a bigger downward
            # offset than marker_y's own 0.08 - see its own comment
            # above) so the two never visually collide on a date they
            # share. Not fed into fit_payload, same reasoning as
            # marker_y itself - see repositionMarkers() in
            # chart_component/index.html for how this gets kept in
            # that same relative band as the user pans/zooms.
            earnings_marker_y = marker_y - (range_history["High"].max() - range_history["Low"].min()) * 0.06
            fig.add_trace(go.Scatter(
                x=earnings_dates, y=[earnings_marker_y] * len(earnings_dates),
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color="red", line=dict(width=2, color="red")),
                text=["E"] * len(earnings_dates), textfont=dict(color="red", size=10),
                textposition="middle center", name="Earnings", showlegend=False, meta="earnings_marker",
                hovertemplate="Earnings release<br>%{x|%b %d, %Y}<extra></extra>",
            ), row=1, col=1)

    if stop_loss is not None:
        fig.add_hline(
            y=stop_loss, row=1, col=1,
            line=dict(color=MUTED_COLOR, width=1, dash="dot"),
            annotation_text="Stop", annotation_position="top left",
            annotation_font=dict(color=MUTED_COLOR, size=10),
        )

    # Same (possibly customized) green/red used for a winning/losing
    # trade elsewhere in the app (see win_loss_color()) - dashed rather
    # than the real Stop line's dotted style, so a watchlist ticker that
    # someday becomes an open position with both a real stop AND a
    # leftover plan doesn't show two visually identical grey lines.
    if plan_entry_price is not None:
        fig.add_hline(
            y=plan_entry_price, row=1, col=1,
            line=dict(color=win_loss_color(True), width=1, dash="dash"),
            annotation_text="Plan Entry", annotation_position="top left",
            annotation_font=dict(color=win_loss_color(True), size=10),
        )
    if plan_stop_price is not None:
        fig.add_hline(
            y=plan_stop_price, row=1, col=1,
            line=dict(color=win_loss_color(False), width=1, dash="dash"),
            annotation_text="Plan Stop", annotation_position="top left",
            annotation_font=dict(color=win_loss_color(False), size=10),
        )

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

    # An overlay ticker gets its own line on a SECOND price scale (see
    # secondary_y above), not a % change conversion - two different
    # stocks' raw dollar prices are never expected to land in the same
    # range, so sharing the primary axis would squash one of them flat.
    if has_overlay:
        fit_payload["overlay"].append({
            "x": [ts.isoformat() for ts in overlay_history.index],
            "lo": overlay_history["Close"].tolist(), "hi": overlay_history["Close"].tolist(),
        })
        fig.add_trace(go.Scatter(
            x=overlay_history.index, y=overlay_history["Close"], mode="lines",
            line=dict(color=settings["overlay_color"], width=2, dash="dash"),
            name=settings["overlay_symbol"],
            hovertemplate="%{x|%b %d, %Y}: $%{y:,.2f}<extra></extra>",
        ), row=1, col=1, secondary_y=True)

    # A large, faint watermark of the symbol behind the price panel.
    fig.add_annotation(
        text=symbol, xref="x domain", yref="y domain", x=0.5, y=0.5,
        showarrow=False, font=dict(size=72, color=WATERMARK_COLOR),
    )

    # Right-edge colored badges: current price + each moving average's
    # latest value, in that line's own color - a distinctive DeepVue touch.
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

    if include_summary_header:
        # Anchored at the SAME paper y (1.02) the MA legend uses just
        # below, then lifted a fixed PIXEL amount above it via yshift -
        # deliberately not a bigger fraction like y=1.15 instead: paper
        # y/1 here spans the full two-row (price+volume) figure, so a
        # fraction that looks like a small nudge is actually a much
        # bigger pixel jump than it looks, and one attempt at y=1.15
        # pushed the text entirely off the top of the canvas (invisible,
        # not just misplaced) - a pixel-based yshift keeps this exact
        # regardless of how tall the combined subplot domain is.
        fig.add_annotation(
            x=0, xref="paper", y=1.02, yref="paper", xanchor="left", yanchor="bottom", yshift=22,
            text=_summary_header_text(history, symbol, INTERVAL_LABELS.get(interval, interval)),
            showarrow=False, align="left", font=dict(color=CHART_TEXT_COLOR, size=13),
        )

    fig.update_layout(
        height=560,
        # The overlay's own axis needs room on the left when it's
        # active; otherwise there's nothing over there since the price
        # scale itself lives on the right (see the side="right" update
        # below) - no need to reserve the space when it's not in use.
        margin=dict(t=55 if include_summary_header else 30, b=35, l=50 if has_overlay else 10, r=60),
        yaxis_title="Price ($)",
        yaxis_type="log" if settings["price_scale"] == "Log" else "linear",
        plot_bgcolor=CHART_BACKGROUND,
        paper_bgcolor=CHART_BACKGROUND,
        font=dict(color=CHART_TEXT_COLOR),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        # Without this, a click-drag (and on many browsers, a two-finger
        # trackpad swipe) defaults to drawing a rectangular zoom-select
        # box instead of panning - "pan" is what actually lets you slide
        # back and forth through time.
        dragmode="pan",
        # "x" fires a hover event for whichever bar the crosshair's
        # vertical line is over, based on x-position alone - not
        # "closest" (nearest point counting BOTH x and y distance),
        # which used to mean the OHLC summary line below only updated
        # when the cursor was also close to that bar's actual price,
        # not just lined up with it.
        hovermode="x",
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
        side="right",
    )
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    if has_overlay:
        # Overrides the generic styling just above for this one axis -
        # on the left (opposite the primary scale) and without its own
        # gridlines, so it doesn't compete with the primary axis's grid
        # for the same horizontal lines. Colored to match its line/badge
        # so it reads as "belonging" to that ticker at a glance.
        fig.update_yaxes(
            side="left", showgrid=False, zeroline=False, fixedrange=True,
            title_text=settings["overlay_symbol"],
            title_font=dict(color=settings["overlay_color"]),
            tickfont=dict(color=settings["overlay_color"]),
            row=1, col=1, secondary_y=True,
        )
    else:
        # make_subplots() above always reserves this secondary axis (so
        # the SAME figure layout works whether or not an overlay ends up
        # attached to it) - without hiding it here, an unused empty axis
        # would otherwise still render on the right, right on top of the
        # real price axis, whenever there's no overlay ticker.
        fig.update_yaxes(visible=False, row=1, col=1, secondary_y=True)

    # No floating tooltip box over the chart for candles/MAs/volume -
    # the crosshair plus the live summary line above the chart replace
    # it. "none" (rather than "skip") still fires the hover EVENTS that
    # summary line depends on; it only hides Plotly's own popup label.
    #
    # Entry/exit markers are the one exception, left alone here on
    # purpose: showing what a marker IS used to mean appending a second
    # line to that summary line instead, which changed its height every
    # time the crosshair crossed a marker - the whole chart visibly
    # jumped up and down below it. Plotly's own floating tooltip doesn't
    # have that problem (it's an overlay, not part of the page layout),
    # and it already appears anchored right next to the marker itself,
    # which happens to sit in the low band near the volume panel - so
    # this is what actually satisfies "show it near the arrow" instead
    # of "show it in a fixed bar that has to grow to fit it."
    for trace in fig.data:
        if trace.meta == "entry_exit_marker":
            trace.hoverlabel = dict(
                bgcolor=CHART_BACKGROUND, bordercolor=outcome_color, font=dict(color=CHART_TEXT_COLOR, size=11))
            continue
        trace.hovertemplate = None
        trace.hoverinfo = "none"

    return fig, fit_payload


def _chart_signature(fig):
    """
    A stable fingerprint of a figure's DATA - everything except its
    drawn shapes (line/rect annotations - see build_figure()'s
    `drawings` parameter). render_interactive_chart() below uses this
    to tell the chart component "this is still fundamentally the same
    chart" versus "this is a genuinely different chart now" (a new
    symbol, timeframe, or Chart Settings change).

    That distinction matters because saving a drawing triggers a normal
    Streamlit rerun (like any widget), which re-invokes the component
    with fresh args - without this signature, the component couldn't
    tell that apart from an actual chart change, and would rebuild the
    whole chart on every single drawing edit, resetting your current
    pan/zoom position and selected tool every time.
    """
    fig_dict = fig.to_dict()
    fig_dict["layout"] = {k: v for k, v in fig_dict["layout"].items() if k != "shapes"}
    return hashlib.md5(json.dumps(fig_dict, sort_keys=True, default=str).encode()).hexdigest()


def _json_safe(value):
    """
    Recursively replaces NaN/Infinity floats with None, so json.dumps()
    of the result is valid, spec-compliant JSON. Python's json module
    happily emits a literal `NaN`/`Infinity` token for these by default
    - not valid JSON by the actual spec, and the browser's strict
    JSON.parse() rejects it outright the moment it appears anywhere in
    the string. That used to mean one bad/incomplete data point
    anywhere in fit_payload (e.g. a stray NaN high/low from yfinance)
    silently broke the ENTIRE chart component - the render event's own
    JSON.parse(args.fit_json) call would throw before initChart() ever
    ran, leaving a blank area with no visible error anywhere in the
    Streamlit app itself (only in the browser console). The chart
    component's own JS (see chart_component/index.html's
    seriesExtent()) already treats null as "no data for this point" -
    it just needs valid JSON to get that far. Plotly's own fig.to_json()
    already does the equivalent conversion for the figure itself; this
    covers fit_payload, which is serialized with plain json.dumps().
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def render_interactive_chart(fig, fit_payload, drawings, key):
    """
    Renders the chart via the trading_journal_chart custom component
    (see chart_component/index.html and this module's own
    _chart_component declaration up top) - a real bidirectional
    Streamlit component, not a plain st.iframe embed, since drawing
    tools (and the current pan/zoom position - see below) need to send
    data back to Python, which a plain iframe has no way to do.

    `drawings` is this symbol's current saved shapes (see
    database.get_drawings()) - baked into the chart on load via
    build_figure()'s own `drawings` parameter, and editable from there
    (move/resize/erase existing ones, or add new).

    Returns {"drawings": [...], "view_range": (start, end) datetimes or
    None}. `drawings` reflects whatever's on the chart right now; it's
    the CALLER's job to compare that against what's saved and call
    database.save_drawings() if it's different - this function only
    renders and reports, it doesn't decide when to persist anything.
    `view_range` is the chart's current x-axis range (None until the
    component's first report arrives, or if the user hasn't panned/
    zoomed) - lets a caller capture "whatever's on screen right now"
    (see pages/1_Trade_Analyzer.py's Review Session "Save this
    timeframe") instead of always re-deriving a fresh default view.
    Parsed here (the raw values from the browser are date strings,
    not always the same format Python's own datetime.fromisoformat()
    can parse pre-3.11) so every caller gets plain datetimes, not one
    more string format to handle itself.

    `key` must be unique per chart on the page, same rules as any other
    Streamlit widget key.
    """
    result = _chart_component(
        fig_json=fig.to_json(),
        fit_json=json.dumps(_json_safe(fit_payload)),
        drawings=drawings,
        chart_signature=_chart_signature(fig),
        colors=_component_theme_colors(),
        key=key,
        default={"drawings": drawings, "view_range": None},
    )
    raw_range = result.get("view_range")
    view_range = (
        (pd.Timestamp(raw_range[0]).to_pydatetime(), pd.Timestamp(raw_range[1]).to_pydatetime())
        if raw_range else None
    )
    return {"drawings": result.get("drawings", []), "view_range": view_range}
