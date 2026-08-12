"""
Market Context
=====================
Two things on one page:

  1. Today's Market Narrative - a written summary (index levels, sector
     leadership, market-moving news) saved by
     database.get_market_narrative()/save_market_narrative(). Auto-
     generated once per day via narrative_generator.py (the Claude API's
     web search tool, grounded in market_context.compute_context()'s
     real numbers for the "Stock Highlights" section) the first time
     this page loads with nothing saved yet for today - see
     render_narrative() below for the once-per-day guard, since each
     generation costs real API spend. A manual paste-in box stays
     available underneath, both as a "Regenerate didn't work" fallback
     (no ANTHROPIC_API_KEY set, or the API call failed) and for hand-
     editing/overriding whatever got generated.

  2. Your Watchlist & Positions vs. the Market - a live, computed table
     covering every ticker on your Shortlist (Lists 1-4) or held as an
     Open Position: is it above its own 21 EMA, how does its 6-month
     return compare to QQQ's, how tight is it (ADR%, NR7), how close to
     its 6-month high, and did today register as a distribution or
     accumulation day. All of this reuses screener.engine's own tested
     math (see market_context.py) - nothing here is a separate,
     possibly-drifting copy of the Screener page's numbers.

Both sections are informational only, not investment advice - same
caption already used on the Screener page.
"""

import pandas as pd
import streamlit as st

import auth
import database
import market_context
import narrative_generator
import nav
import timeutil

# Set in session_state (not session_keys.py - this key is only ever
# read/written right here, see that module's own docstring on why
# single-use keys aren't centralized there) when an auto-generation
# attempt fails, so a later rerun of this page - triggered by ANY
# widget interaction anywhere on it, not just this section - doesn't
# silently retry (and re-pay for) a generation that already failed
# today. The Regenerate button is the explicit way to try again.
NARRATIVE_GENERATION_FAILED_KEY = "_market_narrative_generation_failed_today"

