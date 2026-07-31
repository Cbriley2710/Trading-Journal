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

The actual chart-building (candlesticks, moving averages, colors,
ticker overlay) lives in charting.py, shared with the Shortlist page
and the nightly archive script - this file just wires up trade
selection and the fact tiles around it.

Also here: the guided Review Session (see render_review_session()
below) - the same "step through a queue, write a note, Save & Next"
idea as Shortlist's Journal Session, but for CLOSED trades picked from
a date range instead of today's open positions/watchlist. A review can
also capture chart snapshots at more than one timeframe per trade (see
"Save this timeframe" below) - useful when something notable shows up
on, say, the hourly chart but not the daily one. Saved reviews are
browsed on the Logbook page's "Trade Reviews" section, the same way
Journal Session's notes end up browsable on Logbook itself.

This is a second "page" of the dashboard app - Streamlit automatically
turns any file placed in a pages/ folder next to dashboard.py into its
own page, listed in the sidebar. Nothing needs to be registered by
hand.
"""

from datetime import timedelta

import streamlit as st

import auth
import charting
import database
import nav
import ui
from analyze_trades import trade_label

st.set_page_config(page_title="Trade Analyzer", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("trade_analyzer")

st.title("Trade Analyzer")


def load_trades():
    conn = database.get_connection()
    return database.get_trades(conn)


def fact_tile(column, label, value, color=None):
    """This page's stat tile - same default size as every other page's
    now (see ui.stat_tile()'s own docstring; this used to be a smaller
    1.3rem here and on Shortlist only, a leftover inconsistency from
    before this shared helper existed)."""
    ui.stat_tile(column, label, value, color)


def render_trade_facts(trade):
    """The five fact tiles (entry/exit, shares, P/L, % change) for one
    closed trade - shared by the plain single-trade view and each step
    of the Review Session below."""
    is_short = trade["direction"] == "SHORT"
    # For a short, "sell_price" is the short sale (the entry event) and
    # "buy_price" is the cover (the exit event) - the opposite pairing
    # from a long trade. See match_trades_lifo() in analyze_trades.py.
    entry_price, exit_price = (trade["sell_price"], trade["buy_price"]) if is_short \
        else (trade["buy_price"], trade["sell_price"])

    outcome_color = charting.win_loss_color(trade["profit_loss"] >= 0)
    # Expressed as % of entry price, using the actual (correctly-signed)
    # profit_loss rather than a raw price ratio - a profitable short
    # would otherwise show a misleading negative % (cover price fell
    # below the entry price, which is the whole point of a profitable
    # short).
    pct_change = (trade["profit_loss"] / (entry_price * trade["quantity"])) * 100 if entry_price else 0.0

    cols = st.columns(5)
    fact_tile(cols[0], "Short Entry" if is_short else "Entry", f"${entry_price:,.2f}")
    fact_tile(cols[1], "Cover" if is_short else "Exit", f"${exit_price:,.2f}")
    fact_tile(cols[2], "Shares", f"{trade['quantity']:,.0f}")
    fact_tile(cols[3], "P/L", f"${trade['profit_loss']:,.2f}", outcome_color)
    fact_tile(cols[4], "% Change", f"{pct_change:,.2f}%", outcome_color)


def render_trade_chart(conn, trade, key_prefix, anchor_id=None):
    """
    The Timeframe/Chart-Settings controls plus the interactive price
    chart for one closed trade - extracted from what used to be this
    page's own inline, one-shot code so the Review Session below can
    render several different trades (and re-render the SAME trade at a
    freshly picked timeframe) within one script run, the same reason
    Shortlist's render_price_chart() exists as a real function instead
    of inline page code.

    Returns (timeframe_label, settings, chart_rendered) - the caller
    needs `timeframe_label`/`settings` to capture a matching snapshot
    via charting.build_trade_review_snapshot() when "Save this
    timeframe" is clicked, and `chart_rendered` (False when there's no
    price data) to know whether to skip straight past the review
    controls instead of showing them against a chart that isn't there.
    """
    timeframe_label = st.radio(
        "Timeframe", options=list(charting.TIMEFRAMES.keys()), index=1,
        horizontal=True, key=f"{key_prefix}_timeframe")
    interval, padding_days = charting.TIMEFRAMES[timeframe_label]

    control_cols = st.columns([4, 1])
    settings = charting.render_settings_toolbar(control_cols[1], key_prefix)
    control_cols[0].caption("Scroll on the chart to zoom in/out through time; drag or swipe to pan.")

    # The chart opens showing just this default window (visible_start
    # to visible_end), but fetches a much wider one (wide_start to
    # wide_end - see FETCH_BUFFER_MULTIPLIER) so scrolling/zooming out
    # on the chart reveals real history instead of hitting an empty
    # edge immediately.
    visible_start = trade["entry_date"] - timedelta(days=padding_days)
    visible_end = trade["date"] + timedelta(days=padding_days)

    fetch_padding_days = padding_days * charting.FETCH_BUFFER_MULTIPLIER
    wide_start = trade["entry_date"] - timedelta(days=fetch_padding_days)
    wide_end = trade["date"] + timedelta(days=fetch_padding_days)

    # Fetch extra history before wide_start so the longest selected
    # moving average already has a real window of data at the left edge
    # of the fetched range, instead of only "warming up" partway
    # through it.
    max_ma_period = max(settings["ma_periods"], default=0)
    lookback_days = max_ma_period * charting.LOOKBACK_DAYS_PER_PERIOD[interval]
    fetch_start = wide_start - timedelta(days=lookback_days)

    with st.spinner(f"Fetching {timeframe_label.lower()} price history for {trade['symbol']}..."):
        history = charting.fetch_history(
            trade["symbol"], fetch_start, wide_start, wide_end, interval, settings["ma_periods"], settings["ma_type"])

    if history.empty:
        st.warning(charting.history_error_message(history, trade["symbol"]))
        return timeframe_label, settings, False

    overlay_history = None
    if settings["overlay_symbol"]:
        with st.spinner(f"Fetching overlay data for {settings['overlay_symbol']}..."):
            overlay_history = charting.fetch_history(
                settings["overlay_symbol"], fetch_start, wide_start, wide_end, interval, [])
        if overlay_history.empty:
            st.warning(
                charting.history_error_message(overlay_history, settings["overlay_symbol"])
                + " Showing chart without it."
            )
            overlay_history = None

    entry_point = {
        "entry_date": trade["entry_date"], "buy_price": trade["buy_price"],
        "exit_date": trade["date"], "sell_price": trade["sell_price"],
        "direction": trade["direction"],
    }

    saved_drawings = database.get_drawings(conn, trade["symbol"])

    if anchor_id:
        st.markdown(f'<div id="{anchor_id}"></div>', unsafe_allow_html=True)

    fig, fit_payload = charting.build_figure(
        trade["symbol"], history, entry_point, settings, overlay_history, interval=interval,
        visible_range=(visible_start, visible_end), drawings=saved_drawings, bake_arrow_traces=False)
    current_drawings = charting.render_interactive_chart(fig, fit_payload, saved_drawings, key=key_prefix)

    # Only writes to the database when something's actually different
    # from what's saved - see pages/2_Shortlist.py's own
    # render_price_chart() for the same pattern.
    if current_drawings != saved_drawings:
        database.save_drawings(conn, trade["symbol"], current_drawings)

    return timeframe_label, settings, True


# --- Review Session ---------------------------------------------------

def _trade_key(trade):
    """The natural-key tuple that identifies one closed trade -
    symbol/entry_date/exit_date/direction, all as plain strings/values
    so it's usable as both a dict key and a JSON-safe persisted value.
    NOT trades.id - see trade_reviews' own schema comment in
    database.py for why a trade's id isn't a stable identifier across
    an import (database.rebuild_trades() regenerates it from scratch
    every time)."""
    return (trade["symbol"], trade["entry_date"].date().isoformat(), trade["date"].date().isoformat(), trade["direction"])


def _queue_natural_keys(queue):
    """The plain natural-key list that gets persisted for resuming -
    see trade_review_session_progress's own schema comment in
    database.py for why the full trade dicts (datetimes, floats)
    aren't stored as-is."""
    return [
        {"symbol": s, "entry_date": e, "exit_date": x, "direction": d}
        for s, e, x, d in (_trade_key(t) for t in queue)
    ]


