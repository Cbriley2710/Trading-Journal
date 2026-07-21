"""
Daily Report
=====================
Builds one PDF covering every list on the Shortlist page - Lists 1-4
(by their saved names) plus Open Positions - with each ticker's
archived chart image and journal notes for a given day, and emails it
to a mailing list. Reads exactly the same data the Logbook page
already shows per symbol (database.get_logbook_entry()), just
assembled into one file instead of browsed one ticker at a time.

Landscape, one page per ticker, dark-themed to match the chart images
themselves (charting.py's own CHART_BACKGROUND/CHART_TEXT_COLOR/
MUTED_COLOR) - a chart archived from this app is already a wide,
dark-background image, so a landscape page fits it without shrinking
it down, and a dark page background means there's no bright white
border around a dark chart.

Two things call generate_and_send_report() below: the "Generate &
Email Report" button on the Logbook page (always runs when clicked),
and nightly_archive.py (only runs if nothing is recorded yet for today
in the database.daily_reports table) - the same "manual action is
primary, the nightly script is the fallback" pattern this project
already uses for per-ticker chart archiving.

SENDING EMAIL: through Gmail's SMTP server, which needs a Google
Account "App Password" (NOT your regular Gmail password - Gmail
stopped accepting that for this years ago). Generate one, once, at
myaccount.google.com/apppasswords (requires 2-Step Verification to be
turned on first), then put it in REPORT_EMAIL_APP_PASSWORD below -
never your actual account password.
"""

import io
import os
import smtplib
from email.message import EmailMessage

import streamlit as st
from fpdf import FPDF
from PIL import Image

import archiving
import charting
import database

PAGE_MARGIN_MM = 15
# A4 LANDSCAPE is 297mm x 210mm (width and height swapped from the
# usual portrait 210mm x 297mm) - one wide chart per page instead of
# several narrower ones stacked on a tall portrait page.
CONTENT_WIDTH_MM = 297 - 2 * PAGE_MARGIN_MM


