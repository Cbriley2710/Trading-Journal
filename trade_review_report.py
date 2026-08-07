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
from analyze_trades import review_session_summary, trade_stats
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


def _draw_chart_image(pdf, chart_image, room_below_mm=0):
    """
    Draws one chart snapshot at the current page position, scaled to
    fit the page width - or, if that would run past the bottom margin
    (minus `room_below_mm`, reserved for whatever's meant to follow on
    the SAME page, e.g. notes text), scaled down to whatever height
    actually remains instead. Same "cap to remaining room, scale width
    to match" logic as daily_report.py's _write_ticker_page() - a
    landscape page is much shorter than it is wide. Shared by the
    trade-review header page's primary snapshot (see
    _write_trade_review_page()) and _write_snapshot_page() below, which
    just pass different `room_below_mm`.
    """
    image = Image.open(io.BytesIO(chart_image))
    scaled_height_mm = CONTENT_WIDTH_MM * (image.height / image.width)
    max_height_mm = pdf.h - pdf.get_y() - pdf.b_margin - room_below_mm
    if scaled_height_mm > max_height_mm:
        scaled_height_mm = max_height_mm
        image_width_mm = scaled_height_mm * (image.width / image.height)
    else:
        image_width_mm = CONTENT_WIDTH_MM
    x = pdf.l_margin + (CONTENT_WIDTH_MM - image_width_mm) / 2
    # pdf.image() already advances the cursor to just below the placed
    # image on its own (same as daily_report.py's own _write_ticker_
    # page() relies on) - an extra manual pdf.set_y() here would double
    # the advance, pushing whatever's meant to follow on this SAME page
    # (the notes text, in _write_trade_review_page()) far enough down
    # to trigger its own unwanted page break.
    pdf.image(image, x=x, w=image_width_mm)


def _write_snapshot_page(pdf, symbol, timeframe, chart_image):
    """One full page for a single captured timeframe snapshot - a trade
    with several saved timeframes gets one of these for every timeframe
    BEYOND the primary one already shown on the trade's own header page
    (see _write_trade_review_page()), each full-size, rather than
    shrinking all of them onto one page until they're too small to
    actually read."""
    pdf.add_page()
    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=17)
    pdf.cell(0, 10, safe_text(f"{symbol} - {timeframe}"), new_x="LMARGIN", new_y="NEXT")
    _draw_chart_image(pdf, chart_image)


