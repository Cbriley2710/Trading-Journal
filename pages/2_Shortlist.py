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

  - Watchlists: FIVE side-by-side lists of tickers you add by hand
    (one at a time or a pasted comma-separated batch), independent of
    whether you actually hold a position - see database.get_watchlist().
    Each list's name is editable, a ticker lives in one list at a time,
    and clicking any ticker loads its chart + journal below. A ticker
    stays listed (and keeps getting archived) until you remove it;
    removing it doesn't erase its Logbook history.
"""

from datetime import date, datetime, timedelta

import streamlit as st

import auth
import charting
import database
import nav
import ui

st.set_page_config(page_title="Shortlist", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Shortlist")

st.title("Shortlist")


def fact_tile(column, label, value, color=None):
    """This page's slightly smaller variant of the shared stat tile."""
    ui.stat_tile(column, label, value, color, size="1.3rem")


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
    interval, padding_days = charting.TIMEFRAMES[timeframe_label]

    control_cols = st.columns([4, 1])
    settings = charting.render_settings_toolbar(control_cols[1], key_prefix)
    control_cols[0].caption("Scroll on the chart to zoom in/out through time; drag or swipe to pan.")

    # The chart opens showing just this default window (visible_start
    # through today), but fetches further back than that (wide_start -
    # see FETCH_BUFFER_MULTIPLIER) so scrolling/zooming out reveals real
    # history instead of hitting an empty edge immediately. There's no
    # future data to extend into on the right, so that side is unchanged.
    visible_start = entry_point["entry_date"] - timedelta(days=padding_days)
    display_end = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=1)

    fetch_padding_days = padding_days * charting.FETCH_BUFFER_MULTIPLIER
    wide_start = entry_point["entry_date"] - timedelta(days=fetch_padding_days)

    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
    fetch_start = wide_start - timedelta(days=lookback_days)

    with st.spinner(f"Fetching {timeframe_label.lower()} price history for {symbol}..."):
        history = charting.fetch_history(
            symbol, fetch_start, wide_start, display_end, interval, settings["ma_periods"])

    if history.empty:
        st.warning(charting.history_error_message(history, symbol))
        return

    # A watchlist ticker has no real trade price - use the closing price
    # near when it was added instead, just for marker placement.
    if "buy_price" not in entry_point:
        entry_point = dict(entry_point, buy_price=charting.price_near_date(history, entry_point["entry_date"]))

    overlay_history = None
    if settings["overlay_symbol"]:
        with st.spinner(f"Fetching overlay data for {settings['overlay_symbol']}..."):
            overlay_history = charting.fetch_history(
                settings["overlay_symbol"], fetch_start, wide_start, display_end, interval, [])
        if overlay_history.empty:
            st.warning(
                charting.history_error_message(overlay_history, settings["overlay_symbol"])
                + " Showing chart without it."
            )
            overlay_history = None

    # (The OHLC summary line now lives inside the chart component itself,
    # where it updates live as the crosshair moves - see charting.py.)

    fig, fit_payload = charting.build_figure(
        symbol, history, entry_point, settings, overlay_history, entry_label=entry_label, interval=interval,
        visible_range=(visible_start, display_end))
    charting.render_interactive_chart(fig, fit_payload)

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
                datetime.combine(today, datetime.min.time()), direction=entry_point.get("direction", "LONG"))
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
        short_tag = " (Short)" if position["direction"] == "SHORT" else ""
        return (
            f"{position['symbol']}{short_tag}: {position['quantity']:,.0f} shares @ "
            f"avg ${position['avg_price']:,.2f} (opened {position['entry_date']:%m/%d/%Y})"
        )

    selected_index = st.selectbox(
        "Choose an open position", options=range(len(positions)),
        format_func=lambda i: position_label(positions[i]),
    )
    position = positions[selected_index]
    symbol = position["symbol"]
    is_short = position["direction"] == "SHORT"

    with st.spinner(f"Fetching current price for {symbol}..."):
        current_price = charting.fetch_latest_price(symbol)

    unrealized_pl = None
    unrealized_color = None
    if current_price is not None:
        # A short profits when price FALLS below your average entry -
        # the opposite direction from a long position.
        if is_short:
            unrealized_pl = (position["avg_price"] - current_price) * position["quantity"]
        else:
            unrealized_pl = (current_price - position["avg_price"]) * position["quantity"]
        unrealized_color = charting.GOOD_COLOR if unrealized_pl >= 0 else charting.CRITICAL_COLOR

    cols = st.columns(5)
    fact_tile(cols[0], "Short Entry (avg)" if is_short else "Entry (avg)", f"${position['avg_price']:,.2f}")
    fact_tile(cols[1], "Entry Date", f"{position['entry_date']:%m/%d/%Y}")
    fact_tile(cols[2], "Shares", f"{position['quantity']:,.0f}")
    fact_tile(cols[3], "Current Price", f"${current_price:,.2f}" if current_price is not None else "N/A")
    fact_tile(cols[4], "Unrealized P/L",
              f"${unrealized_pl:,.2f}" if unrealized_pl is not None else "N/A", unrealized_color)

    # A stop-loss price isn't tracked anywhere else in the app - saving it
    # here is what lets the Open Positions page compute "heat" (dollar
    # risk from the current price down to this stop). You can come back
    # and move it any time (e.g. trailing it up as the trade works).
    saved_stop = database.get_stop_loss(conn, symbol)
    new_stop = st.number_input(
        "Stop Loss (0 = no stop)", min_value=0.0, value=saved_stop or 0.0, step=0.01, format="%.2f",
        key="stop_loss_input",
    )
    if st.button("Save Stop Loss", key="stop_loss_save"):
        # 0 means "no stop," not a real $0 stop price - storing an
        # actual $0 would make the Open Positions page count nearly the
        # whole position's value as heat.
        if new_stop > 0:
            database.set_stop_loss(conn, symbol, new_stop)
            st.success(f"Stop loss for {symbol} saved at ${new_stop:,.2f}.")
        else:
            database.delete_stop_loss(conn, symbol)
            st.success(f"Stop loss for {symbol} cleared.")

    st.divider()

    entry_point = {
        "entry_date": position["entry_date"], "buy_price": position["avg_price"],
        "direction": position["direction"],
    }
    render_chart_and_journal(symbol, entry_point, "Short Entry" if is_short else "Entry", key_prefix="position")


