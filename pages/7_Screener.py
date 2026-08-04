"""
Screener
=====================
Paste a list of tickers, click Run, get back a short list of setups
with a real buy trigger, stop, and position size - built to scan
several hundred names in seconds and surface roughly one candidate a
week, not a long list requiring judgment calls. The filter logic
itself lives in screener/engine.py (refactored from the tested
daily_screen.py CLI script - see that module's own docstring); this
page is the UI/data/logging wrapper around it.

TWO TIERS, always both computed, only strict shown by default (see
screener.engine's own module docstring for the tested signal-rate/R
numbers behind that default). Every baseline-passing candidate gets
logged to signal_log regardless of which tier is currently displayed -
see database.save_signals() - so a filter choice stays reviewable later
even for candidates you never actually looked at.

Two tabs:
  Run      Paste tickers, screen, see today's (plus the two prior
           sessions' still-live) setups.
  History  Every signal ever logged, filterable, with reconciled
           outcomes (screener.reconcile) once known - the win-rate/R
           stats that make it possible to tell whether the strict
           filter (or your own discretionary skips) are actually
           helping.
"""
from datetime import timedelta

import pandas as pd
import streamlit as st

import auth
import database
import nav
import screener.data as screener_data
import screener.engine as engine
import screener.reconcile as reconcile
import timeutil
import ui

