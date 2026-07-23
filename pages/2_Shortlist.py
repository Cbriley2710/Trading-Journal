"""
Shortlist
=====================
Five side-by-side lists feed the same daily routine: pick a ticker,
review its chart, and write down your thoughts for today. Clicking
Save archives a chart snapshot (via charting.build_archive_snapshot() -
a fixed 180-trading-day window, not whatever the interactive chart
happens to be showing) together with your notes into that ticker's
permanent Logbook, right away - no need to wait for anything.
nightly_archive.py still runs as a fallback for any ticker you don't
get around to saving on a given day.

  - Lists 1-4: tickers you add by hand (one at a time or a pasted
    comma-separated batch), independent of whether you actually hold a
    position - see database.get_watchlist(). Each list's name is
    editable, a ticker lives in one list at a time, and a "Remove All"
    button clears a whole list at once. A ticker stays listed (and
    keeps getting archived) until you remove it; removing it doesn't
    erase its Logbook history.

  - List 5 ("Open Positions"): auto-updates from your actual trades -
    see database.get_open_positions() - and isn't manually editable,
    since it isn't something you manage by hand. A position shows up
    here as soon as the database has more buys than sells for that
    symbol. Since this app only learns about your trades when you
    import a CSV (no live broker feed), this list is only as current
    as your most recent import.

Clicking any ticker in any list loads its chart + journal below - List
5's tickers additionally show fact tiles (entry price, current price,
unrealized P/L, stop-loss), since those need a real trade behind them.
Stop-loss itself is set/edited on the Open Positions page, not here -
see database.get_stop_loss()/set_stop_loss().
"""

from datetime import datetime, timedelta

import streamlit as st

import auth
import charting
import database
import nav
import timeutil
import ui

