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
from analyze_trades import trade_label, trade_stats

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


def render_trade_facts(conn, trade):
    """The fact tiles (entry/exit, shares, P/L, % change, days held,
    equity contribution) for one closed trade - shared by the plain
    single-trade view and each step of the Review Session below. The
    actual numbers come from analyze_trades.trade_stats(), so the PDF/
    Logbook display of the same trade always agrees with what's shown
    here. Equity Contribution is left off entirely (not shown as a
    blank/zero tile) if the Jan 1 account value baseline hasn't been
    set yet on the Settings page - see trade_stats()'s own docstring
    for why there's nothing meaningful to divide by without it."""
    stats = trade_stats(
        trade["direction"], trade["buy_price"], trade["sell_price"], trade["quantity"],
        trade["profit_loss"], trade["entry_date"].date(), trade["date"].date(),
        account_value=database.get_account_value(conn),
    )
    outcome_color = charting.win_loss_color(trade["profit_loss"] >= 0)

    show_equity = stats["equity_contribution"] is not None
    cols = st.columns(7 if show_equity else 6)
    fact_tile(cols[0], "Short Entry" if stats["is_short"] else "Entry", f"${stats['entry_price']:,.2f}")
    fact_tile(cols[1], "Cover" if stats["is_short"] else "Exit", f"${stats['exit_price']:,.2f}")
    fact_tile(cols[2], "Shares", f"{trade['quantity']:,.0f}")
    fact_tile(cols[3], "P/L", f"${trade['profit_loss']:,.2f}", outcome_color)
    fact_tile(cols[4], "% Change", f"{stats['pct_change']:,.2f}%", outcome_color)
    fact_tile(cols[5], "Days Held", f"{stats['days_held']:,.0f}")
    if show_equity:
        fact_tile(cols[6], "Equity Contribution", f"{stats['equity_contribution']:,.2f}%", outcome_color)


