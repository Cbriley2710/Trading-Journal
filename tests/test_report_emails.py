"""
Tests for daily_report.send_report_email() and symbol_archive_report.
send_symbol_archive_email() - no real SMTP connection, no real
credentials needed. Mocks smtplib.SMTP_SSL and get_secret() the same
way tests/test_twitter_post.py mocks tweepy, covering: recipient-list
parsing (including the "empty string" edge case), and that a real send
calls login()/send_message() with the right pieces.
"""
from datetime import date
from unittest.mock import MagicMock

import pytest

import daily_report
import symbol_archive_report as sar


def _patch_smtp(monkeypatch, module):
    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp
    monkeypatch.setattr(module.smtplib, "SMTP_SSL", lambda host, port: fake_smtp)
    return fake_smtp


def test_send_report_email_raises_if_no_recipients(monkeypatch):
    secrets = {"REPORT_EMAIL_FROM": "me@example.com", "REPORT_EMAIL_APP_PASSWORD": "pw", "REPORT_EMAIL_RECIPIENTS": ""}
    monkeypatch.setattr(daily_report, "get_secret", lambda key: secrets[key])

    with pytest.raises(RuntimeError, match="REPORT_EMAIL_RECIPIENTS"):
        daily_report.send_report_email(b"fake-pdf-bytes", date(2026, 8, 7))


def test_send_report_email_sends_to_every_recipient(monkeypatch):
    secrets = {
        "REPORT_EMAIL_FROM": "me@example.com", "REPORT_EMAIL_APP_PASSWORD": "pw",
        "REPORT_EMAIL_RECIPIENTS": "a@example.com, b@example.com",
    }
    monkeypatch.setattr(daily_report, "get_secret", lambda key: secrets[key])
    fake_smtp = _patch_smtp(monkeypatch, daily_report)

    daily_report.send_report_email(b"fake-pdf-bytes", date(2026, 8, 7))

    fake_smtp.login.assert_called_once_with("me@example.com", "pw")
    sent_msg = fake_smtp.send_message.call_args[0][0]
    assert sent_msg["To"] == "a@example.com, b@example.com"
    assert "August 07, 2026" in sent_msg["Subject"]


def test_send_symbol_archive_email_raises_if_no_recipients(monkeypatch):
    secrets = {"REPORT_EMAIL_FROM": "me@example.com", "REPORT_EMAIL_APP_PASSWORD": "pw", "ARCHIVE_EMAIL_RECIPIENTS": "   "}
    monkeypatch.setattr(sar, "get_secret", lambda key: secrets[key])

    with pytest.raises(RuntimeError, match="ARCHIVE_EMAIL_RECIPIENTS"):
        sar.send_symbol_archive_email(b"fake-pdf-bytes", "NVDA")


def test_send_symbol_archive_email_sends_with_symbol_in_subject(monkeypatch):
    secrets = {
        "REPORT_EMAIL_FROM": "me@example.com", "REPORT_EMAIL_APP_PASSWORD": "pw",
        "ARCHIVE_EMAIL_RECIPIENTS": "archive@example.com",
    }
    monkeypatch.setattr(sar, "get_secret", lambda key: secrets[key])
    fake_smtp = _patch_smtp(monkeypatch, sar)

    sar.send_symbol_archive_email(b"fake-pdf-bytes", "NVDA")

    fake_smtp.login.assert_called_once_with("me@example.com", "pw")
    sent_msg = fake_smtp.send_message.call_args[0][0]
    assert sent_msg["To"] == "archive@example.com"
    assert "NVDA" in sent_msg["Subject"]
