"""
Daily Report
=====================
Builds one PDF covering every list on the Shortlist page - Lists 1-4
(by their saved names) plus Open Positions - with each ticker's
archived chart image and journal notes for a given day, and emails it
to a mailing list. Reads exactly the same data the Logbook page
already shows per symbol (database.get_logbook_entry()), just
assembled into one file instead of browsed one ticker at a time.

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
import database

PAGE_MARGIN_MM = 15
CONTENT_WIDTH_MM = 210 - 2 * PAGE_MARGIN_MM  # A4 width minus left/right margins


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


def _ensure_room(pdf, needed_height_mm):
    """Starts a new page first if `needed_height_mm` of content
    wouldn't fit above the bottom margin - fpdf2's automatic page break
    only triggers on a full cell/line write, which would let a large
    chart image get cut off mid-image across a page boundary."""
    if pdf.get_y() + needed_height_mm > pdf.h - pdf.b_margin:
        pdf.add_page()


def _write_ticker_section(pdf, symbol, entry):
    """One ticker's block: its symbol as a sub-heading, then its chart
    image (or a note that it isn't archived yet) and its notes (or a
    note that none were recorded) - mirroring exactly what the Logbook
    page already shows for one day."""
    pdf.set_font("Helvetica", style="B", size=12)
    _ensure_room(pdf, 10)
    pdf.cell(0, 8, _safe_text(symbol), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)

    if entry is None:
        pdf.cell(0, 6, "Not archived yet.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        return

    if entry["chart_image"]:
        image = Image.open(io.BytesIO(entry["chart_image"]))
        scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
        _ensure_room(pdf, scaled_height_mm)
        # fpdf2's image() already advances the cursor by the image's
        # own height - adding scaled_height_mm again here would double
        # it and leave a large blank gap before the notes below.
        pdf.image(image, x=pdf.l_margin, w=CONTENT_WIDTH_MM)
        pdf.ln(2)
    else:
        pdf.cell(0, 6, "No chart archived for this day yet.", new_x="LMARGIN", new_y="NEXT")

    notes_text = entry["notes"].strip() if entry["notes"] else ""
    pdf.set_font("Helvetica", style="I", size=10)
    pdf.multi_cell(0, 6, _safe_text(notes_text or "No notes recorded for this day."))
    pdf.ln(4)


def build_report_pdf(conn, report_date):
    """
    Builds the full report for one day and returns it as PDF bytes:
    one section per watchlist (its saved name from
    database.get_watchlist_names()) listing every ticker currently in
    it, then an Open Positions section from
    database.get_open_positions() - each ticker showing that day's
    archived chart + notes via database.get_logbook_entry().
    """
    names = database.get_watchlist_names(conn)
    watchlist = database.get_watchlist(conn)
    positions = database.get_open_positions(conn)

    pdf = FPDF(format="A4")
    pdf.set_margin(PAGE_MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_MM)
    pdf.add_page()

    pdf.set_font("Helvetica", style="B", size=18)
    pdf.cell(0, 12, _safe_text(f"Daily Report - {report_date:%B %d, %Y}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    for list_id in range(1, 5):
        symbols = [w["symbol"] for w in watchlist if w["list_id"] == list_id]
        pdf.set_font("Helvetica", style="B", size=14)
        _ensure_room(pdf, 12)
        pdf.cell(0, 10, _safe_text(names[list_id]), new_x="LMARGIN", new_y="NEXT")
        if not symbols:
            pdf.set_font("Helvetica", size=10)
            pdf.cell(0, 6, "No tickers in this list.", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
            continue
        for symbol in symbols:
            _write_ticker_section(pdf, symbol, database.get_logbook_entry(conn, symbol, report_date))

    pdf.set_font("Helvetica", style="B", size=14)
    _ensure_room(pdf, 12)
    pdf.cell(0, 10, "Open Positions", new_x="LMARGIN", new_y="NEXT")
    if not positions:
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 6, "No open positions right now.", new_x="LMARGIN", new_y="NEXT")
    else:
        for position in positions:
            entry = database.get_logbook_entry(conn, position["symbol"], report_date)
            _write_ticker_section(pdf, _position_label(position), entry)

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
