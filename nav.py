"""
Nav
=====================
The top navigation bar every page uses instead of Streamlit's built-in
sidebar page list. That sidebar list resizes the whole page sideways
every time it's opened or closed - this hides it and replaces it with a
plain row of buttons across the top, which never resizes anything since
it's just normal page content, not a resizable panel. The real sidebar
still exists and still works (dashboard.py uses it for its filters) -
only the auto-generated page-switching list inside it is hidden.
"""

import streamlit as st

# (label shown on its button, file path Streamlit needs for switch_page).
# "Dashboard" is the main entry script (dashboard.py), not in pages/.
PAGES = [
    ("Dashboard", "dashboard.py"),
    ("Import Trades", "pages/0_Import_Trades.py"),
    ("Trade Analyzer", "pages/1_Trade_Analyzer.py"),
    ("Shortlist", "pages/2_Shortlist.py"),
    ("Logbook", "pages/3_Logbook.py"),
]


def render_top_nav(current_label):
    """
    Call this once near the top of every page, right after the password
    check passes. `current_label` must match one of the labels in PAGES
    above - that button is shown highlighted and disabled (you're
    already there); clicking any other button jumps straight to that
    page via st.switch_page().
    """
    st.markdown(
        "<style>[data-testid='stSidebarNav'] {display: none;}</style>",
        unsafe_allow_html=True,
    )

    cols = st.columns(len(PAGES))
    for col, (label, path) in zip(cols, PAGES):
        if label == current_label:
            col.button(label, disabled=True, width="stretch", type="primary")
        elif col.button(label, width="stretch"):
            st.switch_page(path)

    st.divider()
