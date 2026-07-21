"""
Open Positions
=====================
Two things this page answers that no other page does: how much money is
actually tied up in open positions right now, and how much of that money
is genuinely at risk ("heat") if every stop-loss got hit today. It also
shows a trend chart of your last 10 trades (open or closed) so you can
spot a hot or cold streak early.

"Heat" here means the dollar distance from today's price down to your
stop-loss, times your shares - i.e. what you'd actually lose from here if
the position got stopped out right now. That number only exists for a
position once you've set a stop-loss for it in the Positions & Stop-Loss
table below - there's nowhere else in this app that stop price is
tracked (see database.get_stop_loss()/set_stop_loss()).

That same table also has an MA Mode column (O/M/A toggle buttons per
position) - an opt-in way to track a trend-following exit against a
moving average, and optionally have this app trail your stop-loss up
to it automatically (see ma_strategy.py). The MA period and every
other threshold it uses are global, set on the Settings page - only
the mode is chosen per ticker here.
"""

from datetime import date

import streamlit as st
import plotly.graph_objects as go

import auth
import charting
import database
import ma_strategy
import nav
import timeutil
from ui import stat_tile

st.set_page_config(page_title="Open Positions", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Open Positions")

st.title("Open Positions")


def position_label(position):
    """A position's symbol, tagged "(Short)" when it's a short position -
    same convention as the Shortlist page's picker, so a short position
    is never mistaken for a long one on these charts."""
    return f"{position['symbol']} (Short)" if position["direction"] == "SHORT" else position["symbol"]


conn = database.get_connection()
positions = database.get_open_positions(conn)
stops = database.get_all_stop_losses(conn)
ma_defaults = database.get_strategy_settings(conn)

# Same calculated-account-value formula as the Dashboard page (see its
# Account Settings expander): a Jan 1 baseline plus this year's deposits
# and realized P/L, plus open positions' unrealized P/L below once it's
# known - so the Cash bar and % of account figures further down reflect
# an actually-maintained account value instead of a stale typed-in one.
jan1_balance = database.get_account_value(conn)
deposits = database.get_deposits(conn)
jan1_date = date(timeutil.today_eastern().year, 1, 1)
deposits_this_year = sum(d["amount"] for d in deposits if d["deposit_date"] >= jan1_date)
realized_pl_this_year = database.get_realized_pl_since(conn, jan1_date)
account_value = (jan1_balance + deposits_this_year + realized_pl_this_year) if jan1_balance else None

# --- Gather live data for every open position ---------------------------
enriched = []
if positions:
    with st.spinner("Fetching current prices..."):
        for position in positions:
            is_short = position["direction"] == "SHORT"
            current_price = charting.fetch_latest_price(position["symbol"])
            cost_basis = position["avg_price"] * position["quantity"]

            current_value = unrealized_pl = None
            if current_price is not None:
                current_value = current_price * position["quantity"]
                # A short profits when price FALLS below your average
                # entry - the opposite direction from a long.
                unrealized_pl = (cost_basis - current_value) if is_short else (current_value - cost_basis)

            stop_loss = stops.get(position["symbol"])

            # MA Stop Rule (see ma_strategy.py) - only computed for a
            # position that's actually opted in (mode != "off"), so a
            # position not using this feature doesn't cost an extra
            # price-history fetch on every page load.
            ma_settings = database.get_position_ma_settings(conn, position["symbol"], ma_defaults)
            ma_signal = None
            if ma_settings["mode"] != "off":
                ma_signal = ma_strategy.compute_signal(
                    position["symbol"], position["avg_price"], is_short, ma_settings)
                if ma_settings["mode"] == "auto" and ma_signal["unlocked"] and ma_signal["ma_value"] is not None:
                    stop_loss = ma_strategy.apply_auto_stop(
                        conn, position["symbol"], is_short, ma_signal["ma_value"], stop_loss)
                    stops[position["symbol"]] = stop_loss

            heat_dollars = heat_pct = None
            if stop_loss is not None and current_price is not None:
                # Heat is the dollar distance from today's price to the
                # stop - for a long that's downside (current - stop);
                # for a short the risk is the opposite direction, price
                # rising up through the stop (stop - current).
                if is_short:
                    heat_dollars = max(0.0, (stop_loss - current_price) * position["quantity"])
                else:
                    heat_dollars = max(0.0, (current_price - stop_loss) * position["quantity"])
                if current_value:
                    heat_pct = heat_dollars / current_value * 100

            enriched.append({
                **position,
                "current_price": current_price,
                "cost_basis": cost_basis,
                "current_value": current_value,
                "unrealized_pl": unrealized_pl,
                "stop_loss": stop_loss,
                "heat_dollars": heat_dollars,
                "heat_pct": heat_pct,
                "ma_settings": ma_settings,
                "ma_signal": ma_signal,
            })

# --- Positions & stop-loss --------------------------------------------------
# The one place stop-loss is set or moved - a table instead of a
# per-ticker input buried in a chart page, so trailing every open
# position's stop as the day goes is one scroll, not five page visits.
# MA Mode (see ma_strategy.py) is one column of this same table: three
# small O/M/A toggle buttons - "Off" does nothing; "Manual" shows a
# read-only signal in the last column; "Auto" also trails Stop Loss up
# to the moving average once it's cleared cost basis by enough, never
# loosening a tighter stop already set by hand. The MA period and every
# threshold this uses are global, set on the Settings page.
#
# Built as a plain grid of st.columns rows, not st.data_editor - a
# data_editor column can't hold three buttons in one cell, and a
# SelectboxColumn version of this turned out to be unreliable (a
# picked value could revert before the script ever saw it, likely the
# grid's own reconciliation fighting with a row that has several
# live-recomputed columns). Individual widgets with their own stable
# keys (buttons, number_input) don't have that problem.
if positions:
    st.header("Positions & Stop-Loss")
    st.caption(
        "Edit Stop Loss directly - 0 (or blank) means no stop set. MA Mode: "
        "click O/M/A to switch (highlighted = active); Manual/Auto show a "
        "live signal in the last column."
    )

    COLUMN_WIDTHS = [1.3, 1.0, 0.7, 0.9, 0.9, 1.0, 1.0, 0.35, 0.35, 0.35, 2.2]
    header_cols = st.columns(COLUMN_WIDTHS)
    for col, label in zip(header_cols, [
        "Ticker", "Entry Date", "Shares", "Avg Price", "Current Price",
        "Unrealized P/L", "Stop Loss", "O", "M", "A", "MA Signal",
    ]):
        col.markdown(f"**{label}**")

    def render_signal(col, e):
        """Colored badges laid out side by side (nested sub-columns
        within this one column) instead of stacked - a glance at the
        color tells you severity before you even read the text."""
        sig = e["ma_signal"]
        if sig["ma_value"] is None:
            col.caption("No price data")
            return

        ma_col, trend_col, distance_col = col.columns([1, 1.2, 1.3])
        ma_col.caption(f"MA ${sig['ma_value']:,.2f}")

        threshold = e["ma_settings"]["closes_threshold"]
        if sig["sell_signal"]:
            trend_col.badge(f"{sig['signal_closes']}/{threshold} vs trend", icon="\U0001F514", color="red")
        elif sig["signal_closes"] > 0:
            trend_col.badge(f"{sig['signal_closes']}/{threshold} vs trend", color="yellow")
        else:
            trend_col.badge("On trend", color="green")

        if sig["distance_pct"] is not None:
            if sig["extended"]:
                distance_col.badge(f"{sig['distance_pct']:.1f}% from MA", icon="\U0001F680", color="orange")
            elif sig["approaching"]:
                distance_col.badge(f"{sig['distance_pct']:.1f}% from MA", icon="⚠️", color="yellow")
            else:
                distance_col.badge(f"{sig['distance_pct']:.1f}% from MA", color="gray")

    for e in enriched:
        symbol = e["symbol"]
        (ticker_col, entry_col, shares_col, avg_col, price_col,
         pl_col, stop_col, o_col, m_col, a_col, signal_col) = st.columns(COLUMN_WIDTHS)

        ticker_col.write(position_label(e))
        entry_col.write(e["entry_date"].strftime("%m/%d/%Y"))
        shares_col.write(f"{e['quantity']:,.0f}")
        avg_col.write(f"${e['avg_price']:,.2f}")
        price_col.write(f"${e['current_price']:,.2f}" if e["current_price"] is not None else "N/A")
        pl_col.write(f"${e['unrealized_pl']:,.2f}" if e["unrealized_pl"] is not None else "N/A")

        new_stop = stop_col.number_input(
            "Stop Loss", min_value=0.0, step=0.01, format="%.2f",
            value=e["stop_loss"] or 0.0, key=f"stop_loss_{symbol}", label_visibility="collapsed",
        )
        old_stop = round(stops.get(symbol) or 0.0, 2)
        if round(new_stop, 2) != old_stop:
            if new_stop > 0:
                database.set_stop_loss(conn, symbol, round(new_stop, 2))
            else:
                database.delete_stop_loss(conn, symbol)
            st.rerun()

        current_mode = e["ma_settings"]["mode"]
        for mode_col, mode_value, mode_letter in ((o_col, "off", "O"), (m_col, "manual", "M"), (a_col, "auto", "A")):
            is_active = current_mode == mode_value
            if mode_col.button(
                mode_letter, key=f"ma_{mode_value}_{symbol}", width="stretch",
                disabled=is_active, type="primary" if is_active else "secondary",
            ):
                # Period/closes/unlock%/approach%/extended% stay None
                # here on purpose - these buttons only ever set Mode,
                # so every position follows the Settings page's global
                # numbers rather than a per-ticker override (see
                # database.get_position_ma_settings()).
                database.save_position_ma_settings(conn, symbol, mode_value, None, None, None, None, None)
                st.rerun()

        if e["ma_signal"] is not None:
            render_signal(signal_col, e)
        else:
            signal_col.write("")

    st.divider()

# --- Equity & heat ---------------------------------------------------------
st.header("Equity & Heat")
st.caption(
    "\"Heat\" is how much you'd lose, from today's price, if every open "
    "position hit its stop-loss right now. Set a stop for each position in "
    "the Positions & Stop-Loss table above to see it here."
)

if not positions:
    st.info(
        "No open positions right now. A ticker shows up here as soon as "
        "an imported buy hasn't been matched to a sell yet."
    )
else:
    priced = [e for e in enriched if e["current_value"] is not None]
    unpriced_symbols = [e["symbol"] for e in enriched if e["current_value"] is None]
    if unpriced_symbols:
        st.warning(
            f"No current price available for: {', '.join(unpriced_symbols)} - "
            "excluded from every total below so the tiles stay consistent "
            "with each other."
        )

    # All three dollar tiles are computed over the same `priced` set -
    # mixing all positions into one tile but only priced ones into the
    # others would make the tiles quietly describe different things.
    total_cost_basis = sum(e["cost_basis"] for e in priced)
    total_current_value = sum(e["current_value"] for e in priced)
    total_unrealized_pl = sum(e["unrealized_pl"] for e in priced)
    unrealized_color = charting.GOOD_COLOR if total_unrealized_pl >= 0 else charting.CRITICAL_COLOR
    if jan1_balance:
        account_value = jan1_balance + deposits_this_year + realized_pl_this_year + total_unrealized_pl

    heat_positions = [e for e in priced if e["heat_dollars"] is not None]
    total_heat_dollars = sum(e["heat_dollars"] for e in heat_positions)
    portfolio_heat_pct = (total_heat_dollars / total_current_value * 100) if total_current_value else None

    cols = st.columns(5)
    stat_tile(cols[0], "Equity Invested", f"${total_cost_basis:,.2f}")
    stat_tile(cols[1], "Current Value", f"${total_current_value:,.2f}")
    stat_tile(cols[2], "Unrealized P/L", f"${total_unrealized_pl:,.2f}", unrealized_color)
    stat_tile(cols[3], "Portfolio Heat", f"${total_heat_dollars:,.2f}")
    stat_tile(cols[4], "Portfolio Heat %",
              f"{portfolio_heat_pct:.1f}%" if portfolio_heat_pct is not None else "N/A")

    st.divider()

    # --- Equity by position ---------------------------------------------
    st.subheader("Equity by Position")
    if not account_value:
        st.caption(
            "Set your account value on the Dashboard page to also see each "
            "bar's % of account and a Cash bar for what's not invested."
        )
    if priced:
        equity_rows = []
        for e in priced:
            pct = (e["current_value"] / account_value * 100) if account_value else None
            text = f"${e['current_value']:,.0f} ({pct:.1f}%)" if pct is not None else f"${e['current_value']:,.0f}"
            equity_rows.append({
                "label": position_label(e),
                "value": e["current_value"],
                "text": text,
                "detail": f"Cost Basis: ${e['cost_basis']:,.2f}",
                "color": charting.CATEGORICAL_PALETTE[0],
            })

        if account_value:
            cash_amount = account_value - total_current_value
            cash_pct = cash_amount / account_value * 100
            equity_rows.append({
                "label": "Cash",
                "value": cash_amount,
                "text": f"${cash_amount:,.0f} ({cash_pct:.1f}%)",
                "detail": "Uninvested cash",
                "color": charting.MUTED_COLOR,
            })

        equity_rows.sort(key=lambda r: r["value"], reverse=True)

        equity_chart = go.Figure()
        equity_chart.add_trace(go.Bar(
            x=[r["value"] for r in equity_rows],
            y=[r["label"] for r in equity_rows],
            orientation="h",
            marker_color=[r["color"] for r in equity_rows],
            customdata=[r["detail"] for r in equity_rows],
            text=[r["text"] for r in equity_rows],
            textposition="outside",
            hovertemplate="%{y}<br>Value: $%{x:,.2f}<br>%{customdata}<extra></extra>",
        ))
        equity_chart.update_yaxes(autorange="reversed")
        st.plotly_chart(charting.style_simple_chart(equity_chart, "Current Value ($)", horizontal=True), theme=None)
    else:
        st.info("No priced positions to chart yet.")

    # --- Heat by position --------------------------------------------------
    st.subheader("Open Heat by Position")
    no_stop_symbols = [e["symbol"] for e in priced if e["stop_loss"] is None]
    if no_stop_symbols:
        st.caption(
            f"No stop set for: {', '.join(no_stop_symbols)} - set one in the "
            "table above to include it here."
        )
    if heat_positions:
        heat_rows = []
        for e in heat_positions:
            acct_pct = (e["heat_dollars"] / account_value * 100) if account_value else None
            text = f"${e['heat_dollars']:,.0f} ({acct_pct:.1f}% of acct)" if acct_pct is not None else f"${e['heat_dollars']:,.0f}"
            detail = f"{e['heat_pct']:.1f}% of position value" if e["heat_pct"] is not None else ""
            heat_rows.append({"label": position_label(e), "value": e["heat_dollars"], "text": text, "detail": detail})

        heat_rows.sort(key=lambda r: r["value"], reverse=True)

        heat_chart = go.Figure()
        heat_chart.add_trace(go.Bar(
            x=[r["value"] for r in heat_rows],
            y=[r["label"] for r in heat_rows],
            orientation="h",
            marker_color=charting.CRITICAL_COLOR,
            customdata=[r["detail"] for r in heat_rows],
            text=[r["text"] for r in heat_rows],
            textposition="outside",
            hovertemplate="%{y}<br>Heat: $%{x:,.2f}<br>%{customdata}<extra></extra>",
        ))
        heat_chart.update_yaxes(autorange="reversed")
        st.plotly_chart(charting.style_simple_chart(heat_chart, "Heat ($)", horizontal=True), theme=None)
    else:
        st.info("No positions with a stop-loss set yet.")

st.divider()

# --- Last 10 trades trend -------------------------------------------------
st.header("Last 10 Trades")
st.caption("Your most recent trades by entry date, open or closed, to spot a short-term trend.")

trend_rows = []
for trade in database.get_trades(conn):
    trend_rows.append({
        "symbol": trade["symbol"], "entry_date": trade["entry_date"],
        "pl": trade["profit_loss"], "is_open": False, "direction": trade["direction"],
    })
for e in enriched:
    if e["unrealized_pl"] is not None:
        trend_rows.append({
            "symbol": e["symbol"], "entry_date": e["entry_date"],
            "pl": e["unrealized_pl"], "is_open": True, "direction": e["direction"],
        })

if not trend_rows:
    st.info("No trades yet.")
else:
    trend_rows.sort(key=lambda r: r["entry_date"])
    last10 = trend_rows[-10:]

    trend_chart = go.Figure()
    trend_chart.add_hline(y=0, line_color=charting.MUTED_COLOR, line_width=1)
    trend_chart.add_trace(go.Bar(
        x=[f"{r['symbol']}{' (S)' if r['direction'] == 'SHORT' else ''} {r['entry_date']:%m/%d}" for r in last10],
        y=[r["pl"] for r in last10],
        marker=dict(
            color=[charting.GOOD_COLOR if r["pl"] >= 0 else charting.CRITICAL_COLOR for r in last10],
            opacity=[0.55 if r["is_open"] else 1.0 for r in last10],
        ),
        customdata=[["Open (unrealized)" if r["is_open"] else "Closed"] for r in last10],
        text=[f"${r['pl']:,.0f}" for r in last10],
        textposition="outside",
        hovertemplate="%{x}: $%{y:,.2f}<br>%{customdata[0]}<extra></extra>",
    ))
    st.plotly_chart(charting.style_simple_chart(trend_chart, "P/L ($)"), theme=None)