st.set_page_config(page_title="Screener", page_icon="🔎", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("screener")

st.title("Screener")
st.caption(
    "Screening and watchlist only - no auto-execution, no order placement, no "
    "trade recommendations. Informational, not investment advice."
)

conn = database.get_connection()


# --- Advanced panel: P overrides ---------------------------------------

def _render_advanced_panel():
    """
    Exposes the subset of screener.engine.P this page's spec calls out
    (min price, min ADV, min ADR%, RS excess threshold, both near-high
    thresholds, RS line tolerance, stop ADR multiple, max risk %, risk
    per trade %) as plain number inputs, collapsed by default. Returns
    the full overridden params dict (base P plus whatever's here) -
    passed to engine.screen_tickers() per-call, never mutating P itself
    (see engine.indicators()'s own note on why that matters for a
    shared deployment).
    """
    with st.expander("Advanced: filter thresholds", expanded=False):
        st.caption("Defaults match screener.engine.P - the tested 2022-2026 backtest settings.")
        c1, c2, c3 = st.columns(3)
        min_price = c1.number_input("Min Price ($)", min_value=0.0, value=engine.P["MIN_PRICE"], step=1.0)
        min_adv_m = c1.number_input(
            "Min Avg $ Volume ($M)", min_value=0.0, value=engine.P["MIN_ADV"] / 1e6, step=1.0)
        min_adr = c1.number_input("Min ADR %", min_value=0.0, value=engine.P["MIN_ADR"], step=0.5)
        rs_excess_pts = c2.number_input(
            "RS Excess Threshold (points)", value=engine.P["RS_EXCESS"] * 100, step=0.5,
            help="6-month return must beat the benchmark's by more than this many percentage points.")
        near_high_base_pct = c2.number_input(
            "Baseline: within X% of 6-mo high", min_value=0.0,
            value=engine.P["NEAR_HIGH_BASE"] * 100, step=1.0)
        near_high_strict_pct = c2.number_input(
            "Strict: within X% of 6-mo high", min_value=0.0,
            value=engine.P["NEAR_HIGH_STRICT"] * 100, step=1.0)
        rsline_tol_pct = c3.number_input(
            "RS line within X% of its own 6-mo high", min_value=0.0,
            value=(1 - engine.P["RSLINE_TOL"]) * 100, step=0.5)
        stop_adr_mult = c3.number_input(
            "Stop = NR7 low - X x ADR%", min_value=0.0, value=engine.P["STOP_ADR_MULT"], step=0.1)
        max_risk_pct = c3.number_input(
            "Max Risk % (trigger to stop)", min_value=0.1, value=engine.P["MAX_RISK_PCT"], step=0.5)
        risk_per_trade_pct = c3.number_input(
            "Risk per Trade (% of equity)", min_value=0.01, value=engine.P["RISK_PER_TRADE"] * 100, step=0.1)

    return {
        **engine.P,
        "MIN_PRICE": min_price,
        "MIN_ADV": min_adv_m * 1e6,
        "MIN_ADR": min_adr,
        "RS_EXCESS": rs_excess_pts / 100,
        "NEAR_HIGH_BASE": near_high_base_pct / 100,
        "NEAR_HIGH_STRICT": near_high_strict_pct / 100,
        "RSLINE_TOL": 1 - rsline_tol_pct / 100,
        "STOP_ADR_MULT": stop_adr_mult,
        "MAX_RISK_PCT": max_risk_pct,
        "RISK_PER_TRADE": risk_per_trade_pct / 100,
    }


# --- Run tab -------------------------------------------------------------

def _known_chart_tickers(conn):
    watchlist = {w["symbol"] for w in database.get_watchlist(conn)}
    positions = {p["symbol"] for p in database.get_open_positions(conn)}
    return watchlist, positions


def _render_header(result, screened_count):
    gate_col, ret_col, count_col = st.columns(3)
    gate_label = "OPEN" if result.gate_open else "CLOSED"
    with gate_col:
        st.metric("Market Gate", gate_label,
                   f"{result.gate_pct:+.2f}% vs 21 EMA" if pd.notna(result.gate_pct) else None)
        st.caption(f"{result.benchmark_label}, as of {result.asof}")
    with ret_col:
        bm_return = f"{result.benchmark_return_pct:+.1f}%" if result.benchmark_return_pct is not None else "N/A"
        st.metric("Benchmark 6-Mo Return", bm_return)
        st.caption("Candidates must beat this over the same window.")
    with count_col:
        st.metric("Screened", screened_count)
        st.caption(f"{len(result.strict)} strict, {len(result.baseline_only)} baseline")

    if not result.gate_open:
        st.warning(
            "Market gate is CLOSED - no new entries are indicated right now. "
            "Results below are still shown and logged, but treat them as "
            "informational only."
        )


def _render_funnel(result, insufficient, failed):
    with st.expander("Funnel breakdown (why the list is short)", expanded=len(result.watchlist) == 0):
        rows = []
        if insufficient:
            rows.append(("Insufficient history", len(insufficient)))
        if failed:
            rows.append(("Could not fetch", len(failed)))
        stage_labels = {
            "below_liquidity_floor": "Below liquidity floor",
            "below_price_floor": "Below price floor",
            "adr_too_low": "ADR too low",
            "below_moving_averages": "Below 21 EMA / 50 SMA",
            "not_near_high": "Not near 6-month high",
            "no_nr7": "No NR7 (narrowest range of 7)",
            "volume_not_contracting": "Volume not contracting",
            "did_not_beat_benchmark": "Didn't beat benchmark",
            "risk_too_wide": "Risk too wide (trigger/stop)",
        }
        for stage, count in result.funnel.items():
            rows.append((stage_labels.get(stage, stage), count))
        funnel_df = pd.DataFrame(rows, columns=["Stage", "Tickers eliminated"])
        st.dataframe(funnel_df, hide_index=True, width="stretch")

        if insufficient:
            detail = ", ".join(f"{t} ({n} bars, need {screener_data.MIN_TRADING_DAYS})"
                                for t, n in insufficient.items())
            st.caption(f"Insufficient data: {detail}")
        if failed:
            st.caption(f"Could not fetch: {', '.join(failed)}")


def _results_display_df(show_df, watchlist_symbols, position_symbols, show_tier):
    display = show_df.copy()
    display["Chart"] = display["ticker"].apply(
        lambda t: "In app" if t in watchlist_symbols or t in position_symbols else "")
    display["NR7-2"] = display["nr7_2"].apply(lambda v: "⚡" if v else "")
    cols = {
        "ticker": "Ticker", "trigger": "Trigger", "stop": "Stop", "risk_pct": "Risk %",
        "shares": "Shares", "position": "Position $", "risk_usd": "Risk $", "adr": "ADR %",
        "rs_excess": "RS Excess", "pct_off_high": "% Off High", "age": "Age", "expires": "Expires",
        "NR7-2": "NR7-2", "Chart": "Chart",
    }
    if show_tier:
        cols = {"tier": "Tier", **cols}
    return display[list(cols.keys())].rename(columns=cols)


def _render_results(result, show_baseline, watchlist_symbols, position_symbols):
    show_df = result.watchlist if show_baseline else result.strict

    if show_df.empty:
        st.info("Nothing at the strict tier today." if not show_baseline else "No qualifying setups today.")
        return

    display = show_df.sort_values(["rs_excess", "risk_pct"], ascending=[False, True])
    st.subheader(f"Watchlist - {len(display)} pending trigger(s)")
    st.dataframe(
        _results_display_df(display, watchlist_symbols, position_symbols, show_tier=show_baseline),
        hide_index=True, width="stretch",
        column_config={
            "Trigger": st.column_config.NumberColumn(format="$%.2f"),
            "Stop": st.column_config.NumberColumn(format="$%.2f"),
            "Risk %": st.column_config.NumberColumn(format="%.2f%%"),
            "Position $": st.column_config.NumberColumn(format="$%.0f"),
            "Risk $": st.column_config.NumberColumn(format="$%.0f"),
            "ADR %": st.column_config.NumberColumn(format="%.2f%%"),
            "RS Excess": st.column_config.NumberColumn(format="%.1f"),
            "% Off High": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

    flagged = display[display.nr7_2]
    if len(flagged):
        st.caption(
            f"⚡ NR7-2 (two consecutive narrow-range bars, higher odds of immediate "
            f"expansion): {', '.join(flagged.ticker)}"
        )

    linkable = [t for t in display.ticker if t in watchlist_symbols or t in position_symbols]
    if linkable:
        jump_col, button_col = st.columns([3, 1])
        chosen = jump_col.selectbox("View a ticker's chart in this app", linkable, key="screener_jump_ticker")
        if button_col.button("Open Chart", key="screener_jump_button"):
            source = "position" if chosen in position_symbols else "watchlist"
            st.session_state["watchlist_selected"] = {"symbol": chosen, "source": source}
            st.switch_page("pages/2_Shortlist.py")

    st.caption(
        "Age 0 = flagged today, 1-2 = carried from a prior session (still live for "
        "up to 3 sessions). RS Excess = 6-month return minus the benchmark's, in "
        "points. Buy stop at Trigger; initial stop as shown."
    )

    total_risk = display.risk_usd.sum()
    equity = st.session_state.get("screener_last_equity", 0)
    risk_pct_of_equity = (total_risk / equity * 100) if equity else None
    footer = f"Total risk if every trigger fills: ${total_risk:,.0f}"
    if risk_pct_of_equity is not None:
        footer += f" ({risk_pct_of_equity:.1f}% of equity)"
    st.markdown(f"**{footer}**")

    csv_bytes = _results_display_df(
        display, watchlist_symbols, position_symbols, show_tier=show_baseline).to_csv(index=False)
    st.download_button(
        "Download CSV", data=csv_bytes,
        file_name=f"screener_{result.asof}.csv", mime="text/csv",
    )
    st.caption("Screening and watchlist only - informational, not investment advice.")


def render_run_tab(conn):
    st.subheader("Run a Screen")

    ticker_text = st.text_area(
        "Tickers (comma, space, tab, or newline separated)",
        height=100, key="screener_ticker_text",
        placeholder="AAPL, MSFT, NVDA\nAMD TSLA",
    )
    tickers = ui.parse_ticker_input(ticker_text)
    st.caption(f"{len(tickers)} ticker(s) parsed" + (f": {', '.join(tickers[:20])}"
               + (", ..." if len(tickers) > 20 else "") if tickers else ""))

    input_cols = st.columns(2)
    equity = input_cols[0].number_input("Account Equity ($)", min_value=0.0, value=10_000.0, step=1000.0)
    asof_date = input_cols[1].date_input(
        "As-of date", value=timeutil.expected_last_trading_day(),
        max_value=timeutil.expected_last_trading_day())

    params = _render_advanced_panel()

    run_clicked = st.button("Run", type="primary", disabled=len(tickers) == 0)

    if run_clicked:
        progress_bar = st.progress(0.0, text="Fetching price data...")

        def _progress(done, total, ticker):
            progress_bar.progress(done / total, text=f"Fetching price data... ({done}/{total}) {ticker}")

        try:
            fetch_result = screener_data.fetch_universe(tickers, benchmark="QQQ", progress_callback=_progress)
        except screener_data.DataFetchError as exc:
            progress_bar.empty()
            st.error(str(exc))
            st.stop()
        progress_bar.empty()

        try:
            with st.spinner("Screening..."):
                result = engine.screen_tickers(
                    fetch_result.prices, asof_date, equity, benchmark="QQQ", params=params)
        except engine.ScreenError as exc:
            st.error(str(exc))
            st.stop()

        new_count = database.save_signals(conn, result.watchlist)

        screened_count = len(set(fetch_result.prices.ticker.unique()) - {"QQQ"})

        st.session_state["screener_result"] = result
        st.session_state["screener_fetch_result"] = fetch_result
        st.session_state["screener_screened_count"] = screened_count
        st.session_state["screener_last_equity"] = equity
        st.session_state["screener_new_signals_logged"] = new_count

    result = st.session_state.get("screener_result")
    if result is None:
        st.info("Paste a ticker list above and click Run to screen.")
        return

    fetch_result = st.session_state["screener_fetch_result"]
    screened_count = st.session_state["screener_screened_count"]
    new_count = st.session_state.get("screener_new_signals_logged", 0)

    st.divider()
    _render_header(result, screened_count)
    st.caption(f"{new_count} new signal(s) logged this run (already-logged ones are never re-logged).")
    _render_funnel(result, fetch_result.insufficient, fetch_result.failed)

    show_baseline = st.checkbox("Show baseline too", key="screener_show_baseline")
    watchlist_symbols, position_symbols = _known_chart_tickers(conn)
    _render_results(result, show_baseline, watchlist_symbols, position_symbols)


# --- History tab -----------------------------------------------------------

def _summary_stats(signals):
    """signals: list of dicts (see database.get_signals_for_history()).
    Every rate/average here is computed only over signals where that
    particular field is actually known - a still-open or never-checked
    signal doesn't silently count as a loss or a non-trigger."""
    total = len(signals)
    known_trigger = [s for s in signals if s["triggered"] is not None]
    triggered = [s for s in known_trigger if s["triggered"]]
    completed = [s for s in signals if s["outcome_r"] is not None]
    winners = [s for s in completed if s["outcome_r"] > 0]

    return {
        "count": total,
        "trigger_rate": (len(triggered) / len(known_trigger) * 100) if known_trigger else None,
        "win_rate": (len(winners) / len(completed) * 100) if completed else None,
        "avg_r": (sum(s["outcome_r"] for s in completed) / len(completed)) if completed else None,
        "avg_hold": (sum(s["outcome_bars"] for s in completed) / len(completed)) if completed else None,
        "completed_count": len(completed),
    }


def _render_stat_row(label, stats):
    cols = st.columns(5)
    cols[0].metric(f"{label}: Signals", stats["count"])
    cols[1].metric("Trigger Rate", f"{stats['trigger_rate']:.0f}%" if stats["trigger_rate"] is not None else "N/A")
    cols[2].metric("Win Rate", f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "N/A")
    cols[3].metric("Avg R", f"{stats['avg_r']:+.2f}R" if stats["avg_r"] is not None else "N/A")
    cols[4].metric("Avg Hold", f"{stats['avg_hold']:.1f} bars" if stats["avg_hold"] is not None else "N/A")


def render_history_tab(conn):
    st.subheader("Signal History")
    st.caption(
        "Every logged signal - strict AND baseline, shown or not, taken or not. "
        "Win rate alone is misleading here on purpose: the strict tier runs "
        "roughly a 32% win rate historically - the edge is in payoff (R), not "
        "hit rate, so R is always shown alongside it."
    )

    if st.button("Reconcile Now", help="Fetch fresh price data and fill in outcomes for pending signals"):
        with st.spinner("Reconciling pending signals..."):
            summary = reconcile.reconcile_all_pending(conn)
        st.success(
            f"Resolved {summary['resolved']}, still pending {summary['still_pending']}"
            + (f", could not fetch: {', '.join(summary['fetch_failed'])}" if summary["fetch_failed"] else "")
        )

    filter_cols = st.columns(3)
    default_start = timeutil.today_eastern() - timedelta(days=90)
    start_date = filter_cols[0].date_input("From", value=default_start, key="screener_hist_start")
    end_date = filter_cols[1].date_input("To", value=timeutil.today_eastern(), key="screener_hist_end")
    tier_filter = filter_cols[2].selectbox("Tier", ["All", "strict", "baseline"], key="screener_hist_tier")

    tier = None if tier_filter == "All" else tier_filter
    signals = database.get_signals_for_history(conn, start_date, end_date, tier=tier)

    if not signals:
        st.info("No signals logged in this range yet.")
        return

    st.markdown("**Overall**")
    _render_stat_row("Overall", _summary_stats(signals))
    st.markdown("**Strict vs. Baseline**")
    _render_stat_row("Strict", _summary_stats([s for s in signals if s["tier"] == "strict"]))
    _render_stat_row("Baseline", _summary_stats([s for s in signals if s["tier"] == "baseline"]))

    st.divider()
    st.markdown("**Signals**")

    table = pd.DataFrame(signals)
    # Both filled to a concrete sentinel BEFORE building `editable` - a
    # bare NaN vs NaN comparison in the edit-detection loop below is
    # ALWAYS True in pandas (NaN never equals itself), which would
    # register every untouched blank-note row as "changed" on every
    # single rerun and write-then-rerun forever.
    table["user_action"] = table["user_action"].fillna("—")
    table["user_note"] = table["user_note"].fillna("")
    editable = table[[
        "signal_date", "ticker", "tier", "trigger_price", "stop_price", "triggered",
        "outcome_r", "exit_reason", "user_action", "user_note",
    ]].rename(columns={
        "signal_date": "Date", "ticker": "Ticker", "tier": "Tier",
        "trigger_price": "Trigger", "stop_price": "Stop", "triggered": "Triggered",
        "outcome_r": "R", "exit_reason": "Exit Reason",
        "user_action": "Your Action", "user_note": "Note",
    })

    edited = st.data_editor(
        editable, hide_index=True, width="stretch", key="screener_history_editor",
        disabled=["Date", "Ticker", "Tier", "Trigger", "Stop", "Triggered", "R", "Exit Reason"],
        column_config={
            "Your Action": st.column_config.SelectboxColumn(options=["—", "taken", "skipped", "missed"]),
            "Trigger": st.column_config.NumberColumn(format="$%.2f"),
            "Stop": st.column_config.NumberColumn(format="$%.2f"),
            "R": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    # Keyed off each row's own Date/Ticker values (present but disabled,
    # not off row position) - safe even if data_editor's grid component
    # ever reflects an interactive column sort in the returned frame,
    # which would otherwise silently write an action to the wrong
    # ticker if this relied on position matching signals[i].
    original_by_key = {(r["Date"], r["Ticker"]): r for _, r in editable.iterrows()}
    changed = False
    for _, row in edited.iterrows():
        original = original_by_key.get((row["Date"], row["Ticker"]))
        if original is None:
            continue
        if row["Your Action"] != original["Your Action"] or row["Note"] != original["Note"]:
            action = None if row["Your Action"] == "—" else row["Your Action"]
            database.update_signal_user_action(
                conn, row["Date"], row["Ticker"], action, row["Note"] or None)
            changed = True
    if changed:
        st.rerun()


run_tab, history_tab = st.tabs(["Run", "History"])
with run_tab:
    render_run_tab(conn)
with history_tab:
    render_history_tab(conn)