st.set_page_config(page_title="Market Context", page_icon="🌐", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("market_context")

st.title("Market Context")
st.caption(
    "Where today's tape stands, and where your own Shortlist/Open Positions stand "
    "against it. Informational only - not investment advice."
)

conn = database.get_connection()


# --- Today's written narrative ---------------------------------------------

def _generate_and_save_narrative(today):
    """
    Calls narrative_generator.generate_narrative() and saves the result
    on success. Returns an error message string on failure (nothing is
    saved, so a later attempt - auto or Regenerate - starts fresh), or
    None on success.
    """
    success, result = narrative_generator.generate_narrative(conn)
    if not success:
        return result
    database.save_market_narrative(conn, today, result)
    return None


def render_narrative():
    """
    Shows today's saved narrative if there is one; if not, generates
    one automatically (see _generate_and_save_narrative() above) the
    first time this page loads today - guarded by
    NARRATIVE_GENERATION_FAILED_KEY so a failure isn't silently retried
    on every later rerun (module docstring has the full reasoning).
    A Regenerate button is always available for a manual refresh later
    in the day, and the paste-in box beneath stays available as a
    fallback for whenever the API call fails or ANTHROPIC_API_KEY isn't
    set - open by default when nothing's saved yet, collapsed once
    something is, so pasting an override later is still one click away
    without the box crowding the page every time you just want to read
    what's already there.
    """
    st.subheader("Today's Market Narrative")
    today = timeutil.today_eastern()
    saved = database.get_market_narrative(conn, today)

    if saved is None and NARRATIVE_GENERATION_FAILED_KEY not in st.session_state:
        with st.spinner("Generating today's market narrative..."):
            error = _generate_and_save_narrative(today)
        if error is None:
            st.rerun()
        st.session_state[NARRATIVE_GENERATION_FAILED_KEY] = error
        saved = None  # still nothing saved - fall through to show the failure below

    if saved is not None:
        st.caption(f"Saved {saved['generated_at']:%b %d, %I:%M %p} ET")
        st.markdown(saved["narrative_markdown"])
    elif NARRATIVE_GENERATION_FAILED_KEY in st.session_state:
        st.warning(
            f"Couldn't auto-generate today's narrative: "
            f"{st.session_state[NARRATIVE_GENERATION_FAILED_KEY]} "
            "Paste one in below instead, or try Regenerate."
        )

    if st.button("Regenerate", key="regenerate_narrative_button"):
        with st.spinner("Generating today's market narrative..."):
            error = _generate_and_save_narrative(today)
        if error is None:
            st.session_state.pop(NARRATIVE_GENERATION_FAILED_KEY, None)
        else:
            st.session_state[NARRATIVE_GENERATION_FAILED_KEY] = error
        st.rerun()

    with st.expander("Paste in today's narrative", expanded=saved is None):
        pasted = st.text_area(
            "Narrative text (Markdown is fine - headings, bold, lists all render)",
            value=saved["narrative_markdown"] if saved else "",
            height=220, key="narrative_paste_box",
        )
        if st.button("Save Narrative", key="save_narrative_button"):
            database.save_market_narrative(conn, today, pasted)
            st.session_state.pop(NARRATIVE_GENERATION_FAILED_KEY, None)
            st.success("Saved.")
            st.rerun()


render_narrative()
st.divider()


# --- Quantitative watchlist context -----------------------------------------

def _day_type_label(day_type):
    return {
        "distribution": "Distribution",
        "accumulation": "Accumulation",
        "neutral": "—",
    }.get(day_type, "—")


def render_watchlist_context():
    st.subheader("Your Watchlist & Positions vs. the Market")

    with st.spinner("Fetching live price data for your Shortlist and Open Positions..."):
        context = market_context.compute_context(conn)

    if not context["rows"]:
        st.info(
            "Nothing to show yet - add a ticker to a Shortlist or hold an open "
            "position, then reload this page."
        )
        return

    summary = context["summary"]
    gate_col, breadth_col, rs_col, day_col = st.columns(4)
    with gate_col:
        st.metric(
            "Market Gate (QQQ)",
            "OPEN" if context["gate_open"] else "CLOSED",
            f"{context['gate_pct']:+.2f}% vs 21 EMA" if pd.notna(context["gate_pct"]) else None,
        )
        st.caption(f"As of {context['asof']}")
    with breadth_col:
        st.metric(
            "Above 21 EMA",
            f"{summary['n_above_ema21']}/{summary['n_total']}",
            f"{summary['pct_above_ema21']:.0f}%",
        )
        st.caption("Breadth across Shortlist + Open Positions")
    with rs_col:
        st.metric("Avg RS Excess", f"{summary['avg_rs_excess_pct']:+.1f} pts")
        st.caption("6-month return vs. QQQ, averaged")
    with day_col:
        st.metric(
            "Distribution / Accumulation",
            f"{summary['n_distribution_today']} / {summary['n_accumulation_today']}",
        )
        st.caption("Down/up today on higher volume than yesterday")

    if not context["gate_open"]:
        st.warning(
            "Market gate is CLOSED (QQQ below its own rising 21 EMA) - the same "
            "signal the Screener page uses to flag a weak tape overall."
        )

    table = pd.DataFrame(context["rows"])
    table["Above EMA"] = table["above_ema21"].map({True: "Yes", False: "No"})
    table["RS Line at High"] = table["rsline_at_high"].map({True: "Yes", False: ""})
    table["NR7-2"] = table["nr7_2"].map({True: "⚡", False: ""})
    table["Vol. Contracting"] = table["vol_contracting"].map({True: "Yes", False: "No", None: "—"})
    table["Today"] = table["day_type"].apply(_day_type_label)

    display = table[[
        "symbol", "source", "close", "ema21", "dist_from_ema21_pct", "Above EMA",
        "rs_excess_pct", "RS Line at High", "adr_pct", "pct_off_6mo_high",
        "NR7-2", "Vol. Contracting", "Today",
    ]].rename(columns={
        "symbol": "Ticker", "source": "List", "close": "Close", "ema21": "21 EMA",
        "dist_from_ema21_pct": "Dist. from EMA", "rs_excess_pct": "RS Excess",
        "adr_pct": "ADR %", "pct_off_6mo_high": "% Off 6-Mo High",
    }).sort_values("RS Excess", ascending=False)

    st.dataframe(
        display, hide_index=True, width="stretch",
        column_config={
            "Close": st.column_config.NumberColumn(format="$%.2f"),
            "21 EMA": st.column_config.NumberColumn(format="$%.2f"),
            "Dist. from EMA": st.column_config.NumberColumn(format="%.1f%%"),
            "RS Excess": st.column_config.NumberColumn(format="%.1f"),
            "ADR %": st.column_config.NumberColumn(format="%.2f%%"),
            "% Off 6-Mo High": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )
    st.caption(
        "Same math as the Screener page (screener/engine.py): RS Excess is 6-month "
        "return minus QQQ's, in points. NR7-2 = today and yesterday both the "
        "narrowest range of the last 7 sessions (a tightening base). Vol. "
        "Contracting = 20-day avg. volume below the 50-day (base-building). "
        "'Today' flags a classic IBD distribution day (down on higher volume than "
        "yesterday) or accumulation day (up on higher volume)."
    )

    if context["failed"] or context["insufficient"]:
        with st.expander("Symbols skipped"):
            if context["failed"]:
                st.caption(f"Could not fetch: {', '.join(context['failed'])}")
            if context["insufficient"]:
                detail = ", ".join(f"{t} ({n} bars)" for t, n in context["insufficient"].items())
                st.caption(f"Not enough history yet: {detail}")


render_watchlist_context()
st.caption("Market Context is informational only - not investment advice.")
