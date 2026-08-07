"""
Symbol Archive Report
=====================
Storage-management job: once a WATCHLIST-ONLY ticker (never actually
traded - see database.has_trade_history()) has been removed from every
watchlist for more than GRACE_PERIOD_DAYS, its whole Logbook history
(every day's notes + chart image) gets bundled into one PDF, emailed
to ARCHIVE_EMAIL_RECIPIENTS, and then permanently deleted from the
database - see database.delete_symbol_logbook_history(). The PDF
becomes that ticker's only remaining record.

A real trade's Logbook is NEVER touched by this, no matter how long
ago it closed or was removed from watchlist - only symbols that were
just a passing watchlist idea and never turned into an actual trade
are eligible. This exists because archived chart images are, by a
wide margin, the biggest thing in this database (see charting.
render_png()'s own docstring) - a watchlist ticker you decided not to
trade has no ongoing reason to keep costing storage forever once
you've moved on from it, but the PDF means nothing is silently lost -
just moved out of the live database and into your inbox.

Called from nightly_archive.py, same "runs automatically, no button
needed" shape as everything else there. Reuses daily_report.py's own
REPORT_EMAIL_FROM/REPORT_EMAIL_APP_PASSWORD (same mailbox) but a
SEPARATE recipient list (ARCHIVE_EMAIL_RECIPIENTS) - these go
somewhere different than the daily report, not necessarily to the
same audience.
"""

import io
import smtplib
from email.message import EmailMessage

from fpdf import FPDF
from PIL import Image

import charting
import database
from report_utils import get_secret, hex_to_rgb, safe_text

GRACE_PERIOD_DAYS = 7

PAGE_MARGIN_MM = 15
CONTENT_WIDTH_MM = 297 - 2 * PAGE_MARGIN_MM

PAGE_BACKGROUND_RGB = hex_to_rgb(charting.CHART_BACKGROUND)
TEXT_COLOR_RGB = hex_to_rgb(charting.CHART_TEXT_COLOR)
MUTED_TEXT_RGB = hex_to_rgb(charting.MUTED_COLOR)


class DarkReportPDF(FPDF):
    """Same dark-page-background trick as daily_report.py's own
    DarkReportPDF - kept as a separate copy (not imported) since fpdf2
    ties header() to the specific PDF instance/class, not worth a
    shared base class for four lines."""
    def header(self):
        self.set_fill_color(*PAGE_BACKGROUND_RGB)
        self.rect(0, 0, self.w, self.h, "F")


