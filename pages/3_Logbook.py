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

Also here, in its own tab: "Trade Reviews" - past guided Review
Sessions from the Trade Analyzer page (see pages/1_Trade_Analyzer.py's
render_review_session()). Unlike the Daily Report, there's no nightly
fallback for these - a Review Session's PDF only ever gets generated
by clicking the button below, whenever you actually want one.

The two are shown as separate tabs rather than stacked one after the
other - they're unrelated browsing tasks (day-by-day chart archive vs.
past review sessions), so switching between them shouldn't mean
scrolling past whichever one you're not currently using.
"""

import streamlit as st

import auth
import charting
import database
import nav
import session_keys as sk
import timeutil
import trade_review_report
import ui
from analyze_trades import trade_stats

st.set_page_config(page_title="Logbook", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

if not auth.check_password():
    st.stop()

nav.render_top_nav("logbook")

st.title("Logbook")

conn = database.get_connection()

tab_logbook, tab_reviews = st.tabs(["Logbook", "Trade Reviews"])

with tab_logbook:
    summary = database.get_logbook_summary(conn)

    if not summary:
        st.info(
            "No logbook entries yet. Write a journal entry for an open "
            "position on the Shortlist page, then check back after tonight's "
            "automated archive run."
        )
    else:
        # --- What each symbol currently is, for the "Currently in" filter --
        # A symbol can be an open position AND on a watchlist at the same
        # time (see database.get_watchlist()'s own docstring - the two are
        # tracked independently), so this builds a LIST of tags per symbol,
        # not one.
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
        # where I left off" browsing - pick one date and see everything
        # from there through today, instead of having to set both ends of
        # a range every time. Overrides the Date range widget while
        # checked.
        use_start_date = filter_cols[3].checkbox("Start from a date instead", key="logbook_use_start_date")
        if use_start_date:
            start_date = filter_cols[3].date_input(
                "Start date (through today)", value=overall_min,
                min_value=overall_min, max_value=timeutil.today_eastern(), key="logbook_start_date",
            )

        # date_input in range mode returns a single date until both ends
        # have been picked - only filter once there's a real (start, end)
        # pair, same pattern as Trade Analyzer's own entry-date filter.
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
        else:
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
            # get_logbook_entries() already returns oldest-first - this
            # just optionally flips that, e.g. to catch up on the most
            # recent days first instead of starting from whatever
            # date/range was picked above.
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

with tab_reviews:
    st.caption(
        "Past guided Review Sessions from the Trade Analyzer page - each "
        "closed trade you reviewed, your notes, and every chart timeframe "
        "you saved for it. Generate a PDF here any time, even for an old one."
    )

    if sk.REVIEW_REPORT_MESSAGE in st.session_state:
        st.info(st.session_state.pop(sk.REVIEW_REPORT_MESSAGE))

    review_reports = database.get_review_reports(conn)
    if not review_reports:
        st.info("No trade reviews yet - start one from the Trade Analyzer page.")
    else:
        def review_report_label(r):
            if r["range_start"] and r["range_end"]:
                range_text = f"{r['range_start']:%m/%d/%Y} to {r['range_end']:%m/%d/%Y}"
            else:
                range_text = "All trades"
            return f"{r['created_at']:%m/%d/%Y %I:%M %p} - {range_text} - {r['trade_count']} trade(s)"

        review_index = st.selectbox(
            "Choose a review report", options=range(len(review_reports)),
            format_func=lambda i: review_report_label(review_reports[i]),
            key=sk.REVIEW_REPORT_PICKER,
        )
        selected_report_summary = review_reports[review_index]

        sent_cols = st.columns([2, 1, 1])
        if selected_report_summary["sent_at"]:
            sent_cols[0].caption(
                f"Already generated and emailed, at {selected_report_summary['sent_at']:%m/%d/%Y %I:%M %p}."
            )
        else:
            sent_cols[0].caption("Not generated/emailed yet.")

        if sent_cols[1].button("Generate & Email PDF", key="send_review_report"):
            with st.spinner("Building the PDF and sending it..."):
                success, message = trade_review_report.generate_and_send_review_report(
                    conn, selected_report_summary["id"])
            if success:
                st.success(message)
            else:
                st.error(message)

        # Two clicks, not one - same reasoning as Shortlist's "Remove
        # All": deleting a report permanently takes its saved chart
        # snapshots and notes with it (database.delete_review_report()
        # cascades to trade_reviews/trade_review_snapshots), with no
        # undo. The confirm flag is keyed per report id so switching to
        # a different report in the picker above doesn't leave a stale
        # "Confirm Delete" armed against the wrong one.
        delete_confirm_key = f"_confirm_delete_review_{selected_report_summary['id']}"
        delete_armed = st.session_state.get(delete_confirm_key, False)
        delete_label = "Confirm Delete" if delete_armed else "Delete Report"
        if sent_cols[2].button(delete_label, key="delete_review_report", type="primary" if delete_armed else "secondary"):
            if not delete_armed:
                st.session_state[delete_confirm_key] = True
                st.rerun()
            else:
                database.delete_review_report(conn, selected_report_summary["id"])
                del st.session_state[delete_confirm_key]
                # The picker's stored index may no longer be valid (or
                # may now point at a different report) now that the
                # list is shorter - clearing it resets to the default
                # (first report) instead of risking an out-of-range
                # selection.
                if sk.REVIEW_REPORT_PICKER in st.session_state:
                    del st.session_state[sk.REVIEW_REPORT_PICKER]
                st.session_state[sk.REVIEW_REPORT_MESSAGE] = "Deleted the review report."
                st.rerun()

        report_detail = database.get_review_report(conn, selected_report_summary["id"])
        if not report_detail["reviews"]:
            st.caption("Nothing was saved in this report.")

        if report_detail["predictions_notes"]:
            st.caption("Before You Begin")
            st.write(report_detail["predictions_notes"])
            st.divider()

        # Fetched once outside the loop below - same Jan 1 baseline every
        # review in this report would divide by, see trade_stats()'s own
        # docstring for why this (not today's fully-calculated account
        # value) is the right number to use.
        jan1_balance = database.get_account_value(conn)
        for review in report_detail["reviews"]:
            short_tag = " (Short)" if review["direction"] == "SHORT" else ""
            st.subheader(
                f"{review['symbol']}{short_tag}: {review['entry_date']:%m/%d/%Y} to {review['exit_date']:%m/%d/%Y}"
            )

            # Same numbers as Trade Analyzer's fact tiles, computed the
            # same way (see analyze_trades.trade_stats()) - the trade's
            # own numbers were saved on the review itself, not re-looked-up
            # from database.get_trades(), so this still shows correctly
            # even if trades have since been re-imported.
            stats = trade_stats(
                review["direction"], review["buy_price"], review["sell_price"], review["quantity"],
                review["profit_loss"], review["entry_date"], review["exit_date"],
                account_value=jan1_balance,
            )
            outcome_color = charting.win_loss_color(review["profit_loss"] >= 0)
            show_equity = stats["equity_contribution"] is not None
            stat_cols = st.columns(7 if show_equity else 6)
            ui.stat_tile(
                stat_cols[0], "Short Entry" if stats["is_short"] else "Entry", f"${stats['entry_price']:,.2f}")
            ui.stat_tile(stat_cols[1], "Cover" if stats["is_short"] else "Exit", f"${stats['exit_price']:,.2f}")
            ui.stat_tile(stat_cols[2], "Shares", f"{review['quantity']:,.0f}")
            ui.stat_tile(stat_cols[3], "P/L", f"${review['profit_loss']:,.2f}", outcome_color)
            ui.stat_tile(stat_cols[4], "% Change", f"{stats['pct_change']:,.2f}%", outcome_color)
            ui.stat_tile(stat_cols[5], "Days Held", f"{stats['days_held']:,.0f}")
            if show_equity:
                ui.stat_tile(
                    stat_cols[6], "Equity Contribution", f"{stats['equity_contribution']:,.2f}%", outcome_color)

            if review["snapshots"]:
                snapshot_cols = st.columns(len(review["snapshots"]))
                for col, (timeframe, chart_image) in zip(snapshot_cols, review["snapshots"].items()):
                    col.caption(timeframe)
                    col.image(chart_image, width="stretch")
            else:
                st.caption("No chart snapshots were saved for this trade.")
            st.write(review["notes"] if review["notes"] else "_No notes recorded for this trade._")
            st.divider()

        if report_detail["reflections_notes"]:
            st.caption("Reflections")
            st.write(report_detail["reflections_notes"])
