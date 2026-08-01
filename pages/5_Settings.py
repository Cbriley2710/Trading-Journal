"""
Settings
=====================
A place for discretionary numbers that are more convenient to tune here
than to have hardcoded in the app's code - starting with the defaults
behind the MA Stop Rule on the Open Positions page (see ma_strategy.py):
which moving average to track a position against, how many closes on
the wrong side of it count as a sell signal, how far that average needs
to clear cost basis before a trailing stop takes over, and the distance
thresholds behind its "approaching"/"extended" warnings.

These numbers are global, used by every position - the Open Positions
page only lets you choose Off/Manual/Auto per ticker, not override the
MA period or thresholds individually (see database.get_position_ma_
settings(), which still supports a per-position override at the data
layer if a future UI ever wants to expose one).

Also here: relative column widths for the Open Positions table's
Positions & Stop-Loss grid (see database.get_open_positions_column_
widths()) - that table is built from plain st.columns() rows rather
than a data_editor grid (needed for the O/M/A mode buttons to be real
buttons), which means there's no native drag-to-resize; this is the
adjustable alternative.

Import Trades used to be its own page - folded in here (at the top)
instead, since it's a one-off/occasional action, not something that
needed a permanent slot in the top nav bar.
"""

import tempfile
import time
from datetime import timedelta
from pathlib import Path

import streamlit as st

import auth
import charting
import database
import nav
import snaptrade_sync
import timeutil