def _write_entry_page(pdf, symbol, entry):
    """
    One day's page: the date as a heading, that day's chart image (or
    a note that none was archived), and that day's notes - the same
    pieces daily_report.py's _write_ticker_page() shows, just iterating
    over ONE symbol's dates instead of one date's symbols.
    """
    pdf.add_page()

    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=17)
    pdf.cell(0, 10, safe_text(symbol), new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=26)
    pdf.cell(0, 16, safe_text(f"{entry['entry_date']:%B %d, %Y}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if entry["chart_image"]:
        image = Image.open(io.BytesIO(entry["chart_image"]))
        scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
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
        pdf.cell(0, 9, "No chart archived for this day.", new_x="LMARGIN", new_y="NEXT")

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

    notes_text = entry["notes"].strip() if entry["notes"] else ""
    pdf.set_font("Helvetica", style="I", size=16)
    pdf.multi_cell(0, 9, safe_text(notes_text or "No notes recorded for this day."))


def build_symbol_archive_pdf(conn, symbol, entries):
    """
    Builds the full archive for one symbol: a cover page (symbol, date
    range, entry count) then one page per logbook entry, oldest first.
    `entries` is whatever database.get_logbook_entries_for_symbol()
    returned - passed in rather than re-queried so the caller can log/
    inspect it before deciding to actually send+delete.
    """
    pdf = DarkReportPDF(format="A4", orientation="L")
    pdf.set_margin(PAGE_MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_MM)
    pdf.add_page()

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=34)
    pdf.cell(0, 22, safe_text(f"{symbol} - Watchlist Archive"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", size=18)
    date_range = f"{entries[0]['entry_date']:%B %d, %Y} - {entries[-1]['entry_date']:%B %d, %Y}"
    pdf.ln(4)
    pdf.multi_cell(
        0, 10,
        safe_text(
            f"{len(entries)} journal {'entry' if len(entries) == 1 else 'entries'} ({date_range}). "
            f"Removed from watchlist and not re-added for over {GRACE_PERIOD_DAYS} days - "
            "archived here and removed from the live database to keep it from growing forever."
        ),
    )

    for entry in entries:
        _write_entry_page(pdf, symbol, entry)

    return bytes(pdf.output())


def send_symbol_archive_email(pdf_bytes, symbol):
    """
    Emails the archive PDF to every address in ARCHIVE_EMAIL_RECIPIENTS
    (comma-separated) - same sending mailbox as daily_report.py
    (REPORT_EMAIL_FROM/REPORT_EMAIL_APP_PASSWORD), different recipient
    list. Raises on failure, same contract as daily_report.py's own
    send_report_email() - the caller decides what "failure" means for
    the delete-or-not decision.
    """
    from_addr = get_secret("REPORT_EMAIL_FROM")
    app_password = get_secret("REPORT_EMAIL_APP_PASSWORD")
    recipients = [addr.strip() for addr in get_secret("ARCHIVE_EMAIL_RECIPIENTS").split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("ARCHIVE_EMAIL_RECIPIENTS has no valid email addresses in it.")

    msg = EmailMessage()
    msg["Subject"] = f"Watchlist Archive - {symbol}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        f"{symbol} was removed from your watchlist over {GRACE_PERIOD_DAYS} days ago and hasn't been "
        "re-added since. Its full journal history is attached, and has now been deleted from the app."
    )
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf",
        filename=f"{symbol}_watchlist_archive.pdf",
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


def _archive_and_delete_one_symbol(conn, symbol):
    """
    The actual per-symbol decision: skip anything with real trade
    history (database.has_trade_history() - Logbooks for actual trades
    are never auto-deleted); for everything else, build+email the PDF
    and ONLY THEN delete it (database.delete_symbol_logbook_history())
    - a failed email leaves the symbol's data untouched, to be retried
    on the next nightly run, never deleted without a successful send
    first. Returns a short string describing what happened (for the
    caller's own log line) - split out from
    archive_and_delete_stale_watchlist_symbols() below specifically so
    it can be tested against ONE known-fake symbol without that
    function's own database.get_stale_watchlist_removals() call
    sweeping in whatever real symbols happen to already be stale in
    production at test time.
    """
    if database.has_trade_history(conn, symbol):
        return "has real trade history, exempt - leaving its Logbook alone."

    entries = database.get_logbook_entries_for_symbol(conn, symbol)
    if not entries:
        # Nothing to archive (e.g. added and removed the same week,
        # before ever being journaled) - just clear the tracking row
        # so this symbol stops being rechecked.
        database.delete_symbol_logbook_history(conn, symbol)
        return "no logbook entries to archive, tracking row cleared."

    pdf_bytes = build_symbol_archive_pdf(conn, symbol, entries)
    send_symbol_archive_email(pdf_bytes, symbol)
    database.delete_symbol_logbook_history(conn, symbol)
    return f"archived {len(entries)} entries, emailed, and deleted."


def archive_and_delete_stale_watchlist_symbols(conn):
    """
    The actual nightly job: finds every symbol removed from watchlist
    more than GRACE_PERIOD_DAYS ago (database.get_stale_watchlist_
    removals()) and runs _archive_and_delete_one_symbol() on each.

    Prints one line per symbol so nightly_archive.py's GitHub Actions
    log shows what happened, same style as archiving.archive_all().
    Never raises - one bad symbol (a malformed image, an SMTP hiccup)
    shouldn't stop the rest from being processed, matching
    archiving.archive_all()'s own per-ticker try/except pattern.
    """
    stale_symbols = database.get_stale_watchlist_removals(conn, GRACE_PERIOD_DAYS)
    print(f"Found {len(stale_symbols)} symbol(s) removed from watchlist over {GRACE_PERIOD_DAYS} days ago.")

    for symbol in stale_symbols:
        try:
            print(f"  {symbol}: {_archive_and_delete_one_symbol(conn, symbol)}")
        except Exception as exc:
            print(f"  {symbol}: archiving failed unexpectedly ({exc}), leaving it for the next run.")
