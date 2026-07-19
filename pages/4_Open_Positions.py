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
position once you've saved a stop-loss for it on the Shortlist page (see
render_open_positions_section() there) - there's nowhere else in this
app that stop price is tracked.
"""

import streamlit as st
import plotly.graph_objects as go

import auth
import charting
import database
import nav

st.set_page_config(page_title="Open Positions", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Open Positions")

st.title("Open Positions")


def stat_tile(column, label, value, color=None):
    """Renders one number in a column, with its label above it - same
    small helper every other page defines locally rather than sharing."""
    style = f"color:{color};" if color else ""
    column.markdown(
        f"""
        <div style="text-align:center;">
            <div style="font-size:0.85rem;color:{charting.MUTED_COLOR};">{label}</div>
            <div style="font-size:1.4rem;font-weight:600;{style}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def position_label(position):
    """A position's symbol, tagged "(Short)" when it's a short position -
    same convention as the Shortlist page's picker, so a short position
    is never mistaken for a long one on these charts."""
    return f"{position['symbol']} (Short)" if position["direction"] == "SHORT" else position["symbol"]


def style_bar_chart(fig, yaxis_title):
    """The shared dark DeepVue look used by every chart in this app,
    applied here to plain go.Bar figures (built directly, not through
    charting.build_figure(), since these are simple single-series bar
    charts rather than price charts)."""
    fig.update_layout(
        height=350,
        margin=dict(t=10, b=45),
        yaxis_title=yaxis_title,
        plot_bgcolor=charting.CHART_BACKGROUND,
        paper_bgcolor=charting.CHART_BACKGROUND,
        font=dict(color=charting.CHART_TEXT_COLOR),
    )
    fig.update_xaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
    return fig


conn = database.get_connection()
positions = database.get_open_positions(conn)
stops = database.get_all_stop_losses(conn)

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
            })

# --- Equity & heat ---------------------------------------------------------
st.header("Equity & Heat")
st.caption(
    "\"Heat\" is how much you'd lose, from today's price, if every open "
    "position hit its stop-loss right now. Set a stop for each position on "
    "the Shortlist page to see it here."
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
        st.warning(f"No current price available for: {', '.join(unpriced_symbols)}.")

    total_cost_basis = sum(e["cost_basis"] for e in enriched)
    total_current_value = sum(e["current_value"] for e in priced)
    total_unrealized_pl = sum(e["unrealized_pl"] for e in priced)
    unrealized_color = charting.GOOD_COLOR if total_unrealized_pl >= 0 else charting.CRITICAL_COLOR

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
    if priced:
        by_value = sorted(priced, key=lambda e: e["current_value"], reverse=True)
        equity_chart = go.Figure()
        equity_chart.add_trace(go.Bar(
            x=[position_label(e) for e in by_value],
            y=[e["current_value"] for e in by_value],
            marker_color=charting.CATEGORICAL_PALETTE[0],
            customdata=[e["cost_basis"] for e in by_value],
            text=[f"${e['current_value']:,.0f}" for e in by_value],
            textposition="outside",
            hovertemplate="%{x}<br>Current Value: $%{y:,.2f}<br>Cost Basis: $%{customdata:,.2f}<extra></extra>",
        ))
        st.plotly_chart(style_bar_chart(equity_chart, "Current Value ($)"), theme=None)
    else:
        st.info("No priced positions to chart yet.")

    # --- Heat by position --------------------------------------------------
    st.subheader("Open Heat by Position")
    no_stop_symbols = [e["symbol"] for e in priced if e["stop_loss"] is None]
    if no_stop_symbols:
        st.caption(
            f"No stop set for: {', '.join(no_stop_symbols)} - set one on the "
            "Shortlist page to include it here."
        )
    if heat_positions:
        by_heat = sorted(heat_positions, key=lambda e: e["heat_dollars"], reverse=True)
        heat_chart = go.Figure()
        heat_chart.add_trace(go.Bar(
            x=[position_label(e) for e in by_heat],
            y=[e["heat_dollars"] for e in by_heat],
            marker_color=charting.CRITICAL_COLOR,
            customdata=[e["heat_pct"] for e in by_heat],
            text=[f"${e['heat_dollars']:,.0f}" for e in by_heat],
            textposition="outside",
            hovertemplate="%{x}<br>Heat: $%{y:,.2f}<br>%{customdata:.1f}% of position<extra></extra>",
        ))
        st.plotly_chart(style_bar_chart(heat_chart, "Heat ($)"), theme=None)
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
    st.plotly_chart(style_bar_chart(trend_chart, "P/L ($)"), theme=None)
