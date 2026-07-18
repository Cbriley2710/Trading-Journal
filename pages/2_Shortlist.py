"""
Shortlist
=====================
Two independent lists live on this page, both feeding the same daily
routine: pick a ticker, review its chart, and write down your thoughts
for today. Clicking Save archives a chart snapshot (via
charting.build_archive_snapshot() - a fixed 180-trading-day window, not
whatever the interactive chart happens to be showing) together with
your notes into that ticker's permanent Logbook, right away - no need
to wait for anything. nightly_archive.py still runs as a fallback for
any ticker you don't get around to saving on a given day.

  - Open Positions: derived automatically from your actual trades -
    see database.get_open_positions(). A position shows up here as
    soon as the database has more buys than sells for that symbol.
    Since this app only learns about your trades when you import a
    CSV (no live broker feed), this list is only as current as your
    most recent import.

  - Watchlist: tickers you add by hand below, independent of whether
    you actually hold a position - see database.get_watchlist(). A
    ticker stays here (and keeps getting archived) until you remove
    it; removing it doesn't erase its Logbook history.
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


def render_chart_and_journal(symbol, entry_point, entry_label, key_prefix):
    """
    Shared by both sections below: the Timeframe/Chart-Settings controls,
    the price chart itself, and the today's-journal box + Save button.
    `key_prefix` keeps each section's Streamlit widgets independent (so
    picking a timeframe for a watchlist ticker doesn't affect the open
    position's chart, etc).
    """
    timeframe_label = st.radio(
        "Timeframe", options=list(charting.TIMEFRAMES.keys()), index=1,
        horizontal=True, key=f"{key_prefix}_timeframe")
    interval, default_padding, min_padding, max_padding = charting.TIMEFRAMES[timeframe_label]

    control_cols = st.columns([3, 1])
    padding_days = control_cols[0].slider(
        "Days of context before the entry",
        min_value=min_padding, max_value=max_padding, value=default_padding,
        key=f"{key_prefix}_padding")
    settings = charting.render_settings_toolbar(control_cols[1])

    display_start = entry_point["entry_date"] - timedelta(days=padding_days)
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
        return

    # A watchlist ticker has no real trade price - use the closing price
    # near when it was added instead, just for marker placement.
    if "buy_price" not in entry_point:
        entry_point = dict(entry_point, buy_price=charting.price_near_date(history, entry_point["entry_date"]))

    overlay_history = None
    if settings["overlay_symbol"]:
        with st.spinner(f"Fetching overlay data for {settings['overlay_symbol']}..."):
            overlay_history = charting.fetch_history(
                settings["overlay_symbol"], fetch_start, display_start, display_end, interval, [])
        if overlay_history.empty:
            st.warning(f"No price data found for overlay ticker {settings['overlay_symbol']}. Showing chart without it.")
            overlay_history = None

    st.caption(charting.build_ohlc_summary(history, symbol, timeframe_label))

    fig = charting.build_figure(
        symbol, history, entry_point, settings, overlay_history, entry_label=entry_label, interval=interval)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Today's Journal")
    conn = database.get_connection()
    today = date.today()
    existing_entry = database.get_logbook_entry(conn, symbol, today)
    existing_notes = existing_entry["notes"] if existing_entry else ""

    notes = st.text_area(
        "Your thoughts on this trade today", value=existing_notes or "",
        height=150, key=f"{key_prefix}_notes")

    if st.button("Save", key=f"{key_prefix}_save"):
        with st.spinner("Saving and archiving today's chart..."):
            png_bytes = charting.build_archive_snapshot(
                symbol, entry_point["entry_date"], entry_point["buy_price"], entry_label,
                datetime.combine(today, datetime.min.time()))
        database.upsert_logbook_entry(conn, symbol, today, notes=notes, chart_image=png_bytes)
        if png_bytes is not None:
            st.success("Saved - today's chart has been archived to the Logbook.")
        else:
            st.warning(
                "Notes saved, but no price data was found to archive a chart "
                "image right now (tonight's fallback archive will try again)."
            )


def render_open_positions_section():
    st.header("Open Positions")

    conn = database.get_connection()
    positions = database.get_open_positions(conn)

    if not positions:
        st.info(
            "No open positions right now. A ticker shows up here as soon as "
            "an imported buy hasn't been matched to a sell yet."
        )
        return

    def position_label(position):
        return (
            f"{position['symbol']}: {position['quantity']:,.0f} shares @ "
            f"avg ${position['avg_price']:,.2f} (opened {position['entry_date']:%m/%d/%Y})"
        )

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

    entry_point = {"entry_date": position["entry_date"], "buy_price": position["avg_price"]}
    render_chart_and_journal(symbol, entry_point, "Entry", key_prefix="position")


def render_watchlist_section():
    st.header("Watchlist")
    st.caption(
        "Tickers you add here get the same daily chart + journal treatment "
        "as an open position, even without an actual trade behind them - "
        "they stay active until you remove them below."
    )

    conn = database.get_connection()

    add_cols = st.columns([3, 1])
    new_symbol = add_cols[0].text_input("Add a ticker", key="watchlist_add_input", placeholder="e.g. NVDA")
    if add_cols[1].button("Add", key="watchlist_add_button") and new_symbol.strip():
        database.add_to_watchlist(conn, new_symbol.strip().upper())
        st.rerun()

    watchlist = database.get_watchlist(conn)

    if not watchlist:
        st.info("Your watchlist is empty - add a ticker above to start tracking it.")
        return

    def watchlist_label(entry):
        return f"{entry['symbol']} (added {entry['added_at']:%m/%d/%Y})"

    selected_index = st.selectbox(
        "Choose a watchlist ticker", options=range(len(watchlist)),
        format_func=lambda i: watchlist_label(watchlist[i]),
    )
    entry = watchlist[selected_index]
    symbol = entry["symbol"]

    if st.button(f"Remove {symbol} from Watchlist"):
        database.remove_from_watchlist(conn, symbol)
        st.success(f"Removed {symbol}. Its Logbook history is kept.")
        st.rerun()

    st.divider()

    entry_point = {"entry_date": entry["added_at"]}
    render_chart_and_journal(symbol, entry_point, "Added", key_prefix="watchlist")


render_open_positions_section()
st.divider()
render_watchlist_section()