def parse_ticker_input(text):
    """
    Turns "NVDA" or a pasted batch like "NVDA, AMD MSFT" (commas,
    spaces, or new lines between tickers - however it was copied) into
    a clean, de-duplicated list of uppercase symbols, keeping the order
    they were typed.
    """
    symbols = []
    for part in text.replace(",", " ").split():
        sym = part.strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols


def render_watchlist_section():
    st.header("Watchlists")
    st.caption(
        "Five lists, each with an editable name. Add tickers one at a time "
        "or paste a comma-separated batch; click any ticker to load its "
        "chart and journal below. A ticker lives in one list at a time, and "
        "every listed ticker gets the same nightly chart archive as an open "
        "position."
    )

    conn = database.get_connection()
    names = database.get_watchlist_names(conn)
    watchlist = database.get_watchlist(conn)

    # A message from the previous button click (add/remove), stashed in
    # session state so it survives the st.rerun() that refreshes the
    # lists - showing it directly before rerunning would lose it.
    if "watchlist_message" in st.session_state:
        st.info(st.session_state.pop("watchlist_message"))

    columns = st.columns(5)
    for list_id, column in zip(range(1, 6), columns):
        with column:
            # The list's name doubles as its editable title - typing a
            # new name saves right away, same silent-save pattern as
            # the Chart Settings moving averages.
            new_name = st.text_input(
                f"List {list_id} name", value=names[list_id],
                key=f"wl_name_{list_id}", label_visibility="collapsed",
            )
            if new_name.strip() and new_name != names[list_id]:
                database.set_watchlist_name(conn, list_id, new_name.strip())

            add_text = st.text_input(
                "Add ticker(s)", key=f"wl_add_{list_id}",
                placeholder="NVDA or NVDA, AMD", label_visibility="collapsed",
            )
            if st.button("Add", key=f"wl_add_btn_{list_id}") and add_text.strip():
                already_elsewhere = []
                added_count = 0
                for sym in parse_ticker_input(add_text):
                    if database.add_to_watchlist(conn, sym, list_id):
                        added_count += 1
                    else:
                        already_elsewhere.append(sym)
                parts = []
                if added_count:
                    parts.append(f"Added {added_count} ticker(s) to {new_name}.")
                if already_elsewhere:
                    locations = {w["symbol"]: w["list_id"] for w in database.get_watchlist(conn)}
                    where = ", ".join(
                        f"{sym} (already in {names.get(locations.get(sym), '?')})"
                        for sym in already_elsewhere
                    )
                    parts.append(f"Not added: {where}.")
                if parts:
                    st.session_state["watchlist_message"] = " ".join(parts)
                st.rerun()

            for entry in [w for w in watchlist if w["list_id"] == list_id]:
                ticker_cols = st.columns([4, 1])
                if ticker_cols[0].button(
                    entry["symbol"], key=f"wl_{list_id}_{entry['symbol']}", width="stretch",
                ):
                    st.session_state["watchlist_selected"] = entry["symbol"]
                if ticker_cols[1].button("✕", key=f"wlx_{list_id}_{entry['symbol']}"):
                    database.remove_from_watchlist(conn, entry["symbol"])
                    if st.session_state.get("watchlist_selected") == entry["symbol"]:
                        del st.session_state["watchlist_selected"]
                    st.session_state["watchlist_message"] = (
                        f"Removed {entry['symbol']}. Its Logbook history is kept."
                    )
                    st.rerun()

    st.divider()

    selected = st.session_state.get("watchlist_selected")
    entries_by_symbol = {w["symbol"]: w for w in watchlist}
    if selected not in entries_by_symbol:
        st.caption("Click a ticker above to load its chart and journal.")
        return

    entry_point = {"entry_date": entries_by_symbol[selected]["added_at"]}
    render_chart_and_journal(selected, entry_point, "Added", key_prefix="watchlist")


render_open_positions_section()
st.divider()
render_watchlist_section()
