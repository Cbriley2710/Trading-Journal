"""
Institutional Holdings
=====================
Two ways to use this page:
  Look Up a Ticker   Type in a ticker, see how many of a curated list of
                      well-known hedge funds hold it - "14 of 20 funds
                      hold this" - plus a per-fund checklist showing
                      shares, dollar value, % of that fund's own
                      portfolio, and whether it was added/trimmed/closed
                      last quarter.
  Recent Moves       The other direction - browse every position that
                      actually changed last quarter across all 20
                      funds, without needing to already have a ticker
                      in mind. See get_recent_moves()'s own docstring
                      for why a fund only shows up here once it has TWO
                      quarters of data to compare.

WHERE THE DATA COMES FROM AND ITS REAL LIMITS: every fund here is
required to file a public SEC Form 13F every quarter, listing its long
US stock positions AS OF the quarter-end date - filed up to 45 days
late. That means:
  - This is always a SNAPSHOT, at least 6 weeks stale, never live.
  - There's no purchase date - "added shares" only ever means "held
    more at this quarter-end than the quarter-end before."
  - Short positions and non-US securities don't show up here at all.
  - Only each fund's ~300 largest positions by dollar value are tracked
    (a systematic/quant fund can report tens of thousands of tiny
    ones) - "% of Portfolio" still comes out accurate even for a
    position outside that top 300, since it's this fund's true total
    that was cut, not this position's fund_total_value_usd.
See institutional_holdings.py's own docstring for the full picture,
including why a stock is matched by CUSIP (its SEC filing identifier),
not ticker, and how that gets resolved.

The list of 20 funds itself lives in the hedge_funds database table
(seeded once by database.seed_hedge_funds() below) and can be edited
any time from the "Manage Tracked Funds" section at the bottom of this
page - no code changes needed to add or remove a fund.
"""
import streamlit as st

import auth
import charting
import database
import institutional_holdings
import nav

