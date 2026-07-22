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

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import auth
import charting
import database
import nav
import timeutil
from ui import stat_tile

# Reusing charting.py's colors (rather than picking new ones here) keeps
# this page's charts looking like the same dark, DeepVue-styled charts
# used everywhere else in the app - Trade Analyzer, Shortlist, Logbook.
GOOD_COLOR = charting.GOOD_COLOR
CRITICAL_COLOR = charting.CRITICAL_COLOR
LINE_COLOR = charting.CATEGORICAL_PALETTE[0]  # the single line in the equity curve chart
MUTED_COLOR = charting.MUTED_COLOR  # neutral labels (stat tile captions) and the zero-line on charts
BASELINE_COLOR = charting.MUTED_COLOR

st.set_page_config(page_title="Trading Journal", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Dashboard")

st.title("Trading Journal Dashboard")

conn = database.get_connection()

# --- Calculated account value --------------------------------------------
# Rather than needing the actual current account value typed in and kept
# up to date by hand, this is built up from a Jan 1 baseline (set once a
# year - see the Account Settings expander at the bottom of this page)
# plus everything that's happened since: deposits, closed-trade P/L, and
# open positions' unrealized P/L right now. Dollar figures further down
# this page are shown as a % of this calculated value.
jan1_balance = database.get_account_value(conn)
deposits = database.get_deposits(conn)
jan1_date = date(timeutil.today_eastern().year, 1, 1)
deposits_this_year = sum(d["amount"] for d in deposits if d["deposit_date"] >= jan1_date)
realized_pl_this_year = database.get_realized_pl_since(conn, jan1_date)

total_unrealized_pl_now = 0.0
if jan1_balance:
    open_positions_now = database.get_open_positions(conn)
    if open_positions_now:
        with st.spinner("Fetching current prices for account value..."):
            for position in open_positions_now:
                current_price = charting.fetch_latest_price(position["symbol"])
                if current_price is None:
                    continue
                cost_basis = position["avg_price"] * position["quantity"]
                current_value = current_price * position["quantity"]
                is_short = position["direction"] == "SHORT"
                total_unrealized_pl_now += (cost_basis - current_value) if is_short else (current_value - cost_basis)

account_value = (
    jan1_balance + deposits_this_year + realized_pl_this_year + total_unrealized_pl_now
) if jan1_balance else None

st.divider()


def load_trades():
    """Pulls every completed trade out of trading.db as a pandas
    DataFrame (a table you can filter/sort/summarize easily), reusing
    database.get_trades() from Phase 1 - no separate data-loading logic."""
    conn = database.get_connection()
    trades = database.get_trades(conn)
    return pd.DataFrame(trades)


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

# --- Account performance by time period ----------------------------------
# Uses trades_df (every closed trade), not `filtered` - this is meant to
# answer "how has my whole account actually done," not whatever narrower
# slice the sidebar filters happen to be set to. Realized P/L only (no
# unrealized P/L from open positions). Every period's % is against the
# Jan 1 baseline specifically, NOT today's calculated account_value -
# that value already has this year's P/L (and today's unrealized P/L)
# baked into it, so using it as the denominator would inflate as the
# year goes on and systematically understate every period's return.
st.subheader("Account Performance")
if jan1_balance:
    today = pd.Timestamp(timeutil.today_eastern())
    periods = [
        ("7 Days", today - pd.Timedelta(days=7)),
        ("30 Days", today - pd.Timedelta(days=30)),
        ("90 Days", today - pd.Timedelta(days=90)),
        ("YTD", pd.Timestamp(year=today.year, month=1, day=1)),
        ("All-Time", None),
    ]
    period_cols = st.columns(len(periods))
    for col, (label, cutoff) in zip(period_cols, periods):
        period_trades = trades_df if cutoff is None else trades_df[trades_df["date"] >= cutoff]
        period_pl = period_trades["profit_loss"].sum()
        period_pct = period_pl / jan1_balance * 100
        stat_tile(col, label, f"${period_pl:,.2f} ({period_pct:+.1f}%)",
                  GOOD_COLOR if period_pl >= 0 else CRITICAL_COLOR)
else:
    st.info("Set your account value as of Jan 1 (Account Settings below) to see account performance by time period.")

st.divider()

# --- Equity curve ---------------------------------------------------------
# Shown as % gain, not $ - so it lines up with the Account Performance
# tiles above, which use the same convention: every % is against the
# Jan 1 baseline specifically, not a real point-in-time account value
# (we don't have historical account-value snapshots to build a true
# equity curve from - see Account Performance's own comment above for
# why). Each window re-starts its cumulative total at 0% at the start
# of that window, so "1 Year" shows the gain made DURING the last
# year, not the whole account's history compressed into one window.
st.subheader("Equity Curve")

if not jan1_balance:
    st.info(
        "Set your account value as of Jan 1 (Account Settings below) "
        "to see the equity curve as a % gain."
    )
else:
    window_labels = ["1M", "3M", "6M", "1Y", "3Y", "All Time"]
    equity_window = st.radio(
        "Window", window_labels, index=5, horizontal=True, key="equity_window")

    today = pd.Timestamp(timeutil.today_eastern())
    window_cutoffs = {
        "1M": today - pd.DateOffset(months=1),
        "3M": today - pd.DateOffset(months=3),
        "6M": today - pd.DateOffset(months=6),
        "1Y": today - pd.DateOffset(years=1),
        "3Y": today - pd.DateOffset(years=3),
        "All Time": None,
    }
    cutoff = window_cutoffs[equity_window]
    window_trades = filtered if cutoff is None else filtered[filtered["date"] >= cutoff]

    if window_trades.empty:
        st.warning("No trades in this window.")
    else:
        equity = window_trades.copy()
        equity["cumulative_pl"] = equity["profit_loss"].cumsum()

        # Plotting one point per TRADE (rather than per day) is what
        # made this look jagged - a straight diagonal line was drawn
        # connecting whichever two trades happened to close next to
        # each other, even if that was two months apart, instead of
        # showing the account sitting flat in between. Reindexing onto
        # every single day (forward-filling the last known cumulative
        # total between trade-close days) turns that into a proper
        # day-by-day line: flat where nothing closed, stepping only on
        # a day something actually did. `window_start` is the fixed
        # calendar cutoff for a sized window (so "1Y" always spans a
        # full year back, flat at 0% before whatever the first trade
        # in that window happened to be) - only "All Time" falls back
        # to the first trade's own date, since it has no fixed edge.
        window_start = cutoff if cutoff is not None else equity["date"].min()
        daily_index = pd.date_range(start=window_start, end=today, freq="D")
        daily_pl = equity.groupby(equity["date"].dt.normalize())["cumulative_pl"].last()
        daily_pl = daily_pl.reindex(daily_index).ffill().fillna(0)
        daily_pct = daily_pl / jan1_balance * 100

        equity_chart = go.Figure()
        equity_chart.add_hline(y=0, line_color=BASELINE_COLOR, line_width=1)
        equity_chart.add_trace(go.Scatter(
            x=daily_index,
            y=daily_pct,
            mode="lines",
            line=dict(color=LINE_COLOR, width=2),
            hovertemplate="%{x|%b %d, %Y}<br>Cumulative: %{y:+.2f}%<extra></extra>",
        ))
        st.plotly_chart(charting.style_simple_chart(equity_chart, "% Gain"), theme=None)

# --- Holding period vs. return scatter -------------------------------------
# One dot per trade: how many days it was held (x) against how much it
# returned as a % of what was actually put into it (y) - a quick way to
# see whether trades held longer tend to do better or worse, at a
# glance, instead of reading it out of the trade table row by row.
st.subheader("Holding Period vs. Return")

scatter_data = filtered.copy()
scatter_data["holding_days"] = (scatter_data["date"] - scatter_data["entry_date"]).dt.days
# Same entry-price convention as the trade table below: for a SHORT
# trade, the stored buy_price is the cover (exit) and sell_price is
# the short sale (entry) - the opposite pairing from a LONG trade.
is_short = scatter_data["direction"] == "SHORT"
entry_price = scatter_data["buy_price"].where(~is_short, scatter_data["sell_price"])
scatter_data["return_pct"] = scatter_data["profit_loss"] / (entry_price * scatter_data["quantity"]) * 100
scatter_colors = [GOOD_COLOR if v >= 0 else CRITICAL_COLOR for v in scatter_data["profit_loss"]]

scatter_chart = go.Figure()
scatter_chart.add_hline(y=0, line_color=BASELINE_COLOR, line_width=1)
scatter_chart.add_trace(go.Scatter(
    x=scatter_data["holding_days"],
    y=scatter_data["return_pct"],
    mode="markers",
    marker=dict(color=scatter_colors, size=8, opacity=0.8),
    customdata=scatter_data[["symbol", "profit_loss"]],
    hovertemplate="%{customdata[0]}<br>Held %{x} day(s)<br>Return: %{y:+.1f}%"
                  "<br>P/L: $%{customdata[1]:,.2f}<extra></extra>",
))
scatter_fig = charting.style_simple_chart(scatter_chart, "Return (%)")
scatter_fig.update_layout(xaxis_title="Holding Period (Days)")
st.plotly_chart(scatter_fig, theme=None)

# --- P/L by symbol chart -------------------------------------------------
st.subheader("Profit/Loss by Symbol")

by_symbol = filtered.groupby("symbol")["profit_loss"].sum().sort_values(ascending=False)
bar_colors = [GOOD_COLOR if v >= 0 else CRITICAL_COLOR for v in by_symbol.values]

# With an account value saved, each symbol's contribution is shown as a
# % of that account too, not just its raw dollar P/L.
if account_value:
    bar_text = [f"${v:,.0f} ({v / account_value * 100:+.1f}%)" for v in by_symbol.values]
    bar_customdata = by_symbol.values / account_value * 100
    bar_hovertemplate = "%{x}: $%{y:,.2f} (%{customdata:+.1f}% of account)<extra></extra>"
else:
    bar_text = [f"${v:,.0f}" for v in by_symbol.values]
    bar_customdata = None
    bar_hovertemplate = "%{x}: $%{y:,.2f}<extra></extra>"

bar_chart = go.Figure()
bar_chart.add_hline(y=0, line_color=BASELINE_COLOR, line_width=1)
bar_chart.add_trace(go.Bar(
    x=by_symbol.index,
    y=by_symbol.values,
    marker_color=bar_colors,
    text=bar_text,
    textposition="outside",
    customdata=bar_customdata,
    hovertemplate=bar_hovertemplate,
))
st.plotly_chart(charting.style_simple_chart(bar_chart, "Total P/L ($)"), theme=None)

# --- Equity allocation (open positions as % of account) -------------------
st.subheader("Equity Allocation")
if not account_value:
    st.info("Set your account value above to see equity allocation across open positions.")
else:
    open_positions = database.get_open_positions(conn)
    if not open_positions:
        st.info(
            "No open positions right now. A ticker shows up here as soon as "
            "an imported buy hasn't been matched to a sell yet."
        )
    else:
        alloc_rows = []
        with st.spinner("Fetching current prices..."):
            for position in open_positions:
                current_price = charting.fetch_latest_price(position["symbol"])
                if current_price is None:
                    continue
                current_value = current_price * position["quantity"]
                alloc_rows.append({
                    "symbol": position["symbol"],
                    "current_value": current_value,
                    "pct": current_value / account_value * 100,
                })

        if not alloc_rows:
            st.warning("No current price data available for open positions.")
        else:
            alloc_rows.sort(key=lambda r: r["pct"], reverse=True)
            alloc_chart = go.Figure()
            alloc_chart.add_trace(go.Bar(
                x=[r["symbol"] for r in alloc_rows],
                y=[r["pct"] for r in alloc_rows],
                marker_color=charting.CATEGORICAL_PALETTE[0],
                text=[f"{r['pct']:.1f}% (${r['current_value']:,.0f})" for r in alloc_rows],
                textposition="outside",
                hovertemplate="%{x}: %{y:.1f}% of account<extra></extra>",
            ))
            st.plotly_chart(charting.style_simple_chart(alloc_chart, "% of Account"), theme=None)

st.divider()

# --- Trade table ----------------------------------------------------------
st.subheader("Trades")

table = filtered.sort_values("date", ascending=False).copy()
table["Result"] = table["profit_loss"].apply(lambda v: "✅ Win" if v > 0 else "❌ Loss" if v < 0 else "Breakeven")
# For a SHORT trade the stored buy_price is the COVER (the exit event)
# and sell_price is the short sale (the entry event) - the opposite
# pairing from a long trade (see match_trades_lifo in analyze_trades.py).
# Swap them for display so "Entry Price" always means the price the
# trade was opened at, whichever direction it was.
is_short = table["direction"] == "SHORT"
table["Entry Price"] = table["buy_price"].where(~is_short, table["sell_price"])
table["Exit Price"] = table["sell_price"].where(~is_short, table["buy_price"])
table["Direction"] = table["direction"].map({"LONG": "Long", "SHORT": "Short"})
table = table.rename(columns={
    "symbol": "Symbol",
    "entry_date": "Date of Entry",
    "quantity": "# Shares",
    "date": "Date of Exit",
    "profit_loss": "Profit/Loss",
})[["Symbol", "Direction", "Date of Entry", "Entry Price", "# Shares", "Date of Exit",
    "Exit Price", "Profit/Loss", "Result"]]

st.dataframe(
    table,
    width="stretch",
    hide_index=True,
    column_config={
        "Date of Entry": st.column_config.DateColumn(format="M/D/YYYY"),
        "Date of Exit": st.column_config.DateColumn(format="M/D/YYYY"),
        "Entry Price": st.column_config.NumberColumn(format="$%.2f"),
        "Exit Price": st.column_config.NumberColumn(format="$%.2f"),
        "Profit/Loss": st.column_config.NumberColumn(format="$%.2f"),
    },
)

st.divider()

with st.expander("Account Settings"):
    st.caption(
        "Set your account's value at the start of this year once - deposits "
        "and trading P/L build up from there automatically, so this doesn't "
        "need to be kept up to date by hand."
    )
    new_jan1_balance = st.number_input(
        "Account Value as of Jan 1 ($)", min_value=0.0, value=jan1_balance or 0.0,
        step=100.0, format="%.2f", key="jan1_balance_input",
    )
    if st.button("Save Jan 1 Value"):
        database.set_account_value(conn, new_jan1_balance)
        st.success(f"Jan 1 account value saved at ${new_jan1_balance:,.2f}.")
        st.rerun()

    if jan1_balance:
        st.caption(
            f"Calculated current account value: ${account_value:,.2f} "
            f"(${jan1_balance:,.2f} Jan 1 baseline + ${deposits_this_year:,.2f} net "
            f"deposits/withdrawals this year + ${realized_pl_this_year:,.2f} realized P/L "
            f"this year + ${total_unrealized_pl_now:,.2f} unrealized P/L now)"
        )

    st.subheader("Deposits & Withdrawals")
    st.caption("Enter a positive amount for a deposit, or a negative amount for a withdrawal.")
    deposit_cols = st.columns([1, 1, 1])
    deposit_amount = deposit_cols[0].number_input(
        "Amount ($, negative = withdrawal)", step=100.0, format="%.2f", key="deposit_amount_input")
    deposit_date = deposit_cols[1].date_input("Date", value=timeutil.today_eastern(), key="deposit_date_input")
    deposit_cols[2].write("")  # vertical spacer so the button lines up with the inputs above
    if deposit_cols[2].button("Add"):
        if deposit_amount != 0:
            database.add_deposit(conn, deposit_date, deposit_amount)
            action = "Deposit" if deposit_amount > 0 else "Withdrawal"
            st.success(f"{action} of ${abs(deposit_amount):,.2f} on {deposit_date:%m/%d/%Y} added.")
            st.rerun()
        else:
            st.warning("Enter an amount other than $0.")

    if deposits:
        for d in sorted(deposits, key=lambda d: d["deposit_date"], reverse=True):
            row_cols = st.columns([1, 1, 1])
            row_cols[0].write(f"{d['deposit_date']:%m/%d/%Y}")
            amount_label = f"${d['amount']:,.2f}" if d["amount"] >= 0 else f"-${abs(d['amount']):,.2f} (withdrawal)"
            row_cols[1].write(amount_label)
            if row_cols[2].button("Delete", key=f"delete_deposit_{d['id']}"):
                database.delete_deposit(conn, d["id"])
                st.rerun()
    else:
        st.caption("No deposits or withdrawals recorded yet.")