st.set_page_config(page_title="Settings", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("settings")

st.title("Settings")

conn = database.get_connection()
settings = database.get_strategy_settings(conn)

st.header("Import Trades")
st.caption(
    "Two ways to get trade history into the shared database (the same "
    "one the Dashboard, Trade Analyzer, Shortlist, and Logbook pages all "
    "read from): connect Fidelity once through SnapTrade for automatic "
    "daily syncing, or drop in a CSV export from Fidelity or Schwab any "
    "time. Both write to the same `transactions` table and are "
    "duplicate-safe against each other (see database._insert_"
    "transactions()), so there's no harm in using both."
)

st.subheader("Connect Fidelity (SnapTrade)")
st.caption(
    "Pulls trade history straight from Fidelity automatically, once "
    "connected - no manual export needed for that account. Connected "
    "read-only, on purpose: this can never place a trade or move "
    "money, even if it wanted to. SnapTrade refreshes trade history "
    "about once a day, so a trade from today typically won't show up "
    "here until tomorrow."
)

if not snaptrade_sync.is_configured():
    # Stage 1: the app's own SnapTrade API key isn't set up at all yet -
    # nothing else below can work without it.
    st.info(
        "SnapTrade isn't set up yet - add SNAPTRADE_CLIENT_ID and "
        "SNAPTRADE_CONSUMER_KEY to .streamlit/secrets.toml (see "
        "secrets.toml.example) to enable this."
    )
elif not snaptrade_sync.is_registered():
    # Stage 2: one-time step, run once ever - see snaptrade_sync.
    # register_user() for why this is needed even with a Personal API
    # key. The returned secret is only ever shown this once, so it's
    # displayed directly here for you to copy - same as any other
    # "shown once at creation" credential.
    st.warning("One more one-time step before you can connect Fidelity: register your SnapTrade user.")
    if st.button("Register with SnapTrade"):
        try:
            user_secret = snaptrade_sync.register_user()
        except Exception as e:
            st.error(f"Registration failed: {e}")
        else:
            st.success("Registered. Copy this value now - SnapTrade will not show it again:")
            st.code(user_secret)
            st.caption(
                "Save it as SNAPTRADE_USER_SECRET alongside your other "
                "SnapTrade secrets (local .streamlit/secrets.toml, this "
                "app's Streamlit Cloud secrets, and your GitHub repo "
                "secrets), then reload this page."
            )
else:
    # Stage 3: fully set up - show what's connected (if anything) and
    # the Connect/Sync buttons.
    try:
        connected_accounts = snaptrade_sync.list_connected_accounts()
    except Exception as e:
        # Most commonly because nothing's been connected yet at all (a
        # brand new SnapTrade account can error here instead of just
        # returning an empty list). Treat this as "no accounts", NOT as
        # "hide everything" - the Connect button below is exactly what
        # fixes this, so it needs to still show up.
        connected_accounts = []
        st.warning(f"Couldn't check connected accounts yet: {e}")

    if connected_accounts:
        account_labels = ", ".join(
            f"{a.get('institution_name') or 'Brokerage'} ({a.get('name') or a.get('number') or a['id']})"
            for a in connected_accounts
        )
        st.success(f"Connected: {account_labels}")
    else:
        st.write("No brokerage connected yet.")

    connect_col, sync_col = st.columns(2)

    if connect_col.button("Connect / Reconnect Fidelity"):
        try:
            portal_url = snaptrade_sync.get_connection_portal_url()
        except Exception as e:
            st.error(f"Couldn't open the connection portal: {e}")
        else:
            st.link_button("Open SnapTrade Connection Portal", portal_url)
            st.caption(
                "This link expires in 5 minutes - you'll log into "
                "Fidelity directly on Fidelity's own site, so your "
                "Fidelity password never passes through SnapTrade or "
                "this app."
            )

    if sync_col.button("Sync Now", disabled=not connected_accounts):
        # A year-wide window, not just "since last sync" - cheap (one
        # API call per connected account) and the shared insert helper
        # already skips anything already stored, so there's no harm in
        # re-checking further back than strictly necessary each time.
        end_date = timeutil.today_eastern()
        start_date = end_date - timedelta(days=365)
        try:
            with st.spinner("Fetching trade activity from SnapTrade..."):
                new_count = database.import_transactions_snaptrade(conn, start_date, end_date)
                trade_count = database.rebuild_trades(conn)
        except Exception as e:
            st.error(f"SnapTrade sync failed: {e}")
        else:
            st.success(
                f"Imported {new_count} new transaction row(s). "
                f"{trade_count} completed stock trade(s) total in the database."
            )

st.subheader("Import from a CSV export")
st.write(
    "Export your trade history from Fidelity (the single-account export) "
    "or Schwab (Transaction History), then drop the CSV file below - the "
    "brokerage is detected automatically."
)
st.caption(
    "Note: this updates the shared database only - it won't touch your "
    "local Trade Tracker Template 2026.xlsx file, since that file only "
    "exists on your PC. Run import_trades.py locally if you also want "
    "the Excel tracker updated."
)

uploaded_file = st.file_uploader("Fidelity or Schwab CSV export", type="csv")

if uploaded_file is not None:
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
        st.success(
            f"Imported {new_count} new transaction row(s). "
            f"{trade_count} completed stock trade(s) total in the database."
        )
    finally:
        tmp_path.unlink(missing_ok=True)

st.divider()

st.header("Price Cache")
st.caption(
    "Pre-fetched daily price history for every open position and "
    "watchlist ticker, refreshed automatically once a day shortly after "
    "market close (see warm_price_cache.py). Use this if a chart's most "
    "recent candlestick is ever missing - Yahoo Finance occasionally "
    "reports a trading day's volume before that same day's price is "
    "fully finalized, which used to get permanently cached as-is until "
    "the next scheduled refresh. Safe to click any time; it only adds "
    "or replaces data, never removes anything."
)

if st.button("Refresh Price Cache Now"):
    tracked_symbols = sorted(database.get_tracked_symbols(conn))
    results = {}
    with st.spinner(f"Refreshing price cache for {len(tracked_symbols)} symbol(s)..."):
        for symbol in tracked_symbols:
            results[symbol] = charting.warm_price_cache_for_symbol(symbol)
            # A short pause between symbols so a big batch doesn't read
            # as a burst of rapid requests to Yahoo Finance and trip
            # its rate limiting - which used to be able to fail every
            # symbol at once and get reported as "data not ready yet"
            # even when Yahoo's own site showed complete data.
            time.sleep(1)
    # Clears this session's own in-memory chart cache too, so a chart
    # opened right after this shows the refreshed data immediately -
    # without this, the hour-long st.cache_data TTL on these could
    # still serve whatever was fetched (possibly incomplete) before
    # this button was clicked.
    charting.fetch_history.clear()
    charting.fetch_daily_closes.clear()

    refreshed = [s for s, r in results.items() if r == "ok"]
    not_ready = [s for s, r in results.items() if r == "not_ready"]
    unreachable = [s for s, r in results.items() if r in ("rate_limited", "fetch_failed")]
    no_data = [s for s, r in results.items() if r == "no_data"]

    if not not_ready and not unreachable and not no_data:
        st.success(f"Refreshed all {len(refreshed)} symbol(s).")
    else:
        if refreshed:
            st.caption(f"Refreshed {len(refreshed)}/{len(tracked_symbols)} symbol(s).")
        if not_ready:
            st.warning(
                f"Yahoo Finance doesn't have a complete, up-to-date close yet "
                f"for: {', '.join(not_ready)} - try again later."
            )
        if unreachable:
            st.warning(
                f"Couldn't reach Yahoo Finance for: {', '.join(unreachable)} - "
                "this is usually a brief network hiccup or Yahoo Finance "
                "rate-limiting a big batch of requests, not a real data "
                "problem. Try clicking the button again in a minute."
            )
        if no_data:
            st.warning(
                f"Yahoo Finance has no data at all for: {', '.join(no_data)} - "
                "double check the ticker is correct."
            )

st.divider()

st.header("MA Stop Rule Defaults")
st.caption(
    "Used by every open position - the Open Positions page only lets you "
    "choose Off/Manual/Auto per ticker; these numbers apply globally."
)

col1, col2 = st.columns(2)
ma_period = col1.number_input(
    "Moving average period (days)", min_value=2, step=1,
    value=settings["ma_period"], key="settings_ma_period",
)
closes_threshold = col2.number_input(
    "Closes against trend to trigger a sell signal", min_value=1, step=1,
    value=settings["closes_threshold"], key="settings_closes_threshold",
)

col3, col4, col5 = st.columns(3)
unlock_pct = col3.number_input(
    "Unlock % (MA must clear cost basis by this much before Auto trails the stop)",
    min_value=0.0, step=0.5, format="%.1f", value=settings["unlock_pct"], key="settings_unlock_pct",
)
approach_pct = col4.number_input(
    "Approaching % (within this distance of the MA)",
    min_value=0.0, step=0.1, format="%.1f", value=settings["approach_pct"], key="settings_approach_pct",
)
extended_pct = col5.number_input(
    "Extended % (this far or more from the MA)",
    min_value=0.0, step=0.5, format="%.1f", value=settings["extended_pct"], key="settings_extended_pct",
)

if st.button("Save Defaults"):
    database.save_strategy_settings(conn, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct)
    st.success("Saved.")
    st.rerun()

st.divider()

st.header("Open Positions Column Widths")
st.caption(
    "Relative widths for the Positions & Stop-Loss table - not pixels, just "
    "proportions of the row (e.g. doubling a number doubles that column's "
    "share of the width). \"Mode Buttons\" applies to all three O/M/A "
    "buttons at once, since they're always the same width as each other."
)

widths = database.get_open_positions_column_widths(conn)

width_col1, width_col2, width_col3 = st.columns(3)
new_widths = {
    "ticker": width_col1.number_input("Ticker", min_value=0.1, step=0.1, value=widths["ticker"], key="width_ticker"),
    "entry_date": width_col2.number_input("Entry Date", min_value=0.1, step=0.1, value=widths["entry_date"], key="width_entry_date"),
    "shares": width_col3.number_input("Shares", min_value=0.1, step=0.1, value=widths["shares"], key="width_shares"),
    "avg_price": width_col1.number_input("Avg Price", min_value=0.1, step=0.1, value=widths["avg_price"], key="width_avg_price"),
    "current_price": width_col2.number_input("Current Price", min_value=0.1, step=0.1, value=widths["current_price"], key="width_current_price"),
    "unrealized_pl": width_col3.number_input("Unrealized P/L", min_value=0.1, step=0.1, value=widths["unrealized_pl"], key="width_unrealized_pl"),
    "stop_loss": width_col1.number_input("Stop Loss", min_value=0.1, step=0.1, value=widths["stop_loss"], key="width_stop_loss"),
    "mode": width_col2.number_input("Mode Buttons (O/M/A)", min_value=0.1, step=0.05, value=widths["mode"], key="width_mode"),
    "ma_signal": width_col3.number_input("MA Signal", min_value=0.1, step=0.1, value=widths["ma_signal"], key="width_ma_signal"),
}

if st.button("Save Column Widths"):
    database.save_open_positions_column_widths(conn, new_widths)
    st.success("Saved.")
    st.rerun()

st.divider()

st.header("Background Image")
st.caption(
    "Upload a picture to use as the app's background, on every page - "
    "see nav.render_top_nav(), which every page already calls. A dark "
    "overlay is applied on top of it automatically, since every chart, "
    "table, and block of text on this app assumes a dark background "
    "underneath it - without that, a bright photo would make plenty of "
    "existing text unreadable no matter what the picture shows."
)

current_background = database.get_background_image(conn)
if current_background:
    st.image(current_background["bytes"], caption="Current background", width=300)

uploaded_background = st.file_uploader(
    "Upload a background image", type=["png", "jpg", "jpeg"], key="background_upload")

background_cols = st.columns(2)
if background_cols[0].button("Save Background", disabled=uploaded_background is None):
    database.save_background_image(conn, uploaded_background.getvalue(), uploaded_background.type)
    nav.clear_background_cache()
    st.success("Background saved.")
    st.rerun()

if background_cols[1].button("Remove Background", disabled=not current_background):
    database.clear_background_image(conn)
    nav.clear_background_cache()
    st.success("Background removed - back to the default look.")
    st.rerun()

st.divider()

st.header("Chart Accent Color")
st.caption(
    "Recolors the interactive chart's toolbar - the active tool "
    "highlight and hover tooltip border. This and Win/Loss Colors below "
    "are the parts of the app's look that can be customized live like "
    "this; everything else (buttons, backgrounds, text) uses the app's "
    "fixed built-in theme, which only changes with a code update."
)

saved_accent = database.get_accent_color(conn)
current_accent = saved_accent or charting.CATEGORICAL_PALETTE[0]
new_accent = charting.color_input(st, "Accent color", current_accent, "settings_accent_color")

accent_cols = st.columns(2)
if accent_cols[0].button("Save Accent Color", disabled=new_accent == saved_accent):
    database.save_accent_color(conn, new_accent)
    charting.clear_accent_color_cache()
    st.success("Accent color saved.")
    st.rerun()

if accent_cols[1].button("Reset to Default", disabled=saved_accent is None):
    database.clear_accent_color(conn)
    charting.clear_accent_color_cache()
    st.success("Accent color reset to the default.")
    st.rerun()

st.divider()

st.header("Win/Loss Colors")
st.caption(
    "The green/red pair used everywhere a stat tile, chart marker, or "
    "bar shows a gain vs. a loss - Dashboard, Trade Analyzer, Shortlist, "
    "and Open Positions."
)

saved_trade_colors = database.get_trade_colors(conn)
current_good = saved_trade_colors["good"] or charting.GOOD_COLOR
current_critical = saved_trade_colors["critical"] or charting.CRITICAL_COLOR

trade_color_cols = st.columns(2)
new_good = charting.color_input(trade_color_cols[0], "Win color", current_good, "settings_good_color")
new_critical = charting.color_input(trade_color_cols[1], "Loss color", current_critical, "settings_critical_color")

trade_color_buttons = st.columns(2)
trade_colors_unchanged = (
    new_good == saved_trade_colors["good"] and new_critical == saved_trade_colors["critical"]
)
if trade_color_buttons[0].button("Save Win/Loss Colors", disabled=trade_colors_unchanged):
    database.save_trade_colors(conn, new_good, new_critical)
    charting.clear_trade_colors_cache()
    st.success("Win/loss colors saved.")
    st.rerun()

if trade_color_buttons[1].button(
    "Reset to Defaults", disabled=saved_trade_colors == {"good": None, "critical": None}
):
    database.clear_trade_colors(conn)
    charting.clear_trade_colors_cache()
    st.success("Win/loss colors reset to the defaults.")
    st.rerun()

st.divider()

st.header("Navigation Bar Labels")
st.caption(
    "Rename the buttons in the top navigation bar. Leave a box blank "
    "(or matching the default) to use the app's built-in name for that "
    "page."
)

saved_nav_labels = database.get_nav_labels(conn)
nav_label_cols = st.columns(len(nav.PAGES))
nav_label_inputs = {}
for col, (page_id, default_label, _path) in zip(nav_label_cols, nav.PAGES):
    current_value = saved_nav_labels.get(page_id, default_label)
    nav_label_inputs[page_id] = col.text_input(default_label, value=current_value, key=f"settings_nav_label_{page_id}")

new_nav_labels = {}
for page_id, default_label, _path in nav.PAGES:
    text = nav_label_inputs[page_id].strip()
    if text and text != default_label:
        new_nav_labels[page_id] = text

nav_label_buttons = st.columns(2)
if nav_label_buttons[0].button("Save Navigation Labels", disabled=new_nav_labels == saved_nav_labels):
    database.save_nav_labels(conn, new_nav_labels)
    nav.clear_nav_labels_cache()
    st.success("Navigation labels saved.")
    st.rerun()

if nav_label_buttons[1].button("Reset to Defaults", disabled=not saved_nav_labels, key="reset_nav_labels"):
    database.clear_nav_labels(conn)
    nav.clear_nav_labels_cache()
    st.success("Navigation labels reset to the defaults.")
    st.rerun()
