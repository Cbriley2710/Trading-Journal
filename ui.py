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
import time

import streamlit.components.v1 as components

import charting


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