st.set_page_config(page_title="Shortlist", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Shortlist")

st.title("Shortlist")


def fact_tile(column, label, value, color=None):
    """This page's slightly smaller variant of the shared stat tile."""
    ui.stat_tile(column, label, value, color, size="1.3rem")


def render_price_chart(symbol, entry_point, entry_label, key_prefix, stop_loss=None):
    """
    The Timeframe/Chart-Settings controls plus the price chart itself -
    split out from render_chart_and_journal() below so the Journal
    Session queue view (which needs the chart but its own Save/Next
    buttons instead of a plain Save button) can reuse it too. Returns
    the entry_point dict, possibly with a "buy_price" added (a watchlist
    ticker starts with none - see below), or None if no price data was
    found, so the caller knows to skip the journal box entirely.
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
    display_end = datetime.combine(timeutil.today_eastern(), datetime.min.time()) + timedelta(days=1)

    fetch_padding_days = padding_days * charting.FETCH_BUFFER_MULTIPLIER
    wide_start = entry_point["entry_date"] - timedelta(days=fetch_padding_days)

    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
    fetch_start = wide_start - timedelta(days=lookback_days)

    with st.spinner(f"Fetching {timeframe_label.lower()} price history for {symbol}..."):
        history = charting.fetch_history(
            symbol, fetch_start, wide_start, display_end, interval, settings["ma_periods"], settings["ma_type"])

    if history.empty:
        st.warning(charting.history_error_message(history, symbol))
        return None

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

    conn = database.get_connection()
    saved_drawings = database.get_drawings(conn, symbol)

    fig, fit_payload = charting.build_figure(
        symbol, history, entry_point, settings, overlay_history, entry_label=entry_label, interval=interval,
        visible_range=(visible_start, display_end), stop_loss=stop_loss, drawings=saved_drawings,
        bake_arrow_traces=False)
    current_drawings = charting.render_interactive_chart(fig, fit_payload, saved_drawings, key=key_prefix)

    # Only writes to the database when the chart component reports
    # something actually different from what's saved - every pan/zoom
    # or tool-only interaction reports the same list back, so this
    # doesn't fire on those.
    if current_drawings != saved_drawings:
        database.save_drawings(conn, symbol, current_drawings)

    return entry_point


def render_journal_box(conn, symbol, key_prefix, submit_labels=("Save",)):
    """
    Today's Journal - a short text box, pre-filled with today's existing
    entry for `symbol` if there is one, sized for a quick note rather
    than a full-width essay box (a journal entry here is usually a
    sentence or two). Narrow on purpose too, leaving a column beside it
    free for the Save/Next/Skip button(s), so notes + button(s) sit in
    one compact row near the bottom of the chart instead of a full-width
    box with buttons stacked below it pushing everything past one screen.

    Wrapped in a form (rather than a plain text_area + button) for two
    things a plain widget can't do: Ctrl+Enter while typing in the box
    submits the form the same as clicking the button, instead of just
    committing the text and leaving you to still click Save separately;
    and clear_on_submit empties the box right after a successful submit
    instead of leaving what you just wrote sitting there.

    The widget's own key includes `symbol`, not just `key_prefix` - the
    single-ticker view reuses the same key_prefix ("position" or
    "watchlist") no matter which ticker is currently selected, and
    Streamlit only seeds a widget's value from `value=` the FIRST time a
    given key is created in a session. Without `symbol` in the key,
    switching from one ticker to another kept showing the PREVIOUS
    ticker's notes (or whatever you'd typed for it) instead of the new
    ticker's own entry.

    `submit_labels` is one label for a plain Save button (the
    single-ticker view), or two for a primary/secondary pair side by
    side (the guided Journal Session's "Save & Next →"/"Skip" - the
    first label is the one Ctrl+Enter triggers, since only one button
    per form can be `type="primary"`). Returns (clicked_label_or_None,
    notes) - notes is always whatever's in the box at submit time,
    regardless of which button was clicked.
    """
    today = timeutil.today_eastern()
    existing_entry = database.get_logbook_entry(conn, symbol, today)
    existing_notes = existing_entry["notes"] if existing_entry else ""

    clicked = None
    with st.form(key=f"{key_prefix}_{symbol}_journal_form", clear_on_submit=True, border=False):
        box_col, button_col = st.columns([3, 1])
        notes = box_col.text_area(
            "Today's Journal", value=existing_notes or "",
            height=68, key=f"{key_prefix}_{symbol}_notes")

        if len(submit_labels) == 1:
            # type="primary" isn't needed here for Ctrl+Enter to work -
            # a single-submit-button form always responds to it
            # regardless of button type; primary/secondary is purely
            # visual, and this keeps the plain "Save" button looking
            # exactly like it did before this was a form.
            if button_col.form_submit_button(submit_labels[0], width="stretch"):
                clicked = submit_labels[0]
        else:
            primary_label, secondary_label = submit_labels
            if button_col.form_submit_button(primary_label, type="primary", width="stretch"):
                clicked = primary_label
            if button_col.form_submit_button(secondary_label, width="stretch"):
                clicked = secondary_label

    return clicked, notes


def save_journal_entry(conn, symbol, entry_point, entry_label, notes, stop_loss=None):
    """Archives today's chart snapshot and saves the journal entry -
    the actual work behind every "Save" button on this page, whether
    it's the plain single-ticker view or a Journal Session step."""
    today = timeutil.today_eastern()
    with st.spinner("Saving and archiving today's chart..."):
        png_bytes = charting.build_archive_snapshot(
            symbol, entry_point["entry_date"], entry_point["buy_price"], entry_label,
            datetime.combine(today, datetime.min.time()), direction=entry_point.get("direction", "LONG"),
            stop_loss=stop_loss)
    database.upsert_logbook_entry(conn, symbol, today, notes=notes, chart_image=png_bytes)
    return png_bytes


