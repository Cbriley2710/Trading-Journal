"""
UI
=====================
Tiny shared UI pieces used by multiple pages - the same idea as nav.py
(one focused file for one shared thing), so a tweak to how something
looks only ever has to happen in one place. Until this file existed,
the stat tile below was copy-pasted into four different pages, which
had already started to drift (two pages used a slightly bigger font
than the other two).
"""

import json
import tempfile
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import analyze_trades
import charting
import database
import position_sizing
import timeutil


def scroll_to_anchor(anchor_id):
    """
    Scrolls the browser to the element with `id="{anchor_id}"` on the
    page. Streamlit reruns the whole script on every interaction, and a
    rerun triggered by st.rerun() in particular resets the page's scroll
    to the very top - past the nav bar and anything else above whatever
    you were actually looking at. Placing an anchor
    (st.markdown(f'<div id="{anchor_id}"></div>', unsafe_allow_html=True))
    right above the section you want to land on, then calling this right
    after, puts you back where you were instead of at the page's outer top.

    Runs inside a zero-height iframe (components.html) since a plain
    st.markdown(..., unsafe_allow_html=True) doesn't execute embedded
    <script> tags at all - only a real component iframe does.
    window.parent is what reaches back out into the actual page from
    inside that iframe.

    Three things a naive version of this gets wrong, all fixed here:

    1. Saving the SAME ticker's journal twice in a row sends this exact
       same HTML both times - Streamlit/React can (and does) decide the
       component's content hasn't changed and skip actually reloading
       the iframe, so the <script> only ever ran the FIRST time. The
       `_nonce` timestamp, embedded somewhere the browser has to parse
       but that has no visible effect, makes every call's HTML
       byte-different, forcing a real reload every time.
    2. The anchor div is rendered by an ordinary st.markdown call
       earlier in the same script run, but Streamlit's frontend patches
       the page asynchronously - there's no guarantee the anchor has
       actually been mounted into the DOM, or that everything ABOVE it
       (widgets, the custom chart component's iframe) has finished its
       own layout, by the moment this iframe's script starts running.
       A single scrollIntoView call can fire against a page that's
       still reflowing above the anchor, landing short of the target.
       Rather than trying exactly once, this keeps re-issuing
       scrollIntoView on every retry tick for up to ~10 seconds (200
       tries, 50ms apart) - "instant" scrolling makes repeat calls
       cheap, and each one re-corrects for whatever has shifted since
       the last, so the final position is right even if layout above
       the anchor is still settling when the first attempt runs. The
       anchor used for the Journal Session's chart (see
       pages/2_Shortlist.py's render_price_chart()) sits right before
       a live price-history fetch, which can easily take longer than a
       couple seconds on a cold cache - a shorter window risked giving
       up before the anchor even existed, silently leaving the page at
       the rerun's default top-of-page scroll reset instead of at the
       chart.
    3. Re-issuing scrollIntoView unconditionally for the full ~10
       seconds means it was fighting any attempt to manually scroll
       away during that entire window - every 50ms retry would just
       snap the page straight back, making the page feel locked in
       place right after a chart loads. A wheel/touch/keyboard event on
       the real page cancels the remaining retries immediately, so a
       deliberate scroll wins instead of getting overridden. The
       listeners are registered on window.parent (the real page, not
       this throwaway iframe) - since this iframe gets torn down and
       replaced on every call, the browser won't clean those up on its
       own, so this removes them itself once either the user has
       scrolled or the retries run out, whichever comes first.
    """
    components.html(
        f"""
        <!-- _nonce: {time.time()} -->
        <script>
            (function() {{
                let interrupted = false;
                const events = ["wheel", "touchmove", "keydown"];
                function onUserInput() {{
                    interrupted = true;
                    cleanup();
                }}
                function cleanup() {{
                    events.forEach(e => window.parent.removeEventListener(e, onUserInput));
                }}
                events.forEach(e => window.parent.addEventListener(e, onUserInput, {{passive: true}}));

                (function attempt(triesLeft) {{
                    if (interrupted) {{
                        return;
                    }}
                    const el = window.parent.document.getElementById("{anchor_id}");
                    if (el) {{
                        el.scrollIntoView({{behavior: "instant", block: "start"}});
                    }}
                    if (triesLeft > 0) {{
                        setTimeout(function() {{ attempt(triesLeft - 1); }}, 50);
                    }} else {{
                        cleanup();
                    }}
                }})(200);
            }})();
        </script>
        """,
        height=0,
    )


