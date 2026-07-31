"""
Report Utils
=====================
Small pieces shared by every PDF report this app builds - currently
daily_report.py and trade_review_report.py. Split out once a second
report module needed the exact same secret-lookup/Latin-1-safety/
hex-to-rgb helpers daily_report.py already had, the same "more than one
place needs this, so it gets a shared home" reasoning behind ui.py/
nav.py rather than one module importing another's underscore-prefixed
internals.
"""

import os

import streamlit as st


def get_secret(key):
    """Reads a secret from st.secrets first, falling back to a plain
    environment variable - the same dual lookup
    database._get_database_url() uses, needed here too since report
    code runs both inside Streamlit (a page's button) and inside plain
    scripts (nightly_archive.py, run by GitHub Actions with no
    st.secrets file)."""
    try:
        return st.secrets[key]
    except Exception:
        pass

    value = os.environ.get(key)
    if value:
        return value

    raise RuntimeError(
        f"No {key} found. Add it to .streamlit/secrets.toml (see "
        f"secrets.toml.example) or set it as an environment variable."
    )


def safe_text(text):
    """fpdf2's built-in fonts only support Latin-1 - if a note ever has
    a character outside that (an emoji, say), swap it for a "?" instead
    of crashing the whole report over one character."""
    return text.encode("latin-1", "replace").decode("latin-1")


def hex_to_rgb(hex_color):
    """"#RRGGBB" -> (r, g, b) ints - fpdf2's set_fill_color()/
    set_text_color() want separate 0-255 numbers, not a hex string."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
