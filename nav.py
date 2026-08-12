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

# (stable page id, default label shown on its button, file path Streamlit
# needs for switch_page). "dashboard" is the main entry script
# (dashboard.py), not in pages/. The id is what every page passes to
# render_top_nav() and never changes; the label is just the default
# text shown until/unless it's overridden on the Settings page (see
# get_nav_labels() below) - keeping these separate is what lets a page's
# button be renamed without breaking "which page am I on" tracking.
PAGES = [
    ("dashboard", "Dashboard", "dashboard.py"),
    ("trade_analyzer", "Trade Analyzer", "pages/1_Trade_Analyzer.py"),
    ("shortlist", "Shortlist", "pages/2_Shortlist.py"),
    ("open_positions", "Open Positions", "pages/4_Open_Positions.py"),
    ("logbook", "Logbook", "pages/3_Logbook.py"),
    ("goals", "Goals", "pages/6_Goals.py"),
    ("screener", "Screener", "pages/7_Screener.py"),
    ("market_context", "Market Context", "pages/8_Market_Context.py"),
    ("settings", "Settings", "pages/5_Settings.py"),
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


@st.cache_data(ttl=300, show_spinner=False)
def _load_nav_labels():
    """
    Cached read of the saved nav-bar label overrides (see database.
    get_nav_labels()) - render_top_nav() calls this on every page
    load/rerun, same reasoning as _load_background_image() above.
    """
    conn = database.get_connection()
    return database.get_nav_labels(conn)


def clear_nav_labels_cache():
    """Clears the cached nav-label read - call this right after saving
    or resetting labels on the Settings page so the CURRENT session
    sees the change immediately instead of waiting out the TTL."""
    _load_nav_labels.clear()


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


def render_top_nav(current_page):
    """
    Call this once near the top of every page, right after the password
    check passes. `current_page` must match one of the stable ids in
    PAGES above (e.g. "settings", not "Settings") - that button is
    shown highlighted and disabled (you're already there); clicking any
    other button jumps straight to that page via st.switch_page(). The
    actual button TEXT comes from _load_nav_labels() (a page's custom
    label from the Settings page, or PAGES' own default if it's never
    been customized) - current_page itself never changes even if the
    label does, since page routing/highlighting can't depend on
    whatever a user happens to have renamed a button to.
    """
    _apply_background()

    st.markdown(
        """
        <style>
        [data-testid='stSidebarNav'] {display: none;}
        /* Chrome's CSS scroll anchoring silently readjusts this
        container's scroll position whenever content above/around it
        resizes (e.g. a chart finishing its async layout right after
        ui.scroll_to_anchor() moves the page) - it was quietly undoing
        that deliberate scroll a moment after it happened, landing back
        wherever the browser's own anchor node happened to sit instead
        of at the ticker's chart. Disabling it here is what makes
        scroll_to_anchor()'s position actually stick. */
        [data-testid='stMain'] {overflow-anchor: none;}

        /* On a phone-width screen, len(PAGES) (8) equal-width columns
        squeeze labels like "Trade Analyzer" into unreadably narrow
        buttons. Scoped to just the nav row (the "top_nav_row" container
        key below, via Streamlit's documented .st-key-<key> class - see
        https://docs.streamlit.io/develop/concepts/configuration/theming
        under "Using custom CSS classes") so this doesn't affect any
        other st.columns() grid elsewhere in the app (Open Positions'
        table, Shortlist's watchlists, etc), which need their precise
        column alignment kept intact. */
        @media (max-width: 640px) {
            .st-key-top_nav_row [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap;
            }
            .st-key-top_nav_row [data-testid="stHorizontalBlock"] > div {
                min-width: 30%;
            }
            .st-key-top_nav_row button {
                font-size: 0.75rem;
                padding: 0.25rem 0.4rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    nav_labels = _load_nav_labels()

    with st.container(key="top_nav_row"):
        cols = st.columns(len(PAGES))
        for col, (page_id, default_label, path) in zip(cols, PAGES):
            label = nav_labels.get(page_id, default_label)
            # Keyed by the stable page_id, not the label - two pages
            # given the same custom label would otherwise collide
            # (Streamlit identifies a plain st.button by its label plus
            # position, and a duplicate would raise an error).
            if page_id == current_page:
                col.button(label, disabled=True, width="stretch", type="primary", key=f"nav_btn_{page_id}")
            elif col.button(label, width="stretch", key=f"nav_btn_{page_id}"):
                st.switch_page(path)

    st.divider()
