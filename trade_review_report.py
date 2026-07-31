"""
Trade Review Report
=====================
Builds one PDF for a single Trade Review Report (see pages/1_Trade_
Analyzer.py's guided Review Session) and emails it - the same idea as
daily_report.py, just covering a batch of CLOSED trades reviewed
together instead of "today's" open positions/watchlist. Reads exactly
what database.get_review_report() already returns (the same data the
Logbook page's Trade Reviews section browses), assembled into one file.

Landscape, dark-themed, same shared report_utils.py pieces
(get_secret/safe_text/hex_to_rgb) daily_report.py uses - the actual
page-building code is NOT shared beyond that, since the two reports'
layout (one page per ticker there, one header page PLUS one page per
captured timeframe snapshot here) is different enough that forcing a
shared page-builder would cost more readability than it'd save.

Unlike a chart snapshot, a trade can have SEVERAL of them (Hourly,
Daily, Weekly, ...) - each gets its own full page rather than being
crammed several-to-a-page, so none of them end up too small to read.

Reuses the SAME REPORT_EMAIL_* secrets as daily_report.py (same mailbox,
same recipients) - no reason for a second set of email config.
"""

import io
import smtplib
from email.message import EmailMessage

from fpdf import FPDF
from PIL import Image

import charting
import database
from report_utils import get_secret, hex_to_rgb, safe_text

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


def _write_snapshot_page(pdf, symbol, timeframe, chart_image):
    """One full page for a single captured timeframe snapshot - a trade
    with three saved timeframes gets three of these, each full-size,
    rather than shrinking all three onto one page until they're too
    small to actually read."""
    pdf.add_page()
    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 7, safe_text(f"{symbol} - {timeframe}"), new_x="LMARGIN", new_y="NEXT")

    image = Image.open(io.BytesIO(chart_image))
    scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
    # Same "cap to remaining room, scale width to match" logic as
    # daily_report.py's _write_ticker_page() - a landscape page is much
    # shorter than it is wide.
    max_height_mm = pdf.h - pdf.get_y() - pdf.b_margin
    if scaled_height_mm > max_height_mm:
        scaled_height_mm = max_height_mm
        image_width_mm = scaled_height_mm * (image.width / image.height)
    else:
        image_width_mm = CONTENT_WIDTH_MM
    x = pdf.l_margin + (CONTENT_WIDTH_MM - image_width_mm) / 2
    pdf.image(image, x=x, w=image_width_mm)


def _write_trade_review_page(pdf, review):
    """One reviewed trade's header page (symbol/direction, entry/exit
    dates, notes), followed by one additional page per captured
    timeframe snapshot."""
    pdf.add_page()
    short_tag = " (Short)" if review["direction"] == "SHORT" else ""

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=18)
    pdf.cell(0, 11, safe_text(f"{review['symbol']}{short_tag}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(
        0, 7, safe_text(f"{review['entry_date']:%m/%d/%Y} to {review['exit_date']:%m/%d/%Y}"),
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(4)

    notes_text = review["notes"].strip() if review["notes"] else ""
    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="I", size=12)
    pdf.multi_cell(0, 7, safe_text(notes_text or "No notes recorded."))

    if not review["snapshots"]:
        pdf.ln(4)
        pdf.set_text_color(*MUTED_TEXT_RGB)
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 6, "No chart snapshots were saved for this trade.", new_x="LMARGIN", new_y="NEXT")

    for timeframe, chart_image in review["snapshots"].items():
        _write_snapshot_page(pdf, review["symbol"], timeframe, chart_image)


def build_review_report_pdf(conn, report_id):
    """
    Builds one Trade Review Report as PDF bytes: a cover page (date
    range, how many trades), then each reviewed trade's own page(s) -
    see _write_trade_review_page(). Raises ValueError if this report
    doesn't exist (a stale/deleted report_id).
    """
    report = database.get_review_report(conn, report_id)
    if report is None:
        raise ValueError(f"No trade review report with id {report_id}.")

    pdf = DarkReportPDF(format="A4", orientation="L")
    pdf.set_margin(PAGE_MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_MM)
    pdf.add_page()

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=24)
    pdf.cell(0, 16, safe_text("Trade Review Report"), new_x="LMARGIN", new_y="NEXT")

    if report["range_start"] and report["range_end"]:
        range_text = f"{report['range_start']:%m/%d/%Y} to {report['range_end']:%m/%d/%Y}"
    else:
        range_text = "All trades"
    pdf.set_font("Helvetica", size=13)
    pdf.cell(
        0, 9, safe_text(f"{range_text} - {len(report['reviews'])} trade(s) reviewed"),
        new_x="LMARGIN", new_y="NEXT",
    )

    for review in report["reviews"]:
        _write_trade_review_page(pdf, review)

    return bytes(pdf.output())


def send_review_report_email(pdf_bytes, report):
    """
    Emails the report PDF to every address in REPORT_EMAIL_RECIPIENTS,
    through the same Gmail SMTP setup daily_report.py uses (same
    secrets - REPORT_EMAIL_FROM/REPORT_EMAIL_APP_PASSWORD, same mailbox
    and recipient list, no reason for a second set of email config).
    Raises on failure - generate_and_send_review_report() below is what
    turns that into a plain success/failure result instead of a crash.
    """
    from_addr = get_secret("REPORT_EMAIL_FROM")
    app_password = get_secret("REPORT_EMAIL_APP_PASSWORD")
    recipients = [addr.strip() for addr in get_secret("REPORT_EMAIL_RECIPIENTS").split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("REPORT_EMAIL_RECIPIENTS has no valid email addresses in it.")

    msg = EmailMessage()
    msg["Subject"] = f"Trade Review Report - {report['created_at']:%B %d, %Y}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(f"Attached: a trade review report covering {len(report['reviews'])} trade(s).")
    msg.add_attachment(
        pdf_bytes, maintype="application", subtype="pdf",
        filename=f"trade_review_report_{report['id']}.pdf",
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


def generate_and_send_review_report(conn, report_id):
    """
    Builds and emails one Trade Review Report, then records it as sent
    (see database.mark_review_report_sent()) so the Logbook page's
    status caption reflects it. Never raises - returns (success,
    message), same contract as daily_report.generate_and_send_report().
    """
    try:
        pdf_bytes = build_review_report_pdf(conn, report_id)
        report = database.get_review_report(conn, report_id)
        send_review_report_email(pdf_bytes, report)
    except Exception as exc:
        return False, f"Could not generate/send the report: {exc}"

    database.mark_review_report_sent(conn, report_id)
    return True, "Report generated and emailed."