def render_chart_and_journal(symbol, entry_point, entry_label, key_prefix, stop_loss=None):
    """
    The plain single-ticker view: chart, then today's-journal box, then
    a Save button. Used by both the watchlist ticker view and the open
    position detail view below.
    """
    anchor_id = f"{key_prefix}_{symbol}_journal_anchor"
    st.markdown(f'<div id="{anchor_id}"></div>', unsafe_allow_html=True)

    entry_point = render_price_chart(symbol, entry_point, entry_label, key_prefix, stop_loss=stop_loss)
    if entry_point is None:
        return

    conn = database.get_connection()
    clicked, notes = render_journal_box(conn, symbol, key_prefix)

    if clicked:
        png_bytes = save_journal_entry(conn, symbol, entry_point, entry_label, notes, stop_loss=stop_loss)
        if png_bytes is not None:
            st.success("Saved - today's chart has been archived to the Logbook.")
        else:
            st.warning(
                "Notes saved, but no price data was found to archive a chart "
                "image right now (tonight's fallback archive will try again)."
            )
        # Lands back on the chart/journal box (not the page's outer top,
        # above the nav bar and lists) - a plain rerun from a form
        # submit would otherwise leave you having to scroll back down
        # to see the very thing you just saved.
        ui.scroll_to_anchor(anchor_id)


def position_label(position):
    """A position's symbol, tagged "(Short)" when it's a short position -
    used both by List 5's ticker buttons and its detail view below."""
    return f"{position['symbol']} (Short)" if position["direction"] == "SHORT" else position["symbol"]


def render_position_stats(position, conn):
    """
    Fact tiles (entry, current price, unrealized P/L, stop-loss) for an
    open position - shared by the plain single-ticker detail view and
    the Journal Session queue view below. Returns the saved stop-loss
    price to draw on the chart, or None if there isn't one.

    Stop-loss itself is read-only here - it's set and edited on the
    Open Positions page now (a table of every position with an editable
    Stop Loss column), not per-ticker on this page, so there's one
    place to manage all of them instead of hunting through each chart.
    """
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

    stop_loss = database.get_stop_loss(conn, symbol)

    cols = st.columns(6)
    fact_tile(cols[0], "Short Entry (avg)" if is_short else "Entry (avg)", f"${position['avg_price']:,.2f}")
    fact_tile(cols[1], "Entry Date", f"{position['entry_date']:%m/%d/%Y}")
    fact_tile(cols[2], "Shares", f"{position['quantity']:,.0f}")
    fact_tile(cols[3], "Current Price", f"${current_price:,.2f}" if current_price is not None else "N/A")
    fact_tile(cols[4], "Unrealized P/L",
              f"${unrealized_pl:,.2f}" if unrealized_pl is not None else "N/A", unrealized_color)
    fact_tile(cols[5], "Stop Loss", f"${stop_loss:,.2f}" if stop_loss is not None else "Not set")
    st.caption("Set or move the stop-loss for this position on the Open Positions page.")

    return stop_loss


def render_position_detail(position, conn):
    """
    The rich view for an open position: fact tiles (entry, current
    price, unrealized P/L, stop-loss), and the chart + journal -
    everything the old dropdown-based Open Positions section used to
    show, now driven directly by a position dict from List 5 instead of
    a dropdown selection.
    """
    symbol = position["symbol"]
    is_short = position["direction"] == "SHORT"

    stop_loss = render_position_stats(position, conn)

    st.divider()

    entry_point = {
        "entry_date": position["entry_date"], "buy_price": position["avg_price"],
        "direction": position["direction"],
    }
    render_chart_and_journal(
        symbol, entry_point, "Short Entry" if is_short else "Entry", key_prefix="position", stop_loss=stop_loss)


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


