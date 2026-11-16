"""
Monthly Review Reminder
=====================
Nudges you to review last month's closed trades.

Two pieces share the same check ("does last month still have closed
trades with no saved review?"):

  - An email, sent on the 1st of every month by a scheduled GitHub
    Actions workflow (.github/workflows/monthly_review_reminder.yml,
    which runs `python monthly_review.py`). Skipped if last month had no
    closed trades or every one is already journaled, and sent at most
    once per month (see database.monthly_review_reminders).
  - A banner on the Trade Analyzer page (see pages/1_Trade_Analyzer.py's
    render_monthly_review_banner()), which stays up until last month's
    trades are all journaled.

"Last month's trades" means trades whose EXIT date falls in that
calendar month - the same rule the Review Session's date range uses.
"Journaled" means a saved review in any Review Session (see
analyze_trades.is_trade_journaled()). The email deliberately has no
dollar amounts - just counts.
"""

import smtplib
from datetime import timedelta
from email.message import EmailMessage

import database
import timeutil
from analyze_trades import is_trade_journaled
from report_utils import get_secret


def previous_month_start(today):
    """The 1st of the calendar month before `today`'s month."""
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def pending_for_previous_month(trades, reviewed_exact, reviewed_loose, today=None):
    """
    Returns (month_start, month_trades, unreviewed_trades) for the
    previous calendar month, or None if there's nothing to nag about
    (no closed trades that month, or all of them already journaled).
    `trades` are database.get_trades() dicts.
    """
    today = today or timeutil.today_eastern()
    month_start = previous_month_start(today)
    this_month_start = today.replace(day=1)
    month_trades = [t for t in trades if month_start <= t["date"].date() < this_month_start]
    unreviewed = [t for t in month_trades if not is_trade_journaled(t, reviewed_exact, reviewed_loose)]
    if not unreviewed:
        return None
    return month_start, month_trades, unreviewed


def send_reminder_email(month_start, total_count, unreviewed_count):
    """Emails the reminder to REPORT_EMAIL_RECIPIENTS through Gmail's
    SMTP server, the same account/app password the Daily Report uses.
    Raises on failure."""
    from_addr = get_secret("REPORT_EMAIL_FROM")
    app_password = get_secret("REPORT_EMAIL_APP_PASSWORD")
    recipients = [addr.strip() for addr in get_secret("REPORT_EMAIL_RECIPIENTS").split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("REPORT_EMAIL_RECIPIENTS has no valid email addresses in it.")

    month_name = f"{month_start:%B %Y}"
    msg = EmailMessage()
    msg["Subject"] = f"Time to review your {month_name} trades"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        f"{month_name} is closed out: you had {total_count} closed trade(s), and "
        f"{unreviewed_count} haven't been journaled yet.\n\n"
        "Open your trading journal -> Trade Analyzer -> click the \"Review "
        f"{month_start:%B}\" button to start a Review Session for the month."
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


def main():
    conn = database.get_connection()
    today = timeutil.today_eastern()
    month_start = previous_month_start(today)

    if database.get_monthly_reminder_sent(conn, month_start):
        print(f"Reminder for {month_start:%B %Y} was already sent, skipping.")
        return

    reviewed_exact, reviewed_loose = database.get_reviewed_trade_keys(conn)
    pending = pending_for_previous_month(database.get_trades(conn), reviewed_exact, reviewed_loose, today)
    if pending is None:
        print(f"Nothing to review for {month_start:%B %Y} (no closed trades, or all journaled), skipping.")
        return

    _, month_trades, unreviewed = pending
    send_reminder_email(month_start, len(month_trades), len(unreviewed))
    database.mark_monthly_reminder_sent(conn, month_start)
    print(f"Sent the {month_start:%B %Y} review reminder ({len(unreviewed)} of {len(month_trades)} unjournaled).")


if __name__ == "__main__":
    main()
