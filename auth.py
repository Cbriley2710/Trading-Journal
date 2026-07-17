"""
Auth
=====================
The password gate shared by every page of the app (dashboard.py and
anything in pages/). It lives in its own file so both places use the
exact same check - Streamlit runs each page as its own script, so
without a shared module, a new page would need its own copy of this
logic and could easily drift out of sync (e.g. someone adds a page and
forgets the gate entirely).
"""

import streamlit as st


def check_password():
    """
    Shows a password box and returns True only once the right password
    has been entered. The correct password lives in st.secrets (see
    dashboard.py's module docstring) - it's never written in this
    file, so it's never something Git/GitHub would ever see.
    """
    if st.session_state.get("authenticated"):
        return True

    st.title("Trading Journal")
    entered = st.text_input("Password", type="password")

    if entered:
        if entered == st.secrets.get("DASHBOARD_PASSWORD"):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False