def _reorder_for_resume(trades, saved_order):
    """Filters/reorders TODAY's real closed trades to match
    `saved_order` (the natural-key list saved when a paused Review
    Session was started) - a saved trade no longer matching anything
    today (its underlying transactions were edited/re-imported since)
    is silently dropped, same as Journal Session's own
    _reorder_for_resume() in pages/2_Shortlist.py."""
    by_key = {_trade_key(t): t for t in trades}
    return [
        by_key[(s["symbol"], s["entry_date"], s["exit_date"], s["direction"])]
        for s in saved_order
        if (s["symbol"], s["entry_date"], s["exit_date"], s["direction"]) in by_key
    ]


def _advance_review_session(conn, session):
    """Moves the Review Session to the next trade: bumps the index,
    clears any snapshots captured for the trade just finished, persists
    the new progress, flags the next render to scroll back down and
    focus the notes box, and reruns. Shared by Save & Next and Skip."""
    session["index"] += 1
    session["pending_snapshots"] = {}
    database.save_review_session_progress(
        conn, session["report_id"], _queue_natural_keys(session["queue"]), session["index"])
    st.session_state["_scroll_to_review_anchor"] = True
    st.rerun()


def render_review_notes_box(key_prefix):
    """Trade Review's own note box + Save & Next/Skip buttons - modeled
    on Shortlist's render_journal_box() (same form/Ctrl+Enter/
    clear_on_submit reasoning), but its own function since the wording
    and backing store (a trade_reviews row, not a logbook_entries one)
    are different enough that sharing code would need more
    parameterization than it's worth."""
    clicked = None
    with st.form(key=f"{key_prefix}_review_form", clear_on_submit=True, border=False):
        box_col, button_col = st.columns([4, 1])
        notes = box_col.text_area("Trade Review Notes", height=68, key=f"{key_prefix}_notes")
        if button_col.form_submit_button("Save & Next →", type="primary", width="stretch"):
            clicked = "Save & Next →"
        if button_col.form_submit_button("Skip", width="stretch"):
            clicked = "Skip"
    return clicked, notes


