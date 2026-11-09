"""
Daily Report
=====================
Builds one PDF covering every list on the Shortlist page - Lists 1-4
(by their saved names) plus Open Positions - with each ticker's
chart image and journal notes for a given day, and emails it to a
mailing list. Notes and plan prices come from the saved journal entry
(database.get_logbook_entries_for_date()); the chart image is built
fresh for each ticker while the PDF is assembled (nothing is stored).

Landscape, one page per ticker, dark-themed to match the chart images
themselves (charting.py's own CHART_BACKGROUND/CHART_TEXT_COLOR/
MUTED_COLOR) - a chart from this app is already a wide,
dark-background image, so a landscape page fits it without shrinking
it down, and a dark page background means there's no bright white
border around a dark chart.

The only caller of generate_and_send_report() below is the "Email
Daily Report" button on the Journal Session's Session complete screen
(pages/2_Shortlist.py) - nothing sends it automatically. Sent the same
evening, a ticker's chart can be missing: charting.build_archive_
snapshot() refuses to draw a chart for a day whose close Yahoo Finance
hasn't finished publishing yet. Those tickers get a "chart not
available yet" note on their page and are named in the send result.

SENDING EMAIL: through Gmail's SMTP server, which needs a Google
Account "App Password" (NOT your regular Gmail password - Gmail
stopped accepting that for this years ago). Generate one, once, at
myaccount.google.com/apppasswords (requires 2-Step Verification to be
turned on first), then put it in REPORT_EMAIL_APP_PASSWORD below -
never your actual account password.
"""

import io
import smtplib
from datetime import datetime
from email.message import EmailMessage

from fpdf import FPDF
from PIL import Image

import charting
import database
import position_sizing
from report_utils import get_secret, hex_to_rgb, safe_text

PAGE_MARGIN_MM = 15
# A4 LANDSCAPE is 297mm x 210mm (width and height swapped from the
# usual portrait 210mm x 297mm) - one wide chart per page instead of
# several narrower ones stacked on a tall portrait page.
CONTENT_WIDTH_MM = 297 - 2 * PAGE_MARGIN_MM

PAGE_BACKGROUND_RGB = hex_to_rgb(charting.CHART_BACKGROUND)
TEXT_COLOR_RGB = hex_to_rgb(charting.CHART_TEXT_COLOR)
MUTED_TEXT_RGB = hex_to_rgb(charting.MUTED_COLOR)


class DarkReportPDF(FPDF):
    """
    A plain FPDF with one difference: every page gets the app's own
    dark chart background painted first. fpdf2 calls header()
    automatically at the start of every add_page() - overriding it
    here is what lets a single definition cover every page (including
    ones fpdf2's own auto-page-break inserts mid-document for long
    notes) instead of every call site needing to remember to paint the
    background itself.
    """
    def header(self):
        self.set_fill_color(*PAGE_BACKGROUND_RGB)
        self.rect(0, 0, self.w, self.h, "F")


def _position_label(position):
    """Same "(Short)" tagging convention used on the Shortlist and Open
    Positions pages, so a short position is never mistaken for a long
    one in the report either."""
    return f"{position['symbol']} (Short)" if position["direction"] == "SHORT" else position["symbol"]


def _open_position_stats_line(position, conn, account_value):
    """
    "% Change: +6.2%  ·  % of Account: 14.8%" for one open position's
    Daily Report page - the same math charting.open_position_returns()
    provides for the Journal Session's live fact tiles (pages/
    2_Shortlist.py's render_position_stats()), reused here so the two
    can never drift apart. Returns None if the current price can't be
    fetched right now (same "just don't show it" handling as everywhere
    else in this app a price lookup can fail).

    Safe to include in a report built to be shareable without revealing
    account value: this page shows no quantity or dollar figure
    anywhere else, so a relative % alone doesn't let a reader back out
    the account's actual dollar value - unlike a raw dollar P/L or
    position size, which this report still deliberately never shows.
    """
    current_price = charting.fetch_latest_price(position["symbol"])
    if current_price is None:
        return None

    returns = charting.open_position_returns(position, current_price, account_value)
    parts = [f"% Change: {returns['pct_change']:+.1f}%"]
    if returns["pct_of_account"] is not None:
        parts.append(f"% of Account: {returns['pct_of_account']:.1f}%")
    return "  ·  ".join(parts)