def render_lists_section():
    st.header("Watchlists")
    st.caption(
        "Lists 1-4 are yours to manage: add tickers one at a time or paste a "
        "comma-separated batch, rename a list any time, or clear one with "
        "Remove All. List 5 auto-updates from your open positions and isn't "
        "editable by hand. Click any ticker in any list to load its chart "
        "and journal below."
    )

    conn = database.get_connection()
    names = database.get_watchlist_names(conn)
    watchlist = database.get_watchlist(conn)
    positions = database.get_open_positions(conn)
    positions_by_symbol = {p["symbol"]: p for p in positions}

    # A message from the previous button click (add/remove), stashed in
    # session state so it survives the st.rerun() that refreshes the
    # lists - showing it directly before rerunning would lose it.
    if "watchlist_message" in st.session_state:
        st.info(st.session_state.pop("watchlist_message"))

    columns = st.columns(5)

    for list_id, column in zip(range(1, 5), columns[:4]):
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

            # Wrapped in a form (clear_on_submit=True) so the box empties
            # out right after Add instead of leaving the just-added
            # ticker(s) sitting there. "Remove All" lives in the same
            # form purely to keep its original side-by-side layout with
            # Add - it doesn't depend on the text box at all.
            with st.form(key=f"wl_add_form_{list_id}", clear_on_submit=True, border=False):
                add_text = st.text_input(
                    "Add ticker(s)", key=f"wl_add_{list_id}",
                    placeholder="NVDA or NVDA, AMD", label_visibility="collapsed",
                )
                button_cols = st.columns(2)
                add_clicked = button_cols[0].form_submit_button("Add")
                list_symbols = [w["symbol"] for w in watchlist if w["list_id"] == list_id]
                remove_clicked = button_cols[1].form_submit_button("Remove All")

            if add_clicked and add_text.strip():
                already_elsewhere = []
                added_count = 0
                newly_added = []
                for sym in parse_ticker_input(add_text):
                    if database.add_to_watchlist(conn, sym, list_id):
                        added_count += 1
                        newly_added.append(sym)
                    else:
                        already_elsewhere.append(sym)
                if newly_added:
                    # Warms the persistent price cache (see charting.
                    # warm_price_cache_for_symbol()) for a ticker right
                    # when it's added, instead of waiting for tonight's
                    # after-close warming job - a brief delay here
                    # (proportional to how many tickers were just
                    # pasted in) instead of a slow first chart load
                    # later.
                    with st.spinner(f"Warming price cache for {', '.join(newly_added)}..."):
                        for sym in newly_added:
                            charting.warm_price_cache_for_symbol(sym)
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

            if remove_clicked and list_symbols:
                for sym in list_symbols:
                    database.remove_from_watchlist(conn, sym)
                selected = st.session_state.get("watchlist_selected")
                if selected and selected.get("source") == "watchlist" and selected.get("symbol") in list_symbols:
                    del st.session_state["watchlist_selected"]
                st.session_state["watchlist_message"] = (
                    f"Cleared {new_name} ({len(list_symbols)} ticker(s)). Their Logbook history is kept."
                )
                st.rerun()

            # A fixed-height, scrollable window for the ticker list
            # itself - so a long list scrolls in place instead of
            # pushing the chart/journal section further down the page
            # (only this part scrolls; the name and add-ticker inputs
            # above stay put).
            with st.container(height=250):
                for entry in [w for w in watchlist if w["list_id"] == list_id]:
                    ticker_cols = st.columns([4, 1])
                    if ticker_cols[0].button(
                        entry["symbol"], key=f"wl_{list_id}_{entry['symbol']}", width="stretch",
                    ):
                        st.session_state["watchlist_selected"] = {"symbol": entry["symbol"], "source": "watchlist"}
                    if ticker_cols[1].button("✕", key=f"wlx_{list_id}_{entry['symbol']}"):
                        database.remove_from_watchlist(conn, entry["symbol"])
                        selected = st.session_state.get("watchlist_selected")
                        if selected and selected.get("source") == "watchlist" and selected.get("symbol") == entry["symbol"]:
                            del st.session_state["watchlist_selected"]
                        st.session_state["watchlist_message"] = (
                            f"Removed {entry['symbol']}. Its Logbook history is kept."
                        )
                        st.rerun()

    # List 5: read-only, auto-populated from your actual open trades -
    # no name edit, no add/remove, since there's nothing to manage by
    # hand here (a position only disappears once you actually close it).
    with columns[4]:
        st.markdown("**Open Positions**")
        st.caption("Auto-updates from your trades.")
        with st.container(height=250):
            if not positions:
                st.caption("No open positions right now.")
            for position in positions:
                if st.button(position_label(position), key=f"wl_pos_{position['symbol']}", width="stretch"):
                    st.session_state["watchlist_selected"] = {"symbol": position["symbol"], "source": "position"}

    st.divider()

    selected = st.session_state.get("watchlist_selected")
    if not selected:
        st.caption("Click a ticker above to load its chart and journal.")
        return

    if selected.get("source") == "position" and selected.get("symbol") in positions_by_symbol:
        render_position_detail(positions_by_symbol[selected["symbol"]], conn)
        return

    watchlist_by_symbol = {w["symbol"]: w for w in watchlist}
    if selected.get("source") == "watchlist" and selected.get("symbol") in watchlist_by_symbol:
        entry = watchlist_by_symbol[selected["symbol"]]
        entry_point = {"entry_date": entry["added_at"]}
        render_chart_and_journal(entry["symbol"], entry_point, "Added", key_prefix="watchlist")
        return

    # The selection no longer resolves to anything real (position
    # closed, ticker removed) - fall back cleanly instead of crashing.
    st.caption("That ticker is no longer listed. Click one above to load its chart and journal.")


