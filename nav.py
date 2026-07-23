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

Also applies the custom background image (see pages/5_Settings.py),
since render_top_nav() is the one thing every page already calls near
its top - piggybacking on it here means the background shows up
everywhere without having to touch every page individually.
"""

import base64

import streamlit as st

import database

# (label shown on its button, file path Streamlit needs for switch_page).
# "Dashboard" is the main entry script (dashboard.py), not in pages/.
PAGES = [
    ("Dashboard", "dashboard.py"),
    ("Import Trades", "pages/0_Import_Trades.py"),
    ("Trade Analyzer", "pages/1_Trade_Analyzer.py"),
    ("Shortlist", "pages/2_Shortlist.py"),
    ("Open Positions", "pages/4_Open_Positions.py"),
    ("Logbook", "pages/3_Logbook.py"),
    ("Settings", "pages/5_Settings.py"),
]


@st.cache_data(ttl=300, show_spinner=False)
def _load_background_image():
    """
    Cached read of the saved custom background image (see database.
    get_background_image()). render_top_nav() calls this on EVERY page
    load/rerun - without caching, that would mean re-fetching a
    potentially multi-hundred-KB image from the database constantly for
    no reason, the same class of cost already worth avoiding elsewhere
    in this project (see charting.fetch_history()'s own caching).
    Cached for 5 minutes; clear_background_cache() below is called
    right after a save/remove on the Settings page so the CURRENT
    session sees the change immediately instead of waiting out the TTL.
    """
    conn = database.get_connection()
    return database.get_background_image(conn)


def clear_background_cache():
    """Clears the cached background image read - call this right after
    saving or removing a background on the Settings page (see
    _load_background_image()'s own docstring for why)."""
    _load_background_image.clear()


def _apply_background():
    """
    Injects CSS for the custom background image, if one's been
    uploaded. A dark, semi-transparent overlay is layered on top of the
    image itself (not just the raw picture) - every chart/table/text
    color on this app assumes a dark background underneath it, so
    without the overlay, a bright uploaded photo would make plenty of
    existing text unreadable regardless of what the picture shows.
    """
    background = _load_background_image()
    if background is None:
        return

    encoded = base64.b64encode(background["bytes"]).decode()
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background-image: linear-gradient(rgba(10, 10, 14, 0.82), rgba(10, 10, 14, 0.82)),
                url("data:{background['mime']};base64,{encoded}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_top_nav(current_label):
    """
    Call this once near the top of every page, right after the password
    check passes. `current_label` must match one of the labels in PAGES
    above - that button is shown highlighted and disabled (you're
    already there); clicking any other button jumps straight to that
    page via st.switch_page().
    """
    _apply_background()

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