def render_review_session(conn):
    """
    The guided Review Session: walks through every checked trade one at
    a time, full-screen, so reviewing all of them in one sitting is
    click-write-Save & Next instead of hunting for the next trade to
    look at every time. Mirrors pages/2_Shortlist.py's
    render_journal_session() closely.
    """
    session = st.session_state["review_session"]
    queue, index = session["queue"], session["index"]

    if index >= len(queue):
        database.clear_review_session_progress(conn)
        report = database.get_review_report(conn, session["report_id"])
        trade_count = len(report["reviews"]) if report else 0
        if trade_count == 0:
            # Every trade in the queue was Skipped - nothing worth
            # keeping, so don't leave an empty report cluttering the
            # Logbook's Trade Reviews list.
            database.delete_review_report(conn, session["report_id"])
            st.info("Session ended - nothing was saved, so no report was kept.")
        else:
            st.success(
                f"Review complete - {trade_count} trade(s) saved. "
                "See the Logbook page's Trade Reviews section to browse or email it."
            )
        if st.button("Back to Trade Analyzer"):
            del st.session_state["review_session"]
            st.rerun()
        return

    trade = queue[index]
    key_prefix = f"review_{index}"
    anchor_id = f"{key_prefix}_review_anchor"
    should_scroll = st.session_state.pop("_scroll_to_review_anchor", False)

    header_cols = st.columns([5, 1])
    header_cols[0].subheader(f"Reviewing {index + 1} of {len(queue)}: {trade_label(trade)}")
    if header_cols[1].button("Exit Session", key=f"{key_prefix}_exit"):
        # Deliberately does NOT clear the persisted progress - exiting
        # mid-session is exactly the "didn't finish" case the saved
        # progress is for, so it's still there to resume next time.
        del st.session_state["review_session"]
        st.rerun()
    st.progress(index / len(queue))

    render_trade_facts(trade)
    st.divider()

    timeframe_label, settings, chart_rendered = render_trade_chart(conn, trade, key_prefix, anchor_id=anchor_id)

    if should_scroll:
        ui.scroll_to_anchor(anchor_id)

    if not chart_rendered:
        # No price data for this one right now - nothing to capture or
        # journal against, so the only sensible move is on to the next
        # trade.
        if st.button("Skip →", key=f"{key_prefix}_skip"):
            _advance_review_session(conn, session)
        return

    pending = session.setdefault("pending_snapshots", {})
    capture_cols = st.columns([1, 3])
    if capture_cols[0].button("📸 Save this timeframe", key=f"{key_prefix}_capture"):
        with st.spinner(f"Saving {timeframe_label.lower()} snapshot..."):
            snapshot = charting.build_trade_review_snapshot(
                trade["symbol"], trade["entry_date"], trade["date"], trade["buy_price"], trade["sell_price"],
                trade["direction"], timeframe_label, settings)
        if snapshot is not None:
            pending[timeframe_label] = snapshot
        else:
            st.warning(f"No price data available to save a {timeframe_label.lower()} snapshot right now.")

    if pending:
        capture_cols[1].caption(f"Captured: {', '.join(pending.keys())}")
    else:
        capture_cols[1].caption('No timeframes captured yet - use "Save this timeframe" for any worth keeping.')

    if should_scroll:
        ui.focus_textarea("Trade Review Notes")

    clicked, notes = render_review_notes_box(key_prefix)

    if clicked == "Save & Next →":
        database.save_trade_review(conn, session["report_id"], trade, notes, pending)
        _advance_review_session(conn, session)
    elif clicked == "Skip":
        _advance_review_session(conn, session)