def _write_trade_review_page(pdf, review, jan1_balance):
    """One reviewed trade's header page (symbol/direction, entry/exit
    dates, stats, its PRIMARY chart snapshot, and notes all together on
    this same page - see the primary-snapshot selection below), followed
    by one additional page per any OTHER captured timeframe. `jan1_
    balance` (database.get_account_value()) is what Equity Contribution
    divides this trade's profit_loss by - see trade_stats()'s own
    docstring for why; left off the stats line entirely if it hasn't
    been set yet, same as the other two places this same math is shown
    (Trade Analyzer, Logbook)."""
    pdf.add_page()
    short_tag = " (Short)" if review["direction"] == "SHORT" else ""

    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=26)
    pdf.cell(0, 16, safe_text(f"{review['symbol']}{short_tag}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.set_font("Helvetica", size=17)
    pdf.cell(
        0, 10, safe_text(f"{review['entry_date']:%m/%d/%Y} to {review['exit_date']:%m/%d/%Y}"),
        new_x="LMARGIN", new_y="NEXT",
    )

    # Same numbers as Trade Analyzer's fact tiles (see
    # analyze_trades.trade_stats()), just as two plain text lines
    # instead of side-by-side tiles - a landscape PDF page has plenty
    # of width but fpdf2 has no equivalent "column of tiles" widget.
    stats = trade_stats(
        review["direction"], review["buy_price"], review["sell_price"], review["quantity"],
        review["profit_loss"], review["entry_date"], review["exit_date"],
        account_value=jan1_balance,
    )
    entry_label = "Short Entry" if stats["is_short"] else "Entry"
    exit_label = "Cover" if stats["is_short"] else "Exit"
    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.cell(
        0, 10,
        safe_text(
            f"{entry_label}: ${stats['entry_price']:,.2f}   {exit_label}: ${stats['exit_price']:,.2f}   "
            f"Shares: {review['quantity']:,.0f}   Days Held: {stats['days_held']}"
        ),
        new_x="LMARGIN", new_y="NEXT",
    )
    outcome_rgb = hex_to_rgb(charting.win_loss_color(review["profit_loss"] >= 0))
    pdf.set_text_color(*outcome_rgb)
    pdf.set_font("Helvetica", style="B", size=17)
    pl_line = f"P/L: ${review['profit_loss']:,.2f}  ({stats['pct_change']:,.2f}%)"
    if stats["equity_contribution"] is not None:
        pl_line += f"   Equity Contribution: {stats['equity_contribution']:,.2f}%"
    pdf.cell(0, 10, safe_text(pl_line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # The PRIMARY snapshot goes right here, on this same page - "Daily"
    # if it was saved (always is; see the Review Session's guaranteed
    # baseline capture in pages/1_Trade_Analyzer.py), otherwise
    # whichever single timeframe exists. This is the fix for the chart
    # and its own data/notes previously always landing on different
    # pages: _write_snapshot_page() used to run for EVERY captured
    # timeframe, including this guaranteed first one, and it always
    # starts a fresh page. `room_below_mm` reserves space for the notes
    # that follow, the same "cap the image, not the text" approach
    # daily_report.py's _write_ticker_page() already uses.
    remaining_snapshots = dict(review["snapshots"])
    primary_timeframe = "Daily" if "Daily" in remaining_snapshots else next(iter(remaining_snapshots), None)
    if primary_timeframe:
        primary_image = remaining_snapshots.pop(primary_timeframe)
        # Scaled up alongside the notes font size below each time it's
        # grown (originally 30, then 35 at +2pt, now 46 at the current
        # 30%-larger sizes) so this reserved budget keeps fitting the
        # same number of lines at the new, taller line height.
        _draw_chart_image(pdf, primary_image, room_below_mm=46)
        pdf.ln(3)

    notes_text = review["notes"].strip() if review["notes"] else ""
    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="I", size=18)
    pdf.multi_cell(0, 10, safe_text(notes_text or "No notes recorded."))

    if not review["snapshots"]:
        pdf.ln(4)
        pdf.set_text_color(*MUTED_TEXT_RGB)
        pdf.set_font("Helvetica", size=16)
        pdf.cell(0, 9, "No chart snapshots were saved for this trade.", new_x="LMARGIN", new_y="NEXT")

    # Any OTHER captured timeframes beyond the primary one still get
    # their own full page each.
    for timeframe, chart_image in remaining_snapshots.items():
        _write_snapshot_page(pdf, review["symbol"], timeframe, chart_image)


def _write_review_summary_section(pdf, summary):
    """
    Writes the same Session Summary shown on the in-app Reflections
    screen (see pages/1_Trade_Analyzer.py's _render_review_summary(),
    both built from analyze_trades.review_session_summary()) onto the
    emailed PDF's own Reflections page - batting average, win/loss $
    and %, and a per-ticker breakdown table. Placed right above the
    Reflections notes themselves, so the numbers that prompted them are
    visible on the same page they were written against.
    """
    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", style="B", size=21)
    pdf.cell(0, 13, safe_text("Session Summary"), new_x="LMARGIN", new_y="NEXT")

    win_rgb = hex_to_rgb(charting.win_loss_color(True))
    loss_rgb = hex_to_rgb(charting.win_loss_color(False))

    pdf.set_font("Helvetica", size=17)
    pdf.set_text_color(*MUTED_TEXT_RGB)
    pdf.cell(
        0, 10,
        safe_text(
            f"Batting Average: {summary['batting_avg']:.1f}%   "
            f"Wins: {summary['win_count']}   Losses: {summary['loss_count']}"
        ),
        new_x="LMARGIN", new_y="NEXT",
    )

    pdf.set_font("Helvetica", style="B", size=17)
    pdf.set_text_color(*(win_rgb if summary["total_pl"] >= 0 else loss_rgb))
    pdf.cell(0, 10, safe_text(f"Total P/L: ${summary['total_pl']:,.2f}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", size=16)
    pdf.set_text_color(*win_rgb)
    win_line = (
        f"Avg Win: ${summary['avg_win_dollar']:,.2f} ({summary['avg_win_pct']:,.2f}%)"
        if summary["avg_win_dollar"] is not None else "Avg Win: -"
    )
    pdf.cell(0, 9, safe_text(win_line), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*loss_rgb)
    loss_line = (
        f"Avg Loss: ${summary['avg_loss_dollar']:,.2f} ({summary['avg_loss_pct']:,.2f}%)"
        if summary["avg_loss_dollar"] is not None else "Avg Loss: -"
    )
    pdf.cell(0, 9, safe_text(loss_line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    per_ticker_rows = sorted(summary["per_ticker"].items(), key=lambda kv: kv[1]["net_pl"], reverse=True)
    pdf.set_text_color(*TEXT_COLOR_RGB)
    pdf.set_font("Helvetica", size=16)
    with pdf.table(
        # Widened alongside the table's own font size each time it's
        # grown (originally 0.6, then 0.72 at +2pt, now 0.94 at the
        # current 30%-larger size) so a value like "$12,345.67" keeps
        # the same relative breathing room in its column instead of
        # being more likely to wrap onto a second line now that it's
        # bigger.
        col_widths=(2, 1, 1, 1, 1.3, 1.3), text_align="LEFT",
        borders_layout="NONE", width=CONTENT_WIDTH_MM * 0.94,
    ) as table:
        header_row = table.row()
        for label in ("Symbol", "Trades", "Wins", "Losses", "Win Rate", "Net P/L"):
            header_row.cell(safe_text(label))
        for symbol, row in per_ticker_rows:
            losses = row["trades"] - row["wins"]
            win_rate = f"{row['wins'] / row['trades'] * 100:.0f}%"
            table_row = table.row()
            table_row.cell(safe_text(symbol))
            table_row.cell(str(row["trades"]))
            table_row.cell(str(row["wins"]))
            table_row.cell(str(losses))
            table_row.cell(win_rate)
            table_row.cell(safe_text(f"${row['net_pl']:,.2f}"))
    pdf.ln(4)


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
    pdf.set_font("Helvetica", style="B", size=34)
    pdf.cell(0, 22, safe_text("Trade Review Report"), new_x="LMARGIN", new_y="NEXT")

    if report["range_start"] and report["range_end"]:
        range_text = f"{report['range_start']:%m/%d/%Y} to {report['range_end']:%m/%d/%Y}"
    else:
        range_text = "All trades"
    pdf.set_font("Helvetica", size=20)
    pdf.cell(
        0, 13, safe_text(f"{range_text} - {len(report['reviews'])} trade(s) reviewed"),
        new_x="LMARGIN", new_y="NEXT",
    )

    # The session's opening journal entry (see database.
    # save_review_predictions_notes()) - on the cover page rather than
    # its own page, since it's a quick "how did I feel going in" note,
    # not something that needs a whole page the way Reflections does.
    if report["predictions_notes"]:
        pdf.ln(4)
        pdf.set_text_color(*MUTED_TEXT_RGB)
        pdf.set_font("Helvetica", style="I", size=17)
        pdf.cell(0, 10, safe_text("Before You Begin"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*TEXT_COLOR_RGB)
        pdf.multi_cell(0, 10, safe_text(report["predictions_notes"]))

    jan1_balance = database.get_account_value(conn)
    for review in report["reviews"]:
        _write_trade_review_page(pdf, review, jan1_balance)

    # The session's closing journal entry (see database.
    # save_review_reflections_notes()) - its own page at the end, same
    # treatment as a per-trade review page, since it's meant to be a
    # real look-back over the whole session, not a quick aside. The
    # Session Summary (see _write_review_summary_section()) goes on
    # this SAME page, above the notes - the same numbers shown on the
    # in-app Reflections screen before you write them, so the emailed
    # copy has the stats behind the reflection, not just the reflection
    # itself.
    if report["reflections_notes"]:
        pdf.add_page()

        summary = review_session_summary(report["reviews"])
        if summary is not None:
            _write_review_summary_section(pdf, summary)

        pdf.set_text_color(*TEXT_COLOR_RGB)
        pdf.set_font("Helvetica", style="B", size=26)
        pdf.cell(0, 16, safe_text("Reflections"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font("Helvetica", style="I", size=18)
        pdf.multi_cell(0, 10, safe_text(report["reflections_notes"]))

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
