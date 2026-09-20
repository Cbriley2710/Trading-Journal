"""
Institutional Holdings
=====================
Type in a ticker, see how many of a curated list of well-known hedge
funds hold it - "14 of 20 funds hold this" - plus a per-fund
checklist showing shares, dollar value, and whether each fund added,
trimmed, or closed the position last quarter.

WHERE THE DATA COMES FROM AND ITS REAL LIMITS: every fund here is
required to file a public SEC Form 13F every quarter, listing its long
US stock positions AS OF the quarter-end date - filed up to 45 days
late. That means:
  - This is always a SNAPSHOT, at least 6 weeks stale, never live.
  - There's no purchase date - "added shares" only ever means "held
    more at this quarter-end than the quarter-end before."
  - Short positions and non-US securities don't show up here at all.
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


# --- Change badge styling -------------------------------------------------

_CHANGE_COLORS = {
    "New": charting.GOOD_COLOR,
    "Increased": charting.GOOD_COLOR,
    "Decreased": charting.CRITICAL_COLOR,
    "Closed": charting.CRITICAL_COLOR,
    "Unchanged": charting.MUTED_COLOR,
}


def _render_snapshot_table(snapshot):
    """Held funds first (largest position first), then funds that don't
    hold it - each as one plain row rather than a dataframe, so the
    Change column can be colored the same way GOOD_COLOR/CRITICAL_COLOR
    color wins and losses everywhere else in this app."""
    held = sorted([r for r in snapshot if r["held"]], key=lambda r: r["value_usd"], reverse=True)
    not_held = sorted([r for r in snapshot if not r["held"]], key=lambda r: r["fund"])

    header = st.columns([3, 1, 2, 2, 2, 2])
    for col, label in zip(header, ["Fund", "Holds It?", "Shares", "Value", "Change", "As Of"]):
        col.markdown(f"**{label}**")

    for row in held + not_held:
        cols = st.columns([3, 1, 2, 2, 2, 2])
        cols[0].write(row["fund"])
        cols[1].write("✅" if row["held"] else "—")
        cols[2].write(f"{row['shares']:,}" if row["shares"] is not None else "—")
        cols[3].write(f"${row['value_usd']:,}" if row["value_usd"] is not None else "—")
        if row["change"]:
            color = _CHANGE_COLORS.get(row["change"], charting.MUTED_COLOR)
            cols[4].markdown(f"<span style='color:{color};font-weight:600;'>{row['change']}</span>", unsafe_allow_html=True)
        else:
            cols[4].write("—")
        cols[5].write(row["quarter_end"].strftime("%b %Y") if row["quarter_end"] else "No data yet")


# --- Ticker lookup ---------------------------------------------------------

ticker = st.text_input("Ticker", placeholder="e.g. AAPL").strip().upper()

if ticker:
    snapshot = institutional_holdings.get_snapshot_for_ticker(conn, ticker)
    held_count = sum(1 for r in snapshot if r["held"])
    total_count = len(snapshot)

    stat_col, _ = st.columns([1, 3])
    charting_color = charting.GOOD_COLOR if held_count > 0 else charting.MUTED_COLOR
    with stat_col:
        st.markdown(
            f"""
            <div style="text-align:center;">
                <div style="font-size:0.85rem;color:{charting.MUTED_COLOR};">Funds holding {ticker}</div>
                <div style="font-size:2rem;font-weight:700;color:{charting_color};">{held_count} of {total_count}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if total_count == 0:
        st.info("No funds are being tracked yet - add some below under Manage Tracked Funds.")
    else:
        _render_snapshot_table(snapshot)
else:
    st.info("Type a ticker above to see which tracked funds hold it.")


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