st.set_page_config(page_title="Institutional Holdings", page_icon="🏦", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("institutional_holdings")

st.title("Institutional Holdings")
st.caption(
    "Based on quarterly SEC Form 13F filings - a snapshot as of the last "
    "quarter-end, filed up to 45 days later. Not live, no short positions, "
    "no exact purchase dates. Only each fund's ~300 largest positions by "
    "dollar value are tracked (some funds report thousands of tiny ones) - "
    "informational, not investment advice."
)

conn = database.get_connection()
database.seed_hedge_funds(conn)  # only actually inserts anything the very first time this page ever runs


# --- Shared formatting helpers ---------------------------------------------

_CHANGE_COLORS = {
    "New": charting.GOOD_COLOR,
    "Increased": charting.GOOD_COLOR,
    "Decreased": charting.CRITICAL_COLOR,
    "Closed": charting.CRITICAL_COLOR,
    "Unchanged": charting.MUTED_COLOR,
}


def _format_change(change, change_pct):
    """"Increased +42%", "New" (no % - can't quantify a rise from zero),
    "Closed -100%", colored the same way GOOD_COLOR/CRITICAL_COLOR color
    wins and losses everywhere else in this app."""
    if not change:
        return "—"
    text = change if change_pct is None else f"{change} {change_pct:+.0f}%"
    color = _CHANGE_COLORS.get(change, charting.MUTED_COLOR)
    return f"<span style='color:{color};font-weight:600;'>{text}</span>"


def _format_as_of(quarter_end, stale):
    if not quarter_end:
        return "No data yet"
    text = quarter_end.strftime("%b %Y")
    if stale:
        # This fund hasn't filed its most recent quarter yet (or filed
        # late) - flagged so its row isn't mistaken for being as current
        # as every other fund's, which could otherwise read as "doesn't
        # hold it this quarter" when really it's just "hasn't told us
        # about this quarter at all yet."
        return f"<span style='color:{charting.MUTED_COLOR};'>{text} ⚠️</span>"
    return text


# --- Look Up a Ticker -------------------------------------------------------

def _render_lookup_tab():
    ticker = st.text_input("Ticker", placeholder="e.g. AAPL").strip().upper()

    if not ticker:
        st.info("Type a ticker above to see which tracked funds hold it.")
        return

    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, ticker)
    held_count = sum(1 for r in snapshot if r["held"])
    total_count = len(snapshot)

    if total_count == 0:
        st.info("No funds are being tracked yet - add some below under Manage Tracked Funds.")
        return

    stat_col, _ = st.columns([1, 3])
    stat_color = charting.GOOD_COLOR if held_count > 0 else charting.MUTED_COLOR
    with stat_col:
        st.markdown(
            f"""
            <div style="text-align:center;">
                <div style="font-size:0.85rem;color:{charting.MUTED_COLOR};">Funds holding {ticker}</div>
                <div style="font-size:2rem;font-weight:700;color:{stat_color};">{held_count} of {total_count}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Held funds first (biggest position first), then funds that don't hold it.
    held = sorted([r for r in snapshot if r["held"]], key=lambda r: r["value_usd"], reverse=True)
    not_held = sorted([r for r in snapshot if not r["held"]], key=lambda r: r["fund"])

    widths = [3, 1, 2, 2, 2, 2, 2]
    header = st.columns(widths)
    for col, label in zip(header, ["Fund", "Holds It?", "Shares", "Value", "% of Portfolio", "Change", "As Of"]):
        col.markdown(f"**{label}**")

    for row in held + not_held:
        cols = st.columns(widths)
        cols[0].write(row["fund"])
        cols[1].write("✅" if row["held"] else "—")
        cols[2].write(f"{row['shares']:,}" if row["shares"] is not None else "—")
        cols[3].write(f"${row['value_usd']:,}" if row["value_usd"] is not None else "—")
        cols[4].write(f"{row['portfolio_pct']:.1f}%" if row["portfolio_pct"] is not None else "—")
        cols[5].markdown(_format_change(row["change"], row["change_pct"]), unsafe_allow_html=True)
        cols[6].markdown(_format_as_of(row["quarter_end"], row["stale"]), unsafe_allow_html=True)


# --- Recent Moves ------------------------------------------------------------

_MOVE_TYPES = ["New", "Increased", "Decreased", "Closed"]


def _render_recent_moves_tab():
    st.caption(
        "Every position that changed between a fund's two most recently "
        "fetched quarters - browse for something worth looking into, "
        "instead of already knowing a ticker to check. A fund only shows "
        "up here once TWO quarters of its data have been fetched, so "
        "right after adding a new fund (or right after this app's very "
        "first-ever refresh) this list will be empty until next quarter."
    )

    selected_types = st.multiselect("Show", _MOVE_TYPES, default=["New", "Increased"])
    moves = institutional_holdings.get_recent_moves(conn)
    filtered = [m for m in moves if m["change"] in selected_types]

    if not moves:
        st.info("No quarter-over-quarter data yet - check back after the next refresh.")
        return
    if not filtered:
        st.info("No moves of the selected type(s) in the latest data.")
        return

    # Biggest conviction moves first - % of that fund's OWN portfolio is a
    # fairer ranking than raw dollar value, since it isn't skewed by fund size.
    filtered.sort(key=lambda m: m["portfolio_pct"] or 0, reverse=True)

    widths = [2, 1, 3, 2, 2, 2, 2, 2]
    header = st.columns(widths)
    for col, label in zip(header, ["Fund", "Ticker", "Company", "Shares", "Value", "% of Portfolio", "Change", "As Of"]):
        col.markdown(f"**{label}**")

    for m in filtered:
        cols = st.columns(widths)
        cols[0].write(m["fund"])
        cols[1].write(m["ticker"] or "—")
        cols[2].write(m["issuer_name"])
        cols[3].write(f"{m['shares']:,}" if m["shares"] else "—")
        cols[4].write(f"${m['value_usd']:,}" if m["value_usd"] else "—")
        cols[5].write(f"{m['portfolio_pct']:.1f}%" if m["portfolio_pct"] is not None else "—")
        cols[6].markdown(_format_change(m["change"], m["change_pct"]), unsafe_allow_html=True)
        cols[7].markdown(_format_as_of(m["quarter_end"], m["stale"]), unsafe_allow_html=True)


lookup_tab, moves_tab = st.tabs(["Look Up a Ticker", "Recent Moves"])
with lookup_tab:
    _render_lookup_tab()
with moves_tab:
    _render_recent_moves_tab()


# --- Refresh ----------------------------------------------------------------

st.divider()
if st.button("Refresh Holdings Data", help="Checks SEC EDGAR for each tracked fund's latest 13F filing"):
    with st.spinner("Checking SEC EDGAR for each fund's latest 13F filing..."):
        results = institutional_holdings.refresh_all_funds(conn)
    updated = {name: status for name, status in results.items() if status.startswith("updated")}
    errors = {name: status for name, status in results.items() if status.startswith("error") or "no 13F" in status}
    if updated:
        st.success("Updated: " + ", ".join(f"{name} ({status})" for name, status in updated.items()))
    if errors:
        st.warning("Could not refresh: " + ", ".join(f"{name} - {status}" for name, status in errors.items()))
    if not updated and not errors:
        st.info("Every tracked fund is already up to date with SEC's latest filings.")


# --- Manage Tracked Funds ----------------------------------------------------

with st.expander("Manage Tracked Funds"):
    st.caption(
        "This is the list of 20 well-known funds tracked above. There's no "
        "free, audited ranking of hedge funds by actual return - this list "
        "is the standard set of large, widely-recognized managers, and you "
        "can freely swap any of them out below."
    )

    funds = database.get_hedge_funds(conn)
    for fund in funds:
        fund_col, remove_col = st.columns([4, 1])
        fund_col.write(f"{fund['display_name']}  (CIK {fund['sec_cik']})")
        if remove_col.button("Remove", key=f"remove_fund_{fund['id']}"):
            database.deactivate_hedge_fund(conn, fund["id"])
            st.rerun()

    st.markdown("**Add a fund**")
    search_name = st.text_input("Fund name", key="new_fund_search", placeholder="e.g. Millennium Management")
    if st.button("Search SEC EDGAR", key="search_fund_button") and search_name:
        with st.spinner("Searching SEC EDGAR..."):
            st.session_state["fund_candidates"] = institutional_holdings.find_cik_candidates(search_name)
        # A previous search's radio selection won't necessarily be one of
        # THESE candidates - clear it so the radio widget below doesn't
        # error trying to restore a choice that no longer exists.
        st.session_state.pop("fund_candidate_choice", None)

    candidates = st.session_state.get("fund_candidates", [])
    if candidates:
        options = {f"{c['display_name']} (CIK {c['cik']})": c for c in candidates}
        chosen_label = st.radio("Matches found on SEC EDGAR:", list(options.keys()), key="fund_candidate_choice")
        if st.button("Add This Fund", key="add_fund_button"):
            chosen = options[chosen_label]
            database.add_hedge_fund(conn, chosen["display_name"], chosen["cik"])
            del st.session_state["fund_candidates"]
            st.success(f"Added {chosen['display_name']} - click Refresh Holdings Data above to fetch its filings.")
            st.rerun()
    elif search_name:
        st.caption("No matches yet - click \"Search SEC EDGAR\" above.")
