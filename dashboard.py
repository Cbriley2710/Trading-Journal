"""
Dashboard
=====================
This is a web page showing your trading stats, metrics, and charts -
reading straight from the hosted database (see database.py), instead
of you having to read the numbers out of Excel yourself.

RUNNING IT LOCALLY (different from the other scripts!):
    python -m streamlit run dashboard.py

That opens a page in your own browser at a local address (like
http://localhost:8501). Once deployed to Streamlit Community Cloud,
the same file becomes the page anyone with the URL and password can
reach - see PASSWORD PROTECTION below.

Streamlit (the library that builds this page) works by re-running this
entire file top-to-bottom every time you change a filter in the
browser - that's why nothing here is wrapped in a function that only
runs once; it's meant to be re-run constantly.

PASSWORD PROTECTION: since this page can be reached over the internet
once deployed, auth.check_password() (shared with every other page in
pages/, so they're all gated the same way) blocks everything else
behind a single shared password stored in
st.secrets["DASHBOARD_PASSWORD"] - never written directly in any file.
st.session_state remembers a correct password for the rest of your
browser session, so it only asks once, not on every filter change.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import auth
import charting
import database
import nav

# Reusing charting.py's colors (rather than picking new ones here) keeps
# this page's charts looking like the same dark, DeepVue-styled charts
# used everywhere else in the app - Trade Analyzer, Shortlist, Logbook.
GOOD_COLOR = charting.GOOD_COLOR
CRITICAL_COLOR = charting.CRITICAL_COLOR
LINE_COLOR = charting.CATEGORICAL_PALETTE[0]  # the single line in the cumulative P/L chart
MUTED_COLOR = charting.MUTED_COLOR  # neutral labels (stat tile captions) and the zero-line on charts
BASELINE_COLOR = charting.MUTED_COLOR

st.set_page_config(page_title="Trading Journal", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Dashboard")

st.title("Trading Journal Dashboard")


def load_trades():
    """Pulls every completed trade out of trading.db as a pandas
    DataFrame (a table you can filter/sort/summarize easily), reusing
    database.get_trades() from Phase 1 - no separate data-loading logic."""
    conn = database.get_connection()
    trades = database.get_trades(conn)
    return pd.DataFrame(trades)


def stat_tile(column, label, value, color=None):
    """Renders one number in a column, with its label above it. If a
    color is given, the number is colored (green for a gain, red for a
    loss) - otherwise it's left the normal text color."""
    style = f"color:{color};" if color else ""
    column.markdown(
        f"""
        <div style="text-align:center;">
            <div style="font-size:0.85rem;color:{MUTED_COLOR};">{label}</div>
            <div style="font-size:1.4rem;font-weight:600;{style}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


trades_df = load_trades()

if trades_df.empty:
    st.info("No trades found yet. Run import_trades.py first to populate trading.db.")
    st.stop()

# --- Sidebar filters ---------------------------------------------------
st.sidebar.header("Filters")

all_symbols = sorted(trades_df["symbol"].unique())
selected_symbols = st.sidebar.multiselect("Symbols", all_symbols, default=all_symbols)

min_date = trades_df["date"].min().date()
max_date = trades_df["date"].max().date()
date_range = st.sidebar.date_input(
    "Exit date range", value=(min_date, max_date), min_value=min_date, max_value=max_date
)
# date_input gives back a single date until the user has picked both
# ends of the range - fall back to the full range until then.
start_date, end_date = date_range if len(date_range) == 2 else (min_date, max_date)

filtered = trades_df[
    trades_df["symbol"].isin(selected_symbols)
    & (trades_df["date"].dt.date >= start_date)
    & (trades_df["date"].dt.date <= end_date)
].sort_values("date")

if filtered.empty:
    st.warning("No trades match the current filters.")
    st.stop()

# --- Stat tiles ---------------------------------------------------------
wins = filtered[filtered["profit_loss"] > 0]
losses = filtered[filtered["profit_loss"] < 0]
total_pl = filtered["profit_loss"].sum()
win_rate = len(wins) / len(filtered) * 100
avg_win = wins["profit_loss"].mean() if not wins.empty else 0
avg_loss = losses["profit_loss"].mean() if not losses.empty else 0
best = filtered.loc[filtered["profit_loss"].idxmax()]
worst = filtered.loc[filtered["profit_loss"].idxmin()]

cols = st.columns(7)
stat_tile(cols[0], "Total Trades", f"{len(filtered)}")
stat_tile(cols[1], "Win Rate", f"{win_rate:.1f}%")
stat_tile(cols[2], "Total P/L", f"${total_pl:,.2f}", GOOD_COLOR if total_pl >= 0 else CRITICAL_COLOR)
stat_tile(cols[3], "Avg Win", f"${avg_win:,.2f}", GOOD_COLOR)
stat_tile(cols[4], "Avg Loss", f"${avg_loss:,.2f}", CRITICAL_COLOR)
stat_tile(cols[5], "Best Trade", f"{best['symbol']} ${best['profit_loss']:,.2f}", GOOD_COLOR)
stat_tile(cols[6], "Worst Trade", f"{worst['symbol']} ${worst['profit_loss']:,.2f}", CRITICAL_COLOR)

st.divider()

# --- Cumulative P/L chart ------------------------------------------------
# Labeled "Cumulative P/L," not "Equity," since we don't have a real
# starting account balance to build a true equity curve from yet.
st.subheader("Cumulative Profit/Loss Over Time")

running = filtered.copy()
running["cumulative_pl"] = running["profit_loss"].cumsum()

cum_chart = go.Figure()
cum_chart.add_hline(y=0, line_color=BASELINE_COLOR, line_width=1)
cum_chart.add_trace(go.Scatter(
    x=running["date"],
    y=running["cumulative_pl"],
    mode="lines",
    line=dict(color=LINE_COLOR, width=2),
    customdata=running[["symbol", "profit_loss"]],
    hovertemplate="%{x|%b %d, %Y}<br>%{customdata[0]}: $%{customdata[1]:,.2f}"
                  "<br>Cumulative: $%{y:,.2f}<extra></extra>",
))
cum_chart.update_layout(
    height=350,
    margin=dict(t=10, b=10),
    xaxis_title=None,
    yaxis_title="Cumulative P/L ($)",
    plot_bgcolor=charting.CHART_BACKGROUND,
    paper_bgcolor=charting.CHART_BACKGROUND,
    font=dict(color=charting.CHART_TEXT_COLOR),
)
cum_chart.update_xaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
cum_chart.update_yaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
st.plotly_chart(cum_chart, theme=None)

# --- P/L by symbol chart -------------------------------------------------
st.subheader("Profit/Loss by Symbol")

by_symbol = filtered.groupby("symbol")["profit_loss"].sum().sort_values(ascending=False)
bar_colors = [GOOD_COLOR if v >= 0 else CRITICAL_COLOR for v in by_symbol.values]

bar_chart = go.Figure()
bar_chart.add_hline(y=0, line_color=BASELINE_COLOR, line_width=1)
bar_chart.add_trace(go.Bar(
    x=by_symbol.index,
    y=by_symbol.values,
    marker_color=bar_colors,
    text=[f"${v:,.0f}" for v in by_symbol.values],
    textposition="outside",
    hovertemplate="%{x}: $%{y:,.2f}<extra></extra>",
))
bar_chart.update_layout(
    height=350,
    margin=dict(t=10, b=10),
    yaxis_title="Total P/L ($)",
    plot_bgcolor=charting.CHART_BACKGROUND,
    paper_bgcolor=charting.CHART_BACKGROUND,
    font=dict(color=charting.CHART_TEXT_COLOR),
)
bar_chart.update_xaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
bar_chart.update_yaxes(gridcolor=charting.GRIDLINE_COLOR, showgrid=True, zeroline=False)
st.plotly_chart(bar_chart, theme=None)

# --- Trade table ----------------------------------------------------------
st.subheader("Trades")

table = filtered.sort_values("date", ascending=False).copy()
table["Result"] = table["profit_loss"].apply(lambda v: "✅ Win" if v > 0 else "❌ Loss" if v < 0 else "Breakeven")
table = table.rename(columns={
    "symbol": "Symbol",
    "entry_date": "Date of Entry",
    "buy_price": "Entry Price",
    "quantity": "# Shares",
    "date": "Date of Exit",
    "sell_price": "Exit Price",
    "profit_loss": "Profit/Loss",
})[["Symbol", "Date of Entry", "Entry Price", "# Shares", "Date of Exit",
    "Exit Price", "Profit/Loss", "Result"]]

st.dataframe(
    table,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Date of Entry": st.column_config.DateColumn(format="M/D/YYYY"),
        "Date of Exit": st.column_config.DateColumn(format="M/D/YYYY"),
        "Entry Price": st.column_config.NumberColumn(format="$%.2f"),
        "Exit Price": st.column_config.NumberColumn(format="$%.2f"),
        "Profit/Loss": st.column_config.NumberColumn(format="$%.2f"),
    },
)