def render_trade_chart(conn, trade, key_prefix, anchor_id=None, timeframe_label=None):
    """
    The Chart-Settings controls plus the interactive price chart for one
    closed trade - extracted from what used to be this page's own
    inline, one-shot code so the Review Session below can render several
    different trades (and re-render the SAME trade at a freshly picked
    timeframe) within one script run, the same reason Shortlist's
    render_price_chart() exists as a real function instead of inline
    page code.

    `timeframe_label`, if given, skips rendering the Timeframe radio
    here entirely and fetches/shows that timeframe instead - the Review
    Session renders its OWN Timeframe control further down the page
    (see render_review_session()), alongside Save & Next/Skip, instead
    of above the chart here. Leave it None (the default) to render the
    radio in its usual spot right above the chart, which the plain
    single-trade view below still does.

    Returns (timeframe_label, settings, chart_rendered, view_range) -
    the caller needs `timeframe_label`/`settings` to capture a matching
    snapshot via charting.build_trade_review_snapshot() when "Save this
    timeframe" is clicked, `chart_rendered` (False when there's no
    price data) to know whether to skip straight past the review
    controls instead of showing them against a chart that isn't there,
    and `view_range` (see charting.render_interactive_chart()) so that
    same snapshot can match wherever the chart's actually been panned/
    zoomed to instead of always resetting to the default view.
    """
    if timeframe_label is None:
        timeframe_options = list(charting.TIMEFRAMES.keys())
        timeframe_label = st.radio(
            "Timeframe", options=timeframe_options, index=timeframe_options.index("Daily"),
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
    # + right_margin_timedelta() so the last real candle doesn't sit
    # jammed against the right edge of the chart - see its own docstring.
    visible_end = trade["date"] + timedelta(days=padding_days) + charting.right_margin_timedelta(interval)

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
        return timeframe_label, settings, False, None

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
    chart_result = charting.render_interactive_chart(fig, fit_payload, saved_drawings, key=key_prefix)
    current_drawings = chart_result["drawings"]

    # Only writes to the database when something's actually different
    # from what's saved - see pages/2_Shortlist.py's own
    # render_price_chart() for the same pattern.
    if current_drawings != saved_drawings:
        database.save_drawings(conn, trade["symbol"], current_drawings)

    return timeframe_label, settings, True, chart_result["view_range"]


# --- Review Session ---------------------------------------------------

def _trade_key(trade):
    """The natural-key tuple that identifies one closed trade -
    symbol/entry_date/exit_date/direction/quantity/buy_price/sell_price,
    all as plain values so it's usable as both a dict key and a
    JSON-safe persisted value. NOT trades.id - see trade_reviews' own
    schema comment in database.py for why a trade's id isn't a stable
    identifier across an import (database.rebuild_trades() regenerates
    it from scratch every time).

    Includes quantity/buy_price/sell_price, not just symbol/dates/
    direction - LIFO matching can legitimately produce two SEPARATE
    closed trades for the same symbol with the identical entry AND
    exit date (e.g. a sell that filled in two separate executions the
    same day, matched against the same buy lot) - confirmed for real
    against production data (two DOCN trades, both 06/01 to 06/05
    LONG, one for 210 shares at $169.51, the other for 2 shares at
    $169.75). Without these extra fields, both collided onto the same
    key and crashed the selection checklist with a duplicate widget
    key. These fields are exactly what rebuild_trades() recomputes
    identically from the same transaction history, so this stays valid
    across an import the same way symbol/dates/direction already do.
    """
    return (
        trade["symbol"], trade["entry_date"].date().isoformat(), trade["date"].date().isoformat(),
        trade["direction"], trade["quantity"], trade["buy_price"], trade["sell_price"],
    )


def _queue_natural_keys(queue):
    """The plain natural-key list that gets persisted for resuming -
    see trade_review_session_progress's own schema comment in
    database.py for why the full trade dicts (datetimes, floats)
    aren't stored as-is."""
    return [
        {"symbol": s, "entry_date": e, "exit_date": x, "direction": d, "quantity": q, "buy_price": b, "sell_price": sp}
        for s, e, x, d, q, b, sp in (_trade_key(t) for t in queue)
    ]


def _saved_key_tuple(s):
    """The comparable tuple form of one saved natural-key dict (from
    _queue_natural_keys()) - shared by _reorder_for_resume() below."""
    return (s["symbol"], s["entry_date"], s["exit_date"], s["direction"], s["quantity"], s["buy_price"], s["sell_price"])


def _reorder_for_resume(trades, saved_order):
    """Filters/reorders TODAY's real closed trades to match
    `saved_order` (the natural-key list saved when a paused Review
    Session was started) - a saved trade no longer matching anything
    today (its underlying transactions were edited/re-imported since)
    is silently dropped, same as Journal Session's own
    _reorder_for_resume() in pages/2_Shortlist.py."""
    by_key = {_trade_key(t): t for t in trades}
    return [by_key[_saved_key_tuple(s)] for s in saved_order if _saved_key_tuple(s) in by_key]


def _jump_review_session(conn, session, new_index):
    """Moves the Review Session directly to `new_index` - used by the
    Previous button and the trade-jump dropdown below. Same "clear this
    trade's scratch snapshot state, persist, scroll+focus on arrival,
    rerun" as _advance_review_session() below, just to an arbitrary
    index instead of always the next one."""
    session["index"] = new_index
    session["pending_snapshots"] = {}
    database.save_review_session_progress(
        conn, session["report_id"], _queue_natural_keys(session["queue"]), session["index"])
    st.session_state["_scroll_to_review_anchor"] = True
    st.rerun()


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


def _render_review_intro(conn, session):
    """The Review Session's opening journal entry, shown once before
    the first trade - "predictions_notes" being NULL is what signals
    this hasn't been written yet (see database.get_review_report()),
    so resuming an in-progress session never asks again."""
    st.subheader("Before You Begin")
    with st.form(key="review_intro_form", clear_on_submit=True, border=False):
        notes = st.text_area(
            "How do you feel about the upcoming review session, and what are your predictions?",
            height=100, key="review_intro_notes",
        )
        if st.form_submit_button("Continue →", type="primary"):
            database.save_review_predictions_notes(conn, session["report_id"], notes)
            st.rerun()


def _review_session_summary(reviews):
    """
    Aggregate stats across every trade actually reviewed (saved) this
    session - batting average (win rate), win/loss $ (average AND
    total, since either framing is useful - a good average with a
    small total isn't the same story as a good total with a huge
    average), win/loss % (average price move on winners vs losers, via
    analyze_trades.trade_stats() - the same math the fact tiles and PDF
    already use), and a per-ticker breakdown. Returns None if `reviews`
    is empty - nothing to summarize.
    """
    if not reviews:
        return None

    winners = [r for r in reviews if r["profit_loss"] >= 0]
    losers = [r for r in reviews if r["profit_loss"] < 0]

    def avg(values):
        return sum(values) / len(values) if values else None

    win_pcts, loss_pcts = [], []
    for r in reviews:
        stats = trade_stats(
            r["direction"], r["buy_price"], r["sell_price"], r["quantity"],
            r["profit_loss"], r["entry_date"], r["exit_date"])
        (win_pcts if r["profit_loss"] >= 0 else loss_pcts).append(stats["pct_change"])

    per_ticker = {}
    for r in reviews:
        row = per_ticker.setdefault(r["symbol"], {"trades": 0, "wins": 0, "net_pl": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if r["profit_loss"] >= 0 else 0
        row["net_pl"] += r["profit_loss"]

    return {
        "total": len(reviews),
        "win_count": len(winners), "loss_count": len(losers),
        "batting_avg": len(winners) / len(reviews) * 100,
        "total_pl": sum(r["profit_loss"] for r in reviews),
        "avg_win_dollar": avg([r["profit_loss"] for r in winners]),
        "avg_loss_dollar": avg([r["profit_loss"] for r in losers]),
        "total_win_dollar": sum(r["profit_loss"] for r in winners) if winners else None,
        "total_loss_dollar": sum(r["profit_loss"] for r in losers) if losers else None,
        "avg_win_pct": avg(win_pcts),
        "avg_loss_pct": avg(loss_pcts),
        "per_ticker": per_ticker,
    }


def _render_review_summary(reviews):
    """Renders _review_session_summary()'s numbers - shown once, on the
    Reflections screen, so the session's closing journal entry can
    actually be written with the real numbers in view instead of from
    memory."""
    summary = _review_session_summary(reviews)
    if summary is None:
        return
    st.subheader("Session Summary")

    win_color = charting.win_loss_color(True)
    loss_color = charting.win_loss_color(False)

    top_cols = st.columns(4)
    ui.stat_tile(top_cols[0], "Batting Average", f"{summary['batting_avg']:.1f}%")
    ui.stat_tile(
        top_cols[1], "Total P/L", f"${summary['total_pl']:,.2f}",
        win_color if summary["total_pl"] >= 0 else loss_color)
    ui.stat_tile(top_cols[2], "Wins", f"{summary['win_count']}", win_color)
    ui.stat_tile(top_cols[3], "Losses", f"{summary['loss_count']}", loss_color)

    bottom_cols = st.columns(4)
    ui.stat_tile(
        bottom_cols[0], "Avg Win $",
        f"${summary['avg_win_dollar']:,.2f}" if summary["avg_win_dollar"] is not None else "—", win_color)
    ui.stat_tile(
        bottom_cols[1], "Avg Loss $",
        f"${summary['avg_loss_dollar']:,.2f}" if summary["avg_loss_dollar"] is not None else "—", loss_color)
    ui.stat_tile(
        bottom_cols[2], "Avg Win %",
        f"{summary['avg_win_pct']:,.2f}%" if summary["avg_win_pct"] is not None else "—", win_color)
    ui.stat_tile(
        bottom_cols[3], "Avg Loss %",
        f"{summary['avg_loss_pct']:,.2f}%" if summary["avg_loss_pct"] is not None else "—", loss_color)

    st.caption(
        "Win $/Loss $ totals: "
        + (f"${summary['total_win_dollar']:,.2f} won" if summary["total_win_dollar"] is not None else "no wins")
        + "  ·  "
        + (f"${summary['total_loss_dollar']:,.2f} lost" if summary["total_loss_dollar"] is not None else "no losses")
    )

    per_ticker_rows = sorted(
        (
            {
                "Symbol": symbol, "Trades": row["trades"], "Wins": row["wins"],
                "Losses": row["trades"] - row["wins"],
                "Win Rate": f"{row['wins'] / row['trades'] * 100:.0f}%",
                "Net P/L": row["net_pl"],
            }
            for symbol, row in summary["per_ticker"].items()
        ),
        key=lambda row: row["Net P/L"], reverse=True,
    )
    st.dataframe(
        per_ticker_rows, hide_index=True, width="stretch",
        column_config={"Net P/L": st.column_config.NumberColumn(format="$%.2f")},
    )
    st.divider()


def _render_review_outro(conn, session, trade_count, reviews):
    """The Review Session's closing journal entry, shown once after the
    last trade (only when at least one trade was actually saved - see
    caller) - same NULL-means-not-written-yet signal as the intro
    above. `reviews` (report["reviews"]) drives the Session Summary
    shown just above the Reflections box - see _render_review_summary()."""
    _render_review_summary(reviews)
    st.subheader("Reflections")
    with st.form(key="review_outro_form", clear_on_submit=True, border=False):
        notes = st.text_area("Reflections", height=100, key="review_outro_notes")
        if st.form_submit_button("Finish →", type="primary"):
            database.save_review_reflections_notes(conn, session["report_id"], notes)
            st.rerun()


def render_review_session(conn):
    """
    The guided Review Session: walks through every checked trade one at
    a time, full-screen, so reviewing all of them in one sitting is
    click-write-Save & Next instead of hunting for the next trade to
    look at every time. Mirrors pages/2_Shortlist.py's
    render_journal_session() closely. Bookended by a one-time journal
    entry before the first trade and after the last (see
    _render_review_intro()/_render_review_outro() above).
    """
    session = st.session_state["review_session"]
    queue, index = session["queue"], session["index"]

    report = database.get_review_report(conn, session["report_id"])
    if report["predictions_notes"] is None:
        _render_review_intro(conn, session)
        return

    if index >= len(queue):
        trade_count = len(report["reviews"])
        if trade_count == 0:
            # Every trade in the queue was Skipped - nothing worth
            # keeping, so don't leave an empty report cluttering the
            # Logbook's Trade Reviews list. No point asking for
            # Reflections on a session with nothing reviewed in it.
            database.clear_review_session_progress(conn)
            database.delete_review_report(conn, session["report_id"])
            st.info("Session ended - nothing was saved, so no report was kept.")
        elif report["reflections_notes"] is None:
            _render_review_outro(conn, session, trade_count, report["reviews"])
            return
        else:
            database.clear_review_session_progress(conn)
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

    # Whatever's already saved for THIS trade in THIS report, if
    # anything - non-None whenever the trade-jump dropdown or Previous
    # brought us back to one already reviewed earlier this session (or
    # a resumed one from a prior sitting). Drives the notes prefill and
    # the "already captured" snapshot caption below, and tells Save &
    # Next whether to update this row instead of inserting a duplicate.
    existing_review = database.get_review_for_trade(conn, session["report_id"], trade)

    # Which trades in the queue already have a saved review - used for
    # both the "N of M (K reviewed)" counter and the jump dropdown's
    # 🟢/⚪ markers just below.
    reviewed_keys = {
        (r["symbol"], r["entry_date"].isoformat(), r["exit_date"].isoformat(),
         r["direction"], r["quantity"], r["buy_price"], r["sell_price"])
        for r in report["reviews"]
    }
    reviewed_count = sum(1 for t in queue if _trade_key(t) in reviewed_keys)

    header_cols = st.columns([5, 1])
    header_cols[0].subheader(
        f"Reviewing {index + 1} of {len(queue)} ({reviewed_count} reviewed): {trade_label(trade)}")
    if header_cols[1].button("Exit Session", key=f"{key_prefix}_exit"):
        # Deliberately does NOT clear the persisted progress - exiting
        # mid-session is exactly the "didn't finish" case the saved
        # progress is for, so it's still there to resume next time.
        del st.session_state["review_session"]
        st.rerun()
    st.progress(index / len(queue))

    # Jump straight to any trade in the queue - marked 🟢 already
    # reviewed vs ⚪ not yet, so it doubles as a progress map. The
    # widget's key includes `index`, not just the report id - that
    # forces a BRAND NEW key (freshly re-seeded to wherever we actually
    # landed) every time the index changes for ANY reason (this
    # dropdown, Previous, Save & Next, Skip), instead of trying to
    # manually keep a stable widget's remembered value in sync with
    # session["index"] from several different directions.
    jump_labels = [
        f"{'🟢' if _trade_key(t) in reviewed_keys else '⚪'} {i + 1}. {trade_label(t)}"
        for i, t in enumerate(queue)
    ]
    jump_choice = st.selectbox(
        "Jump to a trade", options=jump_labels, index=index,
        key=f"review_jump_select_{session['report_id']}_{index}")
    jump_index = jump_labels.index(jump_choice)
    if jump_index != index:
        _jump_review_session(conn, session, jump_index)

    render_trade_facts(conn, trade)
    st.divider()

    # The Timeframe control itself is rendered further down, alongside
    # Save & Next/Skip (see controls_col below), not above the chart
    # like the plain single-trade view still does - but the chart needs
    # to know which timeframe to fetch/show before that widget renders.
    # This reads whatever was chosen on a PRIOR run (default "Daily" the
    # very first time this trade is shown) straight from session_state,
    # under the exact same key the radio widget itself uses further
    # down - reading a widget's key before that widget renders this run
    # is fine, it's only ASSIGNING to it after creation that Streamlit
    # rejects.
    timeframe_key = f"{key_prefix}_timeframe"
    timeframe_options = list(charting.TIMEFRAMES.keys())
    timeframe_label = st.session_state.get(timeframe_key, "Daily")

    _timeframe_label, settings, chart_rendered, current_view_range = render_trade_chart(
        conn, trade, key_prefix, anchor_id=anchor_id, timeframe_label=timeframe_label)

    if should_scroll:
        ui.scroll_to_anchor(anchor_id)

    if not chart_rendered:
        # No price data for this one right now - nothing to capture or
        # journal against, so the only sensible move is on to the next
        # trade.
        nav_cols = st.columns([1, 1, 4])
        if nav_cols[0].button("← Previous", disabled=index == 0, key=f"{key_prefix}_previous_nodata"):
            _jump_review_session(conn, session, index - 1)
        if nav_cols[1].button("Skip →", key=f"{key_prefix}_skip"):
            _advance_review_session(conn, session)
        return

    pending = session.setdefault("pending_snapshots", {})

    # One single row, left to right: notes box (widest), Save this
    # timeframe + Timeframe selector, Previous/Save & Next/Skip.
    # Nothing here needs to be inside an st.form() anymore (see
    # _advance_review_session() - Save & Next/Skip both move to a new
    # trade with a fresh key_prefix, so there's nothing to "clear on
    # submit"; this widget instance just never renders again), which is
    # what makes a single st.columns() band - and true flush alignment
    # with no gap - possible at all.
    notes_col, timeframe_col, buttons_col = st.columns([3, 1, 1])

    if timeframe_col.button("📸 Save this timeframe", key=f"{key_prefix}_capture"):
        # Passing current_view_range means this captures wherever the
        # chart's actually been panned/zoomed to (if at all) instead of
        # always resetting to the default entry/exit-date-derived view -
        # see charting.build_trade_review_snapshot()'s own docstring.
        with st.spinner(f"Saving {timeframe_label.lower()} snapshot..."):
            snapshot = charting.build_trade_review_snapshot(
                trade["symbol"], trade["entry_date"], trade["date"], trade["buy_price"], trade["sell_price"],
                trade["direction"], timeframe_label, settings, view_range_override=current_view_range)
        if snapshot is not None:
            pending[timeframe_label] = snapshot
        else:
            st.warning(f"No price data available to save a {timeframe_label.lower()} snapshot right now.")

    timeframe_col.selectbox(
        "Timeframe", options=timeframe_options, index=timeframe_options.index(timeframe_label),
        key=timeframe_key)

    already_saved_timeframes = set(pending) | (set(existing_review["snapshots"]) if existing_review else set())
    if already_saved_timeframes:
        timeframe_col.caption(f"Already captured: {', '.join(sorted(already_saved_timeframes))}")

    if should_scroll:
        ui.focus_textarea("Trade Review Notes")

    # Keyed on whether a saved review exists, not just `key_prefix` -
    # `key_prefix` alone stays IDENTICAL across visits to this same
    # index (Previous/the jump dropdown don't change it), and Streamlit
    # only honors `value=` the FIRST time a given key is ever created in
    # a session. Revisiting a trade that got saved AFTER this key first
    # existed needs a genuinely NEW key to actually show the now-saved
    # notes instead of silently keeping whatever blank/stale value the
    # box had the first time it was ever rendered this session.
    notes_key = f"{key_prefix}_notes_{'reviewed' if existing_review else 'new'}"
    notes = notes_col.text_area(
        "Trade Review Notes", value=(existing_review["notes"] if existing_review else "") or "",
        height=68, key=notes_key)

    clicked = None
    if buttons_col.button("← Previous", width="stretch", disabled=index == 0, key=f"{key_prefix}_previous"):
        _jump_review_session(conn, session, index - 1)
    if buttons_col.button("Save & Next →", type="primary", width="stretch", key=f"{key_prefix}_save_next"):
        clicked = "Save & Next →"
    if buttons_col.button("Skip", width="stretch", key=f"{key_prefix}_skip_btn"):
        clicked = "Skip"

    if clicked == "Save & Next →":
        # Daily is always saved, even if never explicitly captured above -
        # every other timeframe stays opt-in, on top of this guaranteed
        # baseline. Skipped if it's already in `pending`, OR already
        # saved on an existing review being re-visited - no need to
        # re-fetch and overwrite an unchanged Daily snapshot just
        # because a note got tweaked.
        if "Daily" not in already_saved_timeframes:
            with st.spinner("Saving Daily snapshot..."):
                daily_snapshot = charting.build_trade_review_snapshot(
                    trade["symbol"], trade["entry_date"], trade["date"], trade["buy_price"], trade["sell_price"],
                    trade["direction"], "Daily", settings)
            if daily_snapshot is not None:
                pending["Daily"] = daily_snapshot
        if existing_review:
            database.update_trade_review(conn, existing_review["id"], notes, pending)
        else:
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

    def clamped_preset(start, end):
        """Clamps a preset range to the actual trade history's bounds
        (date_input raises if given a value outside its min/max) -
        every week/month option below is built FROM a real exit date,
        so this only ever narrows a range that already contains at
        least one trade, never invalidates it."""
        return max(start, min_exit), min(end, max_exit)

    def month_end(first_of_month):
        next_month = (
            first_of_month.replace(year=first_of_month.year + 1, month=1) if first_of_month.month == 12
            else first_of_month.replace(month=first_of_month.month + 1)
        )
        return next_month - timedelta(days=1)

    # Every distinct Monday-Sunday week, and every distinct calendar
    # month, that actually has at least one trade closed in it - most
    # recent first - so these dropdowns only ever offer a period worth
    # reviewing, not a blind calendar grid full of empty stretches.
    week_starts = sorted({d - timedelta(days=d.weekday()) for d in exit_dates}, reverse=True)
    month_starts = sorted({d.replace(day=1) for d in exit_dates}, reverse=True)
    week_options = {f"{w:%m/%d/%Y} - {w + timedelta(days=6):%m/%d/%Y}": w for w in week_starts}
    month_options = {f"{m:%B %Y}": m for m in month_starts}

    # Each dropdown applies to the date_input's OWN session_state value
    # (its key, below) and reruns - a faster way to fill in the same
    # picker, not a second, parallel source of truth for the range.
    # Compared against the last label actually applied (not just
    # "isn't the placeholder") so re-rendering after that rerun doesn't
    # re-trigger the exact same apply-and-rerun forever.
    preset_cols = st.columns([2, 2, 3])
    week_choice = preset_cols[0].selectbox(
        "Jump to a week", options=["Choose a week..."] + list(week_options), key="review_week_picker")
    if week_choice != "Choose a week..." and week_choice != st.session_state.get("_applied_week_choice"):
        st.session_state["_applied_week_choice"] = week_choice
        monday = week_options[week_choice]
        st.session_state["review_date_range"] = clamped_preset(monday, monday + timedelta(days=6))
        st.session_state["_review_precheck"] = True
        st.rerun()

    month_choice = preset_cols[1].selectbox(
        "Jump to a month", options=["Choose a month..."] + list(month_options), key="review_month_picker")
    if month_choice != "Choose a month..." and month_choice != st.session_state.get("_applied_month_choice"):
        st.session_state["_applied_month_choice"] = month_choice
        first_of_month = month_options[month_choice]
        st.session_state["review_date_range"] = clamped_preset(first_of_month, month_end(first_of_month))
        st.session_state["_review_precheck"] = True
        st.rerun()

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

    # Only true for the ONE render right after a week/month dropdown
    # just filled in the range above - pop (not get) so it applies
    # exactly once, not to every trade that ever shows up under this
    # filter afterward (a later manual widening of the date range, or
    # even just toggling some OTHER checkbox, also reruns this whole
    # script). A checkbox's `value=` only matters the very first time
    # its own key is instantiated anyway (see the key comment just
    # below), which is exactly this same one render - after that,
    # Streamlit remembers whatever the user actually left it at.
    precheck = st.session_state.pop("_review_precheck", False)

    def _checkbox_key(i, t):
        return f"review_pick_{i}_{_trade_key(t)}"

    if filtered:
        select_cols = st.columns([1, 1, 4])
        # Sets every CURRENTLY FILTERED trade's checkbox state directly,
        # rather than reusing the `_review_precheck`-on-first-render
        # trick above - that trick only seeds a checkbox's value the
        # FIRST time its key is ever created, so it can't retroactively
        # change boxes that already exist from an earlier render (e.g.
        # after manually toggling a few, or after the date range/preset
        # already rendered this same list once).
        if select_cols[0].button("Select All", key="review_select_all"):
            for i, t in enumerate(filtered):
                st.session_state[_checkbox_key(i, t)] = True
            st.rerun()
        if select_cols[1].button("Clear All", key="review_clear_all"):
            for i, t in enumerate(filtered):
                st.session_state[_checkbox_key(i, t)] = False
            st.rerun()

    checked = []
    with st.container(height=300):
        # Keyed by loop position, not just _trade_key(t) - belt and
        # suspenders on top of that key already being strengthened to
        # include quantity/prices (see _trade_key()'s own docstring for
        # the real duplicate-trade case that motivated both fixes).
        for i, t in enumerate(filtered):
            if st.checkbox(trade_label(t), value=precheck, key=_checkbox_key(i, t)):
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
        "pages/5_Settings.py",
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

    render_trade_facts(conn, trade)
    st.divider()

    _timeframe_label, _settings, chart_rendered, _view_range = render_trade_chart(conn, trade, "trade_analyzer")
    if not chart_rendered:
        st.stop()