def render_review_selection(conn, trades):
    """
    Step one of starting a Review Session: pick a date range (filtered
    on EXIT date, not entry date - "review trades that closed in this
    period" is the natural framing for a periodic look-back, unlike the
    plain single-trade view's filter below which is about entry date),
    then check off exactly which of the matching trades to actually
    step through - reviewing is deliberate, not "every trade in this
    range whether you want to or not."
    """
    st.subheader("Select trades to review")

    exit_dates = [t["date"].date() for t in trades]
    min_exit, max_exit = min(exit_dates), max(exit_dates)
    date_range = st.date_input(
        "Filter by exit date", value=(min_exit, max_exit),
        min_value=min_exit, max_value=max_exit, key="review_date_range",
    )

    range_start = range_end = None
    filtered = trades
    if isinstance(date_range, tuple) and len(date_range) == 2:
        range_start, range_end = date_range
        filtered = [t for t in trades if range_start <= t["date"].date() <= range_end]

    filtered = sorted(filtered, key=lambda t: t["date"], reverse=True)

    if not filtered:
        st.info("No trades match this date range.")
    else:
        st.caption(f"{len(filtered)} trade(s) in this range - check the ones you want to review.")

    checked = []
    with st.container(height=300):
        for t in filtered:
            key = _trade_key(t)
            if st.checkbox(trade_label(t), key=f"review_pick_{key}"):
                checked.append(t)

    action_cols = st.columns([1, 1, 3])
    if action_cols[0].button("Begin Review", type="primary", disabled=not checked):
        report_id = database.create_review_report(conn, range_start, range_end)
        st.session_state["review_session"] = {
            "report_id": report_id, "queue": checked, "index": 0, "pending_snapshots": {},
        }
        database.save_review_session_progress(conn, report_id, _queue_natural_keys(checked), 0)
        st.session_state["_scroll_to_review_anchor"] = True
        del st.session_state["review_selecting"]
        st.rerun()
    if action_cols[1].button("Cancel"):
        del st.session_state["review_selecting"]
        st.rerun()