def _build_chart_image(conn, symbol, entry_date, buy_price, entry_label, as_of, direction="LONG"):
    """Builds one ticker's chart PNG for the report, or None if it can't
    be made right now (no price data, or Yahoo hasn't published today's
    close yet). Never raises - one bad symbol shouldn't sink the report."""
    try:
        return charting.build_archive_snapshot(
            symbol, entry_date, buy_price, entry_label, as_of, direction=direction,
            stop_loss=database.get_stop_loss(conn, symbol))
    except Exception as exc:
        print(f"  {symbol}: chart failed unexpectedly ({exc}), leaving it out.")
        return None


def _write_ticker_page(pdf, section_label, symbol, entry, chart_image, stats_line=None):
    """
    One ticker's own page: a small "which list" breadcrumb, the symbol
    as a heading, its chart image (or a note that it isn't available
    yet), and its notes (or a note that none were recorded), given a
    full page each instead of flowing several onto a shared one.
    `entry` is the saved journal row for the day, or None if nothing
    was saved for this ticker.

    `stats_line` (an open position's % Change / % of Account, see
    _open_position_stats_line() above) is optional - None for every
    watchlist ticker, which has no such stats to show.
    """
    pdf.add_page()

    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=17)
    pdf.cell(0, 10, safe_text(section_label), new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=26)
    pdf.cell(0, 16, safe_text(symbol), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    entry = entry or {}

    if chart_image:
        image = Image.open(io.BytesIO(chart_image))
        scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
        # A landscape page is much shorter than it is wide - a chart
        # scaled to the full content width can be taller than what's
        # actually left on the page once the heading above and notes
        # below are accounted for. Cap it to whatever room remains and
        # scale the WIDTH down to match instead, keeping the image's
        # own aspect ratio rather than letting it run off the page.
        # Scaled up alongside the Plan/notes font size below each time
        # it's grown (originally 25, then 30 at +2pt, now 39 at the
        # current 30%-larger sizes) so this reserved budget keeps
        # fitting the same number of lines at the new, taller line
        # height - see this function's own font-size-bump history.
        room_for_notes_mm = 39
        max_height_mm = pdf.h - pdf.get_y() - pdf.b_margin - room_for_notes_mm
        if scaled_height_mm > max_height_mm:
            scaled_height_mm = max_height_mm
            image_width_mm = scaled_height_mm * (image.width / image.height)
        else:
            image_width_mm = CONTENT_WIDTH_MM
        x = pdf.l_margin + (CONTENT_WIDTH_MM - image_width_mm) / 2
        pdf.image(image, x=x, w=image_width_mm)
        pdf.ln(3)
    else:
        pdf.set_font("Helvetica", size=17)
        pdf.cell(0, 9, "Chart not available yet.", new_x="LMARGIN", new_y="NEXT")

    # A watchlist ticker's saved "trading plan for next time" (see
    # pages/2_Shortlist.py's render_journal_box()) - never set for an
    # open position, so this is skipped entirely for those without any
    # extra check here.
    plan_entry = entry.get("plan_entry_price")
    plan_stop = entry.get("plan_stop_price")
    metrics = charting.plan_risk_metrics(plan_entry, plan_stop)
    if metrics is not None:
        equity_text = " / ".join(
            f"{allocation}%: {loss:.1f}%" for allocation, loss in metrics["equity_loss_pcts"].items()
        )
        pdf.set_font("Helvetica", style="B", size=16)
        pdf.multi_cell(
            0, 9,
            safe_text(
                f"Plan: Entry ${plan_entry:,.2f}  ·  Stop ${plan_stop:,.2f}  ·  "
                f"Risk to stop {metrics['price_loss_pct']:.1f}%  ·  "
                f"Equity loss at {equity_text} of account"
            ),
        )
        pdf.ln(1)

    if stats_line:
        pdf.set_font("Helvetica", style="B", size=16)
        pdf.multi_cell(0, 9, safe_text(stats_line))
        pdf.ln(1)

    notes_text = entry["notes"].strip() if entry.get("notes") else ""
    pdf.set_font("Helvetica", style="I", size=16)
    pdf.multi_cell(0, 9, safe_text(notes_text or "No notes recorded for this day."))


def build_report_pdf(conn, report_date, missing_charts=None):
    """
    Builds the full report for one day and returns it as PDF bytes: a
    cover page with the date, then one page per ticker - every list
    (Lists 1-4, by their saved names) followed by Open Positions - each
    showing a freshly built chart + that day's saved notes via
    database.get_logbook_entries_for_date(). A list with nothing in it
    is skipped entirely rather than given an empty page.

    If `missing_charts` is a list, the symbol of every ticker whose
    chart couldn't be built gets appended to it.
    """
    names = database.get_watchlist_names(conn)
    watchlist = database.get_watchlist(conn)
    positions = database.get_open_positions(conn)
    # One query for every symbol's saved journal entry on this date,
    # instead of one query per watchlist ticker plus one per open
    # position (a real N+1 pattern that used to make sending laggy).
    as_of = datetime.combine(report_date, datetime.min.time())
    logbook_entries = database.get_logbook_entries_for_date(conn, report_date)

    pdf = DarkReportPDF(format="A4", orientation="L")
    pdf.set_margin(PAGE_MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_MM)
    pdf.add_page()

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=34)
    pdf.cell(0, 22, safe_text(f"Daily Report - {report_date:%B %d, %Y}"), new_x="LMARGIN", new_y="NEXT")

    # "Since your last CSV upload" - a separate, unrelated cutoff from
    # what the Journal Session's Today's Thoughts step now shows (that
    # step is anchored to your last journal date instead - see
    # ui.render_since_last_journal_summary()). Kept here since a report
    # meant to be shareable still shouldn't need a database read of
    # your journal history to build. A LIVE query at report-generation
    # time, not a snapshot of what was shown at upload time, so it
    # always reflects the current "since last upload" window. No
    # quantity or dollar total here either, for the same reason - this
    # report can be looked at by someone else without revealing
    # position size or account value.
    new_trades = database.get_new_trades_since_last_upload(conn)
    if new_trades:
        pdf.ln(4)
        pdf.set_font("Helvetica", style="B", size=20)
        pdf.cell(0, 12, "New Trades Since Last Upload", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=17)
        for t in new_trades:
            verb = database.NEW_TRADE_ACTION_VERBS.get(t["action"], t["action"].lower())
            pdf.cell(
                0, 9, safe_text(f"{t['symbol']} was {verb} on {t['date']:%m/%d/%Y} at ${t['price']:,.2f}"),
                new_x="LMARGIN", new_y="NEXT",
            )

    # The general, not-tied-to-any-ticker note from the guided Journal
    # Session's first step (see pages/2_Shortlist.py's
    # _render_todays_thoughts_step()) - shown on the cover page only if
    # something was actually written; a day journaled without ever
    # opening a session (or one where it was submitted blank) has
    # nothing to show here, so this is skipped rather than printing an
    # empty heading.
    thoughts = database.get_daily_journal_note(conn, report_date)
    if thoughts and thoughts.strip():
        pdf.ln(4)
        pdf.set_font("Helvetica", style="B", size=20)
        pdf.cell(0, 12, "Today's Thoughts", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=17)
        pdf.multi_cell(0, 9, safe_text(thoughts.strip()))

    # Position Management (see position_sizing.py) - only the current
    # tier and the rolling win/loss record, deliberately no dollar
    # amounts or per-trade % figures here, for the same "shareable
    # without revealing account value" reason as New Trades above. The
    # detailed, per-position breakdown lives only in-app on the Open
    # Positions page.
    sizing = position_sizing.evaluate_position_sizing(conn)
    pdf.ln(4)
    pdf.set_font("Helvetica", style="B", size=20)
    pdf.cell(0, 12, "Position Sizing", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=17)
    tier_number = sizing["tier_index"] + 1
    pdf.cell(
        0, 9,
        safe_text(
            f"Tier {tier_number} ({sizing['tier_pct']:.0f}% of ideal size) - "
            f"{sizing['winners']}W / {sizing['losers']}L of last {sizing['window_size']} trades"
        ),
        new_x="LMARGIN", new_y="NEXT",
    )

    # A ticker that's both an open position and on a watchlist only
    # gets its chart built once (positions go first below, so their
    # real "Entry" marker wins over a watchlist "Added" one).
    chart_cache = {}

    def chart_for(symbol, entry_date, buy_price, entry_label, direction="LONG"):
        if symbol not in chart_cache:
            chart_cache[symbol] = _build_chart_image(
                conn, symbol, entry_date, buy_price, entry_label, as_of, direction=direction)
            if chart_cache[symbol] is None and missing_charts is not None:
                missing_charts.append(symbol)
        return chart_cache[symbol]

    for position in positions:
        is_short = position["direction"] == "SHORT"
        chart_for(
            position["symbol"], position["entry_date"], position["avg_price"],
            "Short Entry" if is_short else "Entry", position["direction"])

    for list_id in range(1, 5):
        for w in [w for w in watchlist if w["list_id"] == list_id]:
            symbol = w["symbol"]
            chart = chart_for(symbol, w["added_at"], None, "Added")
            _write_ticker_page(pdf, names[list_id], symbol, logbook_entries.get(symbol), chart)

    # Computed once, not once per position (same N+1 avoidance as
    # the saved entries above) - get_calculated_account_value() already
    # loops every open position and fetches each one's current price on
    # its own, so re-deriving it per position here would fetch every
    # position's price len(positions) times over.
    account_value = charting.get_calculated_account_value(conn)
    for position in positions:
        entry = logbook_entries.get(position["symbol"])
        stats_line = _open_position_stats_line(position, conn, account_value)
        _write_ticker_page(
            pdf, "Open Positions", _position_label(position), entry, chart_cache.get(position["symbol"]),
            stats_line=stats_line)

    return bytes(pdf.output())