def _hex_to_rgb(hex_color):
    """"#RRGGBB" -> (r, g, b) ints - fpdf2's set_fill_color()/
    set_text_color() want separate 0-255 numbers, not a hex string."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


PAGE_BACKGROUND_RGB = _hex_to_rgb(charting.CHART_BACKGROUND)
TEXT_COLOR_RGB = _hex_to_rgb(charting.CHART_TEXT_COLOR)
MUTED_TEXT_RGB = _hex_to_rgb(charting.MUTED_COLOR)


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


def _get_secret(key):
    """Reads a secret from st.secrets first, falling back to a plain
    environment variable - the same dual lookup
    database._get_database_url() uses, needed here too since this
    code runs both inside Streamlit (the button) and inside the plain
    nightly_archive.py script (GitHub Actions, no st.secrets file)."""
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


def _safe_text(text):
    """fpdf2's built-in fonts only support Latin-1 - if a journal note
    ever has a character outside that (an emoji, say), swap it for a
    "?" instead of crashing the whole report over one character."""
    return text.encode("latin-1", "replace").decode("latin-1")


def _position_label(position):
    """Same "(Short)" tagging convention used on the Shortlist and Open
    Positions pages, so a short position is never mistaken for a long
    one in the report either."""
    return f"{position['symbol']} (Short)" if position["direction"] == "SHORT" else position["symbol"]


def _write_ticker_page(pdf, section_label, symbol, entry):
    """
    One ticker's own page: a small "which list" breadcrumb, the symbol
    as a heading, its archived chart image (or a note that it isn't
    archived yet), and its notes (or a note that none were recorded) -
    the same pieces the Logbook page shows for one day, just given a
    full page each instead of flowing several onto a shared one.
    """
    pdf.add_page()

    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 7, _safe_text(section_label), new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=18)
    pdf.cell(0, 11, _safe_text(symbol), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if entry is None:
        pdf.set_font("Helvetica", size=11)
        pdf.cell(0, 6, "Not archived yet.", new_x="LMARGIN", new_y="NEXT")
        return

    if entry["chart_image"]:
        image = Image.open(io.BytesIO(entry["chart_image"]))
        scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
        # A landscape page is much shorter than it is wide - a chart
        # scaled to the full content width can be taller than what's
        # actually left on the page once the heading above and notes
        # below are accounted for. Cap it to whatever room remains and
        # scale the WIDTH down to match instead, keeping the image's
        # own aspect ratio rather than letting it run off the page.
        room_for_notes_mm = 25
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
        pdf.set_font("Helvetica", size=11)
        pdf.cell(0, 6, "No chart archived for this day yet.", new_x="LMARGIN", new_y="NEXT")

    notes_text = entry["notes"].strip() if entry["notes"] else ""
    pdf.set_font("Helvetica", style="I", size=10)
    pdf.multi_cell(0, 6, _safe_text(notes_text or "No notes recorded for this day."))


def build_report_pdf(conn, report_date):
    """
    Builds the full report for one day and returns it as PDF bytes: a
    cover page with the date, then one page per ticker - every list
    (Lists 1-4, by their saved names) followed by Open Positions - each
    showing that day's archived chart + notes via
    database.get_logbook_entry(). A list with nothing in it is skipped
    entirely rather than given an empty page.
    """
    names = database.get_watchlist_names(conn)
    watchlist = database.get_watchlist(conn)
    positions = database.get_open_positions(conn)

    pdf = DarkReportPDF(format="A4", orientation="L")
    pdf.set_margin(PAGE_MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_MM)
    pdf.add_page()

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=24)
    pdf.cell(0, 16, _safe_text(f"Daily Report - {report_date:%B %d, %Y}"), new_x="LMARGIN", new_y="NEXT")

    for list_id in range(1, 5):
        symbols = [w["symbol"] for w in watchlist if w["list_id"] == list_id]
        for symbol in symbols:
            _write_ticker_page(
                pdf, names[list_id], symbol, database.get_logbook_entry(conn, symbol, report_date))

    for position in positions:
        entry = database.get_logbook_entry(conn, position["symbol"], report_date)
        _write_ticker_page(pdf, "Open Positions", _position_label(position), entry)

    return bytes(pdf.output())


def send_report_email(pdf_bytes, report_date):
    """
    Emails the report PDF to every address in REPORT_EMAIL_RECIPIENTS
    (comma-separated) through Gmail's SMTP server, from
    REPORT_EMAIL_FROM using REPORT_EMAIL_APP_PASSWORD. Raises on
    failure - generate_and_send_report() below is what turns that into
    a plain success/failure result instead of a crash.
    """
    from_addr = _get_secret("REPORT_EMAIL_FROM")
    app_password = _get_secret("REPORT_EMAIL_APP_PASSWORD")
    recipients = [addr.strip() for addr in _get_secret("REPORT_EMAIL_RECIPIENTS").split(",") if addr.strip()]
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


def generate_and_send_report(conn, report_date, archive_first=False):
    """
    Builds and emails the report for one day, then records it in
    database.daily_reports so nightly_archive.py's fallback knows not
    to also send one. Never raises - returns (success, message) so
    both the Logbook page's button and the nightly script can show/log
    the result the same simple way.

    If archive_first is True, archives a fresh chart for every open
    position and watchlist ticker (via archiving.archive_all()) before
    building the PDF - this is what lets the Logbook page's button do
    everything the nightly job would have done, on demand, instead of
    only using whatever charts happened to already be archived. Only
    meaningful for today: a past report_date can't retroactively get a
    same-day snapshot, so the Logbook page only passes True when the
    selected date is today.
    """
    try:
        if archive_first:
            archiving.archive_all(conn, report_date)
        pdf_bytes = build_report_pdf(conn, report_date)
        send_report_email(pdf_bytes, report_date)
    except Exception as exc:
        return False, f"Could not generate/send the report: {exc}"

    database.mark_daily_report_generated(conn, report_date)
    return True, "Report generated and emailed."
