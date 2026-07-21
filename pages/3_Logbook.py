"""
Logbook
=====================
The permanent, day-by-day archive behind every ticker that's ever been
on your Shortlist - review exactly how a trade's chart looked and what
you were thinking, one day at a time, even long after the trade has
closed.

Each day's entry is written here by nightly_archive.py (a chart
snapshot + whatever you wrote in that day's Shortlist journal box) -
see database.get_logbook_entries(). A day with no notes or no archived
image yet (e.g. today, before tonight's archive run) still shows up,
just with whichever piece is missing left blank.

The ticker picker itself is filterable - by date range (or, via
"Start from a date instead", a single date through today - for
picking up where you left off rather than setting both ends of a
range every time), by which list(s)/open-position status a symbol is
CURRENTLY in (see database.get_logbook_summary() for the underlying
per-symbol summary this is built from), and by a keyword search across
every symbol's notes. Either date filter also trims which days show
once you've picked a ticker (oldest first, so the date you picked - or
the closest logged day after it - is what you see first), and a "Hide
days with no notes" toggle skips days that only ever got an
auto-archived chart with nothing written.

This page also has a "Daily Report" section - one PDF covering every
list, emailed to a mailing list. See daily_report.py. If you don't
click Generate yourself, nightly_archive.py sends it automatically as
a fallback once the night's archiving is done, the same "manual action
is primary, the nightly script is the fallback" pattern already used
for per-ticker chart archiving.
"""

import streamlit as st

import auth
import daily_report
import database
import nav
import timeutil

st.set_page_config(page_title="Logbook", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("Logbook")

st.title("Logbook")

conn = database.get_connection()

st.header("Daily Report")
st.caption(
    "One PDF covering every list (each ticker's archived chart + notes for "
    "the day you pick), emailed to your configured recipients. Choosing "
    "today's date first archives a fresh chart for every open position and "
    "watchlist ticker, so the report doesn't depend on tonight's automated "
    "archive run having happened yet. If you don't generate it yourself, "
    "that automated run sends it for you as a fallback."
)
report_cols = st.columns([1, 2])
report_date = report_cols[0].date_input("Report date", value=timeutil.today_eastern(), key="report_date")

already_sent_at = database.get_daily_report_status(conn, report_date)
if already_sent_at:
    report_cols[1].caption(f"Already generated and emailed for this date, at {already_sent_at:%I:%M %p}.")
else:
    report_cols[1].caption("Not generated yet for this date.")

if st.button("Generate & Email Report"):
    is_today = report_date == timeutil.today_eastern()
    spinner_text = (
        "Archiving fresh charts and building/sending the report..." if is_today
        else "Building the PDF and sending it..."
    )
    with st.spinner(spinner_text):
        success, message = daily_report.generate_and_send_report(conn, report_date, archive_first=is_today)
    if success:
        st.success(message)
    else:
        st.error(message)

st.divider()

st.header("Browse Logbook")

summary = database.get_logbook_summary(conn)

if not summary:
    st.info(
        "No logbook entries yet. Write a journal entry for an open "
        "position on the Shortlist page, then check back after tonight's "
        "automated archive run."
    )
    st.stop()

# --- What each symbol currently is, for the "Currently in" filter ------
# A symbol can be an open position AND on a watchlist at the same time
# (see database.get_watchlist()'s own docstring - the two are tracked
# independently), so this builds a LIST of tags per symbol, not one.
watchlist_names = database.get_watchlist_names(conn)
list_id_by_symbol = {w["symbol"]: w["list_id"] for w in database.get_watchlist(conn)}
position_symbols = {p["symbol"] for p in database.get_open_positions(conn)}

LIST_OPTIONS = ["Open Positions"] + [watchlist_names[i] for i in range(1, 5)] + ["Not Currently Tracked"]


def symbol_tags(symbol):
    tags = []
    if symbol in position_symbols:
        tags.append("Open Positions")
    if symbol in list_id_by_symbol:
        tags.append(watchlist_names[list_id_by_symbol[symbol]])
    if not tags:
        tags.append("Not Currently Tracked")
    return tags


overall_min = min(s["first_entry"] for s in summary)
overall_max = max(s["last_entry"] for s in summary)

filter_cols = st.columns([2, 2, 2, 2])
date_range = filter_cols[0].date_input(
    "Date range", value=(overall_min, overall_max),
    min_value=overall_min, max_value=overall_max, key="logbook_date_range",
)
selected_lists = filter_cols[1].multiselect(
    "Currently in", options=LIST_OPTIONS, default=LIST_OPTIONS, key="logbook_list_filter",
)
keyword = filter_cols[2].text_input("Search notes for", key="logbook_keyword")

# An alternative to the Date range picker above, for "catch up from
# where I left off" browsing - pick one date and see everything from
# there through today, instead of having to set both ends of a range
# every time. Overrides the Date range widget while checked.
use_start_date = filter_cols[3].checkbox("Start from a date instead", key="logbook_use_start_date")
if use_start_date:
    start_date = filter_cols[3].date_input(
        "Start date (through today)", value=overall_min,
        min_value=overall_min, max_value=timeutil.today_eastern(), key="logbook_start_date",
    )

# date_input in range mode returns a single date until both ends have
# been picked - only filter once there's a real (start, end) pair, same
# pattern as Trade Analyzer's own entry-date filter.
range_start, range_end = (overall_min, overall_max)
if use_start_date:
    range_start, range_end = start_date, timeutil.today_eastern()
elif isinstance(date_range, tuple) and len(date_range) == 2:
    range_start, range_end = date_range

matching_keyword = database.search_logbook_notes(conn, keyword.strip()) if keyword.strip() else None

filtered_summary = [
    s for s in summary
    if s["first_entry"] <= range_end and s["last_entry"] >= range_start
    and any(tag in selected_lists for tag in symbol_tags(s["symbol"]))
    and (matching_keyword is None or s["symbol"] in matching_keyword)
]

if not filtered_summary:
    st.info("No logbook tickers match these filters.")
    st.stop()


def ticker_option_label(s):
    return f"{s['symbol']} — {', '.join(symbol_tags(s['symbol']))} — {s['entry_count']} day(s) logged"


selected_index = st.selectbox(
    "Choose a ticker", options=range(len(filtered_summary)),
    format_func=lambda i: ticker_option_label(filtered_summary[i]),
)
symbol = filtered_summary[selected_index]["symbol"]

entries = database.get_logbook_entries(conn, symbol)
entries = [e for e in entries if range_start <= e["entry_date"] <= range_end]

order_col, hide_col = st.columns([1, 3])
# get_logbook_entries() already returns oldest-first - this just
# optionally flips that, e.g. to catch up on the most recent days
# first instead of starting from whatever date/range was picked above.
reverse_order = order_col.toggle("Newest first", key="logbook_reverse_order")
hide_empty = hide_col.checkbox("Hide days with no notes", key="logbook_hide_empty")
if hide_empty:
    entries = [e for e in entries if e["notes"]]
if reverse_order:
    entries = list(reversed(entries))

st.caption(f"{len(entries)} day(s) shown for {symbol}.")

if not entries:
    st.info("No entries in this range.")

for entry in entries:
    st.subheader(f"{entry['entry_date']:%A, %B %d, %Y}")

    if entry["chart_image"]:
        st.image(entry["chart_image"], width="stretch")
    else:
        st.caption("No chart archived for this day yet - archives happen overnight.")

    st.write(entry["notes"] if entry["notes"] else "_No notes recorded for this day._")
    st.divider()