def focus_textarea(label):
    """
    Puts the cursor in the <textarea> whose visible label is `label`, so
    stepping to a new ticker in the Journal Session drops you straight
    into typing instead of needing an extra click into the box first.

    Streamlit mirrors a widget's own label onto its underlying
    <textarea> as its aria-label attribute - reused here to find the
    right one, rather than trying to reconstruct Streamlit's own
    internal (undocumented, hashed) widget key/id scheme. Matched with
    a loop + getAttribute rather than spliced into a CSS selector
    string, since a label can contain an apostrophe ("Today's
    Journal") that would otherwise clash with whatever quote character
    wraps the selector - json.dumps() below produces a properly
    escaped JS string literal instead of a hand-quoted one.

    Same iframe + nonce + retry-until-found shape as scroll_to_anchor()
    above, for the same reason (the box may not be mounted into the DOM
    yet when this first runs) - but unlike that scroll restore, this
    stops the moment it succeeds instead of repeating: there's no
    reason to keep stealing focus back once it's already landed there.
    """
    components.html(
        f"""
        <!-- _nonce: {time.time()} -->
        <script>
            (function attempt(triesLeft) {{
                const target = {json.dumps(label)};
                const boxes = window.parent.document.querySelectorAll("textarea");
                let found = null;
                for (const box of boxes) {{
                    if (box.getAttribute("aria-label") === target) {{
                        found = box;
                        break;
                    }}
                }}
                if (found) {{
                    found.focus();
                }} else if (triesLeft > 0) {{
                    setTimeout(function() {{ attempt(triesLeft - 1); }}, 50);
                }}
            }})(200);
        </script>
        """,
        height=0,
    )


def parse_ticker_input(text):
    """
    Turns "NVDA" or a pasted batch like "NVDA, AMD MSFT" (commas,
    spaces, tabs, or new lines between tickers - however it was copied)
    into a clean, de-duplicated list of uppercase symbols, keeping the
    order they were typed. Shared by the Shortlist page's watchlist-add
    box and the Screener page's ticker list - moved here (rather than
    living only on Shortlist, where it started) once a second page
    needed the exact same parsing.
    """
    symbols = []
    for part in text.replace(",", " ").split():
        sym = part.strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols


