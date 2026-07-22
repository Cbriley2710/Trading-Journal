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
    """
    components.html(
        f"""
        <script>
            const el = window.parent.document.getElementById("{anchor_id}");
            if (el) {{ el.scrollIntoView({{behavior: "instant", block: "start"}}); }}
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
