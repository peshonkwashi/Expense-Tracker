"""Unit tests for the IMAP receipt fetcher — no network.

A fake IMAP object stands in for a live mailbox so the attachment-parsing logic
is exercised deterministically. The live connection itself (connect_imap) is a
thin imaplib wrapper and is not unit-tested here.
"""
import os
import sys
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import imap_fetcher  # noqa: E402


def _message_with_pdf():
    msg = MIMEMultipart()
    msg['Subject'] = 'Your receipt from Uber'
    msg.attach(MIMEText('Thanks for riding with us.', 'plain'))
    pdf = MIMEApplication(b'%PDF-1.4 fake receipt bytes', _subtype='pdf')
    pdf.add_header('Content-Disposition', 'attachment', filename='receipt.pdf')
    msg.attach(pdf)
    return msg.as_bytes()


def _message_without_attachment():
    msg = MIMEMultipart()
    msg['Subject'] = 'Your receipt from Uber'
    msg.attach(MIMEText('Your receipt is in the body, no attachment.', 'plain'))
    return msg.as_bytes()


class FakeIMAP:
    """Minimal stand-in for an imaplib connection."""

    def __init__(self, raw_message):
        self.raw = raw_message
        self.logged_out = False
        self.last_query = None

    def select(self, mailbox):
        return ('OK', [b'1'])

    def search(self, charset, query):
        self.last_query = query
        return ('OK', [b'1']) if self.raw else ('OK', [b''])

    def fetch(self, email_id, parts):
        return ('OK', [(b'1 (RFC822 {123}', self.raw)])

    def logout(self):
        self.logged_out = True


class ImapFetcherTests(unittest.TestCase):
    def test_downloads_a_pdf_attachment_to_a_temp_file(self):
        mail = FakeIMAP(_message_with_pdf())
        path = imap_fetcher.fetch_receipt_attachment(mail, 'uber')
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith('.pdf'))
        try:
            with open(path, 'rb') as handle:
                self.assertIn(b'%PDF', handle.read())
        finally:
            os.remove(path)

    def test_returns_none_when_no_email_matches(self):
        mail = FakeIMAP(None)
        self.assertIsNone(imap_fetcher.fetch_receipt_attachment(mail, 'uber'))

    def test_returns_none_when_the_email_has_no_attachment(self):
        mail = FakeIMAP(_message_without_attachment())
        self.assertIsNone(imap_fetcher.fetch_receipt_attachment(mail, 'uber'))

    def test_blank_keyword_is_rejected_before_searching(self):
        mail = FakeIMAP(_message_with_pdf())
        self.assertIsNone(imap_fetcher.fetch_receipt_attachment(mail, '"" '))


if __name__ == '__main__':
    unittest.main()