def stat_tile(column, label, value, color=None, size="1.4rem"):
    """
    Renders one number in a column, with its muted label above it. If a
    color is given, the number is colored (green for a gain, red for a
    loss) - otherwise it's left the normal text color. `size` is the
    number's font size - every page uses this same default now (Trade
    Analyzer and Shortlist used to pass a smaller "1.3rem" here, a
    leftover from before this shared helper existed); `size` still
    exists as a param for whatever future page might genuinely need a
    different size on purpose.
    """
    style = f"color:{color};" if color else ""
    column.markdown(
        f"""
        <div style="text-align:center;">
            <div style="font-size:0.85rem;color:{charting.MUTED_COLOR};">{label}</div>
            <div style="font-size:{size};font-weight:600;{style}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_csv_import_widget(conn, key):
    """
    A Fidelity/Schwab CSV export uploader that imports whatever's new
    into the shared `transactions` table and recalculates completed
    trades - the exact import/error-handling flow used on both the
    Settings page's "Import from a CSV export" section and the Journal
    Session's "Choose Lists to Journal" screen (there so you can catch
    up on today's fills before starting to journal). Shared here, not
    copy-pasted twice, so neither call site's behavior can drift from
    the other - including database.record_csv_upload(), which every
    successful import here calls, so "new trades since your last
    upload" (see get_new_trades_since_last_upload()'s own docstring)
    stays accurate no matter which page you actually uploaded from.

    `key` keeps this widget's own state independent from any OTHER
    file_uploader on the same page render - Streamlit requires a unique
    key whenever more than one of the same widget type exists.
    """
    uploaded_file = st.file_uploader(
        "Fidelity or Schwab CSV export", type="csv", key=f"{key}_csv_uploader")
    if uploaded_file is None:
        return

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = Path(tmp.name)

    try:
        with st.spinner("Importing transactions..."):
            new_count = database.import_transactions(conn, tmp_path)
            trade_count = database.rebuild_trades(conn)
    except ValueError:
        # detect_csv_source() raises this when the file isn't a
        # recognizable Fidelity or Schwab export - show a plain message
        # instead of a crash screen. Nothing was imported.
        st.error(
            "That file doesn't look like a Fidelity or Schwab trade "
            "history export - its header row wasn't recognized. Make "
            "sure you exported Transaction/Account History as a CSV "
            "from your brokerage, then try again."
        )
    else:
        database.record_csv_upload(conn, timeutil.now_eastern())
        st.success(
            f"Imported {new_count} new transaction row(s). "
            f"{trade_count} completed stock trade(s) total in the database."
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def render_since_last_journal_summary(conn, cutoff_date):
    """
    Shows what's happened since you last journaled - trade stats for
    anything that CLOSED since then (win rate, P/L, a per-ticker
    breakdown - see analyze_trades.review_session_summary()), plus a
    sizing check for anything OPENED since then (see position_sizing.
    newly_opened_positions_since()), comparing each new position's
    actual size against today's Position Sizing recommendation.

    `cutoff_date` is database.get_previous_journal_date()'s result -
    None means you've never journaled before, so there's no reference
    point yet (renders nothing, same idea as get_new_trades_since_last_
    upload() before any CSV has ever been uploaded). Also renders
    nothing if there's simply been no activity at all since then.
    """
    if cutoff_date is None:
        return

    closed_since = [t for t in database.get_trades(conn) if t["date"].date() > cutoff_date]
    new_positions = position_sizing.newly_opened_positions_since(conn, cutoff_date)
    if not closed_since and not new_positions:
        return

    st.caption(f"Since your last journal session ({cutoff_date:%m/%d/%Y}):")

    if closed_since:
        # review_session_summary() expects plain `date` objects and an
        # "exit_date" key - database.get_trades() rows use `date` and
        # real datetimes (see that function's own docstring on why).
        reviews = [
            {**t, "entry_date": t["entry_date"].date(), "exit_date": t["date"].date()}
            for t in closed_since
        ]
        summary = analyze_trades.review_session_summary(reviews)
        best = max(reviews, key=lambda r: r["profit_loss"])
        worst = min(reviews, key=lambda r: r["profit_loss"])

        cols = st.columns(7)
        stat_tile(cols[0], "Total Trades", f"{summary['total']}")
        stat_tile(cols[1], "Win Rate", f"{summary['batting_avg']:.1f}%")
        stat_tile(cols[2], "Total P/L", f"${summary['total_pl']:,.2f}", charting.win_loss_color(summary["total_pl"] >= 0))
        stat_tile(cols[3], "Avg Win", f"${summary['avg_win_dollar']:,.2f}" if summary["avg_win_dollar"] is not None else "N/A", charting.win_loss_color(True))
        stat_tile(cols[4], "Avg Loss", f"${summary['avg_loss_dollar']:,.2f}" if summary["avg_loss_dollar"] is not None else "N/A", charting.win_loss_color(False))
        stat_tile(cols[5], "Best Trade", f"{best['symbol']} ${best['profit_loss']:,.2f}", charting.win_loss_color(True))
        stat_tile(cols[6], "Worst Trade", f"{worst['symbol']} ${worst['profit_loss']:,.2f}", charting.win_loss_color(False))

        if len(summary["per_ticker"]) > 1:
            header_cols = st.columns(3)
            header_cols[0].markdown("**Symbol**")
            header_cols[1].markdown("**Trades (Wins)**")
            header_cols[2].markdown("**Net P/L**")
            for symbol, row in summary["per_ticker"].items():
                row_cols = st.columns(3)
                row_cols[0].write(symbol)
                row_cols[1].write(f"{row['trades']} ({row['wins']})")
                row_cols[2].write(f"${row['net_pl']:,.2f}")

    if new_positions:
        st.markdown("**Newly Opened Positions**" if closed_since else "**Newly Opened Positions Since Last Session**")
        sizing = position_sizing.evaluate_position_sizing(conn)
        account_value = sizing["account_value"]

        if account_value is None:
            st.caption("Set your account value on the Dashboard page to see each position's sizing check.")

        header_cols = st.columns([2, 2, 2, 2, 1])
        for col, label in zip(header_cols, ["Symbol", "Entry Date", "Actual Size", "Recommended", ""]):
            col.markdown(f"**{label}**")
        for p in new_positions:
            row_cols = st.columns([2, 2, 2, 2, 1])
            row_cols[0].write(f"{p['symbol']}{' (S)' if p['direction'] == 'SHORT' else ''}")
            row_cols[1].write(p["entry_date"].strftime("%m/%d/%Y"))
            if account_value:
                actual_pct = p["cost_basis"] / account_value * 100
                row_cols[2].write(f"{actual_pct:.2f}% of account")
                row_cols[3].write(f"{sizing['recommended_pct_of_account']:.2f}% of account")
                row_cols[4].write("⚠️" if actual_pct > sizing["recommended_pct_of_account"] else "✅")
            else:
                row_cols[2].write("N/A")
                row_cols[3].write(f"{sizing['recommended_pct_of_account']:.2f}% of account")
                row_cols[4].write("")