# --- Page body ----------------------------------------------------------

trades = load_trades()

if not trades:
    st.info("No trades found yet.")
    st.page_link(
        "pages/0_Import_Trades.py",
        label="Import your trade history to get started.",
        icon="↗️",
    )
    st.stop()

conn = database.get_connection()

if st.session_state.get("review_session") is not None:
    render_review_session(conn)
elif st.session_state.get("review_selecting"):
    render_review_selection(conn, trades)
else:
    # An unfinished Review Session from earlier (or before the tab was
    # closed, or the app went idle) - see database.get_review_session_
    # progress(). Rebuilt against TODAY's real closed trades, then
    # filtered down to the saved order - a trade no longer matching
    # (edited/re-imported transactions) is silently dropped.
    saved_progress = database.get_review_session_progress(conn)
    if saved_progress:
        resumed_queue = _reorder_for_resume(trades, saved_progress["queue"])
        resume_index = min(saved_progress["current_index"], len(resumed_queue))
        if resumed_queue and resume_index < len(resumed_queue):
            st.info(f"You have an unfinished Review Session ({resume_index + 1} of {len(resumed_queue)}).")
            resume_cols = st.columns([1, 1, 3])
            if resume_cols[0].button("▶ Resume Review", type="primary"):
                st.session_state["review_session"] = {
                    "report_id": saved_progress["report_id"], "queue": resumed_queue,
                    "index": resume_index, "pending_snapshots": {},
                }
                st.session_state["_scroll_to_review_anchor"] = True
                st.rerun()
            if resume_cols[1].button("Discard", key="discard_review"):
                database.delete_review_report(conn, saved_progress["report_id"])
                database.clear_review_session_progress(conn)
                st.rerun()
            st.divider()
        else:
            # Nothing left worth resuming (every trade in it is gone) -
            # clean up quietly instead of leaving a dead row behind.
            database.delete_review_report(conn, saved_progress["report_id"])
            database.clear_review_session_progress(conn)

    if st.button("🔍 Start Review Session"):
        st.session_state["review_selecting"] = True
        st.rerun()
    st.divider()

    trades_sorted = sorted(trades, key=lambda t: t["date"], reverse=True)

    entry_dates = [t["entry_date"].date() for t in trades_sorted]
    min_entry_date, max_entry_date = min(entry_dates), max(entry_dates)
    all_symbols = sorted({t["symbol"] for t in trades_sorted})

    filter_cols = st.columns([2, 2])
    date_range = filter_cols[0].date_input(
        "Filter by entry date", value=(min_entry_date, max_entry_date),
        min_value=min_entry_date, max_value=max_entry_date,
    )
    selected_symbols = filter_cols[1].multiselect(
        "Filter by ticker", options=all_symbols, default=all_symbols, key="trade_analyzer_ticker_filter",
    )

    # date_input in range mode returns a single date until both ends
    # have been picked - only filter once we actually have a (start,
    # end) pair, so the list doesn't collapse to nothing while a user
    # is mid-pick.
    if isinstance(date_range, tuple) and len(date_range) == 2:
        range_start, range_end = date_range
        trades_sorted = [t for t in trades_sorted if range_start <= t["entry_date"].date() <= range_end]

    trades_sorted = [t for t in trades_sorted if t["symbol"] in selected_symbols]

    if not trades_sorted:
        st.info("No trades match these filters.")
        st.stop()

    selected_index = st.selectbox(
        "Choose a trade", options=range(len(trades_sorted)),
        format_func=lambda i: trade_label(trades_sorted[i]),
    )
    trade = trades_sorted[selected_index]

    render_trade_facts(trade)
    st.divider()

    _timeframe_label, _settings, chart_rendered = render_trade_chart(conn, trade, "trade_analyzer")
    if not chart_rendered:
        st.stop()
