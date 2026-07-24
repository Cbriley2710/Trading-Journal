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

    Two things a naive version of this gets wrong, both fixed here:

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
    """
    components.html(
        f"""
        <!-- _nonce: {time.time()} -->
        <script>
            (function attempt(triesLeft) {{
                const el = window.parent.document.getElementById("{anchor_id}");
                if (el) {{
                    el.scrollIntoView({{behavior: "instant", block: "start"}});
                }}
                if (triesLeft > 0) {{
                    setTimeout(function() {{ attempt(triesLeft - 1); }}, 50);
                }}
            }})(200);
        </script>
        """,
        height=0,
    )


def stat_tile(column, label, value, color=None, size="1.4rem"):
    """
    Renders one number in a column, with its muted label above it. If a
    color is given, the number is colored (green for a gain, red for a
    loss) - otherwise it's left the normal text color. `size` is the
    number's font size - the Dashboard and Open Positions pages use the
    default, Trade Analyzer and Shortlist use a slightly smaller
    "1.3rem" (kept exactly as each page always looked).
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