def send_report_email(pdf_bytes, report_date):
    """
    Emails the report PDF to every address in REPORT_EMAIL_RECIPIENTS
    (comma-separated) through Gmail's SMTP server, from
    REPORT_EMAIL_FROM using REPORT_EMAIL_APP_PASSWORD. Raises on
    failure - generate_and_send_report() below is what turns that into
    a plain success/failure result instead of a crash.
    """
    from_addr = get_secret("REPORT_EMAIL_FROM")
    app_password = get_secret("REPORT_EMAIL_APP_PASSWORD")
    recipients = [addr.strip() for addr in get_secret("REPORT_EMAIL_RECIPIENTS").split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("REPORT_EMAIL_RECIPIENTS has no valid email addresses in it.")

    msg = EmailMessage()
    msg["Subject"] = f"Daily Trading Report - {report_date:%B %d, %Y}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(f"Attached: the daily trading report for {report_date:%B %d, %Y}.")
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf",
        filename=f"daily_report_{report_date:%Y-%m-%d}.pdf",
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


def generate_and_send_report(conn, report_date):
    """
    Builds and emails the report for one day, then records it in
    database.daily_reports so the Journal Session screen can show it
    was sent. Never raises - returns (success, message) so the caller
    can show the result the same simple way either way. The success
    message names any tickers whose chart couldn't be built yet.
    """
    missing_charts = []
    try:
        pdf_bytes = build_report_pdf(conn, report_date, missing_charts=missing_charts)
        send_report_email(pdf_bytes, report_date)
    except Exception as exc:
        return False, f"Could not generate/send the report: {exc}"

    database.mark_daily_report_generated(conn, report_date)
    message = "Report generated and emailed."
    if missing_charts:
        message += (
            f" No chart yet for {', '.join(missing_charts)} (Yahoo Finance hasn't "
            "published today's close for them) - those pages say so."
        )
    return True, message