def build_journal_queue(conn):
    """
    Builds today's Journal Session queue: every open position first
    (richer detail - fact tiles and a stop-loss line), then every
    watchlist ticker not already covered by a position, in each list's
    existing order. A symbol that's both an open position and sitting
    on a watchlist is only journaled once, as the position.
    """
    queue = []
    seen_symbols = set()

    for position in database.get_open_positions(conn):
        is_short = position["direction"] == "SHORT"
        queue.append({
            "symbol": position["symbol"],
            "source": "position",
            "position": position,
            "entry_point": {
                "entry_date": position["entry_date"], "buy_price": position["avg_price"],
                "direction": position["direction"],
            },
            "entry_label": "Short Entry" if is_short else "Entry",
        })
        seen_symbols.add(position["symbol"])

    for entry in database.get_watchlist(conn):
        if entry["symbol"] in seen_symbols:
            continue
        queue.append({
            "symbol": entry["symbol"],
            "source": "watchlist",
            "position": None,
            "entry_point": {"entry_date": entry["added_at"]},
            "entry_label": "Added",
        })
        seen_symbols.add(entry["symbol"])

    return queue


def _queue_symbol_pairs(queue):
    """The plain {"symbol", "source"} list that gets persisted for
    resuming - see journal_session_progress's own docstring in
    database.py for why the full queue items aren't stored as-is."""
    return [{"symbol": item["symbol"], "source": item["source"]} for item in queue]


def _reorder_for_resume(queue, saved_order):
    """
    Filters and reorders a freshly-built `queue` (today's real, current
    positions/watchlist) to match `saved_order` (the {"symbol","source"}
    list saved when a paused session was started) - so resuming picks up
    in the SAME order you were going through, not whatever order
    build_journal_queue() would produce fresh today. A saved symbol/
    source pair no longer present in today's queue (the position closed,
    or the watchlist ticker was removed, while the session sat paused)
    is silently dropped - there's nothing left to journal for it.
    """
    by_key = {(item["symbol"], item["source"]): item for item in queue}
    return [by_key[(s["symbol"], s["source"])] for s in saved_order if (s["symbol"], s["source"]) in by_key]


def _advance_session(conn, session, queue):
    """
    Moves the Journal Session to the next ticker: bumps the index,
    persists the new progress (see database.save_journal_session_progress
    - so this survives closing the tab or the app going idle), flags the
    next render to scroll back down to the session's own anchor instead
    of the page's outer top, and reruns. Shared by Save & Next, Skip,
    and the "no price data" auto-skip below.
    """
    session["index"] += 1
    database.save_journal_session_progress(conn, _queue_symbol_pairs(queue), session["index"])
    st.session_state["_scroll_to_session_anchor"] = True
    st.rerun()


def render_journal_session(conn):
    """
    The guided Journal Session: walks through every ticker in the queue
    one at a time, full-screen, so journaling all of them in one sitting
    is click-write-Save & Next instead of scrolling back up to the
    watchlists to pick the next ticker every time.
    """
    session = st.session_state["journal_session"]
    queue, index = session["queue"], session["index"]

    if index >= len(queue):
        database.clear_journal_session_progress(conn)
        st.success(f"Session complete - journaled {len(queue)} ticker(s) today.")
        if st.button("Back to Shortlist"):
            del st.session_state["journal_session"]
            st.rerun()
        return

    item = queue[index]
    symbol = item["symbol"]
    key_prefix = f"session_{index}"

    anchor_id = f"{key_prefix}_journal_anchor"
    st.markdown(f'<div id="{anchor_id}"></div>', unsafe_allow_html=True)
    if st.session_state.pop("_scroll_to_session_anchor", False):
        ui.scroll_to_anchor(anchor_id)

    header_cols = st.columns([5, 1])
    header_cols[0].subheader(f"Reviewing {index + 1} of {len(queue)}: {symbol}")
    if header_cols[1].button("Exit Session", key=f"{key_prefix}_exit"):
        # Deliberately does NOT clear the persisted progress - exiting
        # mid-session is exactly the "didn't finish" case the saved
        # progress is for, so it's still there to resume next time.
        del st.session_state["journal_session"]
        st.rerun()
    st.progress(index / len(queue))

    stop_loss = None
    if item["source"] == "position":
        stop_loss = render_position_stats(item["position"], conn)
        st.divider()

    entry_point = render_price_chart(
        symbol, item["entry_point"], item["entry_label"], key_prefix, stop_loss=stop_loss)

    if entry_point is None:
        # No price data for this one right now - nothing to journal
        # against, so the only sensible move is on to the next ticker.
        if st.button("Skip →", key=f"{key_prefix}_skip"):
            _advance_session(conn, session, queue)
        return

    clicked, notes = render_journal_box(conn, symbol, key_prefix, submit_labels=("Save & Next →", "Skip"))

    if clicked == "Save & Next →":
        save_journal_entry(conn, symbol, entry_point, item["entry_label"], notes, stop_loss=stop_loss)
        _advance_session(conn, session, queue)
    elif clicked == "Skip":
        _advance_session(conn, session, queue)


conn = database.get_connection()

if st.session_state.get("journal_session") is not None:
    render_journal_session(conn)
else:
    # An unfinished session from earlier (or before the tab was closed,
    # or the app went idle) - see database.get_journal_session_progress().
    # Rebuilt against TODAY's real positions/watchlist, then filtered
    # down to the saved order (_reorder_for_resume) - a symbol that's
    # since closed or been removed is silently dropped.
    saved_progress = database.get_journal_session_progress(conn)
    if saved_progress:
        resumed_queue = _reorder_for_resume(build_journal_queue(conn), saved_progress["queue"])
        resume_index = min(saved_progress["current_index"], len(resumed_queue))
        if resumed_queue and resume_index < len(resumed_queue):
            st.info(f"You have an unfinished Journal Session ({resume_index + 1} of {len(resumed_queue)}).")
            resume_cols = st.columns([1, 1, 3])
            if resume_cols[0].button("▶ Resume Session", type="primary"):
                st.session_state["journal_session"] = {"queue": resumed_queue, "index": resume_index}
                st.session_state["_scroll_to_session_anchor"] = True
                st.rerun()
            if resume_cols[1].button("Discard"):
                database.clear_journal_session_progress(conn)
                st.rerun()
            st.divider()
        else:
            # Nothing left worth resuming (everything in it is gone) -
            # clean up quietly instead of leaving a dead row behind.
            database.clear_journal_session_progress(conn)

    if st.button("📝 Start Journal Session", type="primary"):
        queue = build_journal_queue(conn)
        if not queue:
            st.info("Nothing to journal yet - add a ticker to a watchlist or open a position first.")
        else:
            st.session_state["journal_session"] = {"queue": queue, "index": 0}
            database.save_journal_session_progress(conn, _queue_symbol_pairs(queue), 0)
            st.session_state["_scroll_to_session_anchor"] = True
            st.rerun()
    st.divider()
    render_lists_section()
