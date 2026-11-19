"""Unit tests for the IMAP receipt fetcher — no network.

A fake IMAP object stands in for a live mailbox so the attachment-parsing logic
is exercised deterministically. The live connection itself (connect_imap) is a
thin imaplib wrapper and is not unit-tested here.
"""
import email
import os
import sys
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date  # noqa: E402
from unittest import mock  # noqa: E402

from services import email_receipts, fx, imap_fetcher  # noqa: E402
from services.ocr import ReceiptScan  # noqa: E402


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

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return ('OK', [b'1'])

    def search(self, charset, query):
        self.last_query = query
        return ('OK', [b'1']) if self.raw else ('OK', [b''])

    def fetch(self, email_id, parts):
        self.last_fetch = parts
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

    def test_mailbox_is_opened_read_only_and_messages_are_peeked(self):
        mail = FakeIMAP(_message_with_pdf())
        imap_fetcher.find_receipt_messages(mail, 'uber', date(2026, 11, 3))
        self.assertTrue(mail.readonly)
        self.assertIn('BODY.PEEK', mail.last_fetch)
        self.assertIn('SINCE "31-Oct-2026" BEFORE "07-Nov-2026"', mail.last_query)

    def test_imap_server_is_chosen_from_the_address(self):
        with mock.patch.dict(os.environ, {'IMAP_SERVER': ''}):
            self.assertEqual(imap_fetcher.imap_server_for('a@gmail.com'), 'imap.gmail.com')
            self.assertEqual(imap_fetcher.imap_server_for('a@hotmail.com'),
                             'outlook.office365.com')
            self.assertEqual(imap_fetcher.imap_server_for('a@unza.zm'), 'imap.unza.zm')
        with mock.patch.dict(os.environ, {'IMAP_SERVER': 'mail.example.org'}):
            self.assertEqual(imap_fetcher.imap_server_for('a@gmail.com'), 'mail.example.org')

    def test_html_receipt_rows_become_receipt_lines(self):
        text = imap_fetcher.html_to_text(UBER_HTML)
        self.assertIn('Trip fare K 45.00', text)
        self.assertNotIn('color', text)  # <style> content is dropped


UBER_HTML = """<html><head><style>td { color: red }</style></head><body>
<h1>Thanks for riding, Ann</h1>
<table>
  <tr><td>Trip fare</td><td>K 45.00</td></tr>
  <tr><td>Booking fee</td><td>K 5.00</td></tr>
  <tr><td><b>Total</b></td><td><b>K 50.00</b></td></tr>
</table></body></html>"""


def _html_receipt(html=UBER_HTML, sender='Uber Receipts <noreply@uber.com>',
                  message_id='<trip-1@uber.com>'):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'Your Tuesday trip with Uber'
    msg['From'] = sender
    msg['Message-ID'] = message_id
    msg['Date'] = 'Tue, 03 Nov 2026 18:20:00 +0200'
    msg.attach(MIMEText('View your receipt in the Uber app.', 'plain'))
    msg.attach(MIMEText(html, 'html'))
    return msg.as_bytes()


class MultiIMAP(FakeIMAP):
    """Serves several messages; ids are 1..n, oldest first."""

    def __init__(self, raws):
        super().__init__(raws[0] if raws else None)
        self.raws = raws

    def search(self, charset, query):
        self.last_query = query
        return ('OK', [b' '.join(str(i + 1).encode() for i in range(len(self.raws)))])

    def fetch(self, email_id, parts):
        self.last_fetch = parts
        return ('OK', [(b'x', self.raws[int(email_id) - 1])])


def _no_ocr(path):
    raise AssertionError('OCR should not run when the body has the items')


class EmailReceiptMatchingTests(unittest.TestCase):
    DAY = date(2026, 11, 3)

    def test_body_receipt_matching_the_amount_is_used_without_ocr(self):
        mail = MultiIMAP([_html_receipt()])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 50.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.MATCHED)
        self.assertEqual([(i['name'], i['price']) for i in result.scan.items],
                         [('Trip fare', 45.0), ('Booking fee', 5.0)])

    def test_the_receipt_with_the_matching_total_wins(self):
        other = UBER_HTML.replace('45.00', '75.00').replace('50.00', '80.00')
        mail = MultiIMAP([_html_receipt(), _html_receipt(other)])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 50.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.MATCHED)
        self.assertEqual(result.scan.total, 50.0)

    def test_a_lone_receipt_with_a_different_total_is_flagged_for_review(self):
        mail = MultiIMAP([_html_receipt()])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 55.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.SOLE_CANDIDATE)
        self.assertTrue(result.usable)

    def test_several_receipts_and_no_match_adds_nothing(self):
        other = UBER_HTML.replace('50.00', '80.00')
        mail = MultiIMAP([_html_receipt(), _html_receipt(other)])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 12.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.AMBIGUOUS)
        self.assertFalse(result.usable)

    def test_no_email_is_not_found(self):
        result = email_receipts.find_receipt_for(
            FakeIMAP(None), 'uber', self.DAY, 50.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.NOT_FOUND)

    def test_attachment_is_ocrd_when_the_body_has_no_items(self):
        scanned = []

        def fake_ocr(path):
            scanned.append(path)
            return ReceiptScan(items=[{'name': 'Burger', 'price': 50.0,
                                       'quantity': 1}], total=50.0)

        mail = MultiIMAP([_message_with_pdf()])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 50.0, fake_ocr)
        self.assertEqual(result.status, email_receipts.MATCHED)
        self.assertEqual(len(scanned), 1)
        self.assertFalse(os.path.exists(scanned[0]))  # temp file cleaned up

    def test_email_with_no_priced_lines_is_unreadable(self):
        mail = MultiIMAP([_message_without_attachment()])
        result = email_receipts.find_receipt_for(mail, 'uber', self.DAY, 50.0, _no_ocr)
        self.assertEqual(result.status, email_receipts.UNREADABLE)


class InboxSweepTests(unittest.TestCase):
    def test_merchant_comes_from_the_sender_or_subject(self):
        msg = email.message_from_bytes(_html_receipt())
        self.assertEqual(email_receipts.merchant_of(msg), ('uber', 'Uber Receipts'))
        unknown = email.message_from_bytes(_html_receipt(
            sender='Weekly Deals <news@example.com>').replace(b'with Uber', b'deals'))
        self.assertEqual(email_receipts.merchant_of(unknown), (None, None))

    def test_receipt_currency_is_detected(self):
        self.assertEqual(fx.detect_currency('Total $12.99'), 'USD')
        self.assertEqual(fx.detect_currency('Total US$ 12.99'), 'USD')
        self.assertEqual(fx.detect_currency('Total 12.99 EUR'), 'EUR')
        self.assertEqual(fx.detect_currency('Total £9.50'), 'GBP')
        self.assertEqual(fx.detect_currency('Total R 120.00'), 'ZAR')
        for kwacha in ('Total K 50.00', 'Total ZMW 50.00', 'Total 50.00',
                       'Total K50.00 (about $2)'):
            with self.subTest(text=kwacha):
                self.assertIsNone(fx.detect_currency(kwacha))

    def test_sweep_reads_new_receipts_and_skips_used_ones(self):
        second = _html_receipt(UBER_HTML.replace('50.00', '80.00'),
                               message_id='<trip-2@uber.com>')
        mail = MultiIMAP([_html_receipt(), second])
        result = email_receipts.read_new_receipts(
            mail, _no_ocr, skip_keys={'<trip-1@uber.com>'})
        self.assertEqual(len(result.receipts), 1)
        receipt = result.receipts[0]
        self.assertEqual((receipt.merchant, receipt.amount, receipt.message_key),
                         ('Uber Receipts', 80.0, '<trip-2@uber.com>'))
        self.assertEqual(receipt.on, date(2026, 11, 3))
        self.assertIn('SUBJECT "receipt"', mail.last_query)

    def test_sweep_skips_foreign_receipts_when_no_rate_is_available(self):
        dollars = UBER_HTML.replace('K ', '$')
        offline = fx.Converter(_SettingsConn(), fetch=_offline)
        result = email_receipts.read_new_receipts(
            MultiIMAP([_html_receipt(dollars)]), _no_ocr, converter=offline)
        self.assertEqual(result.receipts, [])
        self.assertEqual(result.foreign_currency, ['Uber Receipts'])

    def test_sweep_converts_foreign_receipts_and_their_items(self):
        dollars = UBER_HTML.replace('K ', '$')  # $45 + $5 = $50
        converter = fx.Converter(_SettingsConn(), fetch=_rates(USD=0.04))  # K25 per $
        result = email_receipts.read_new_receipts(
            MultiIMAP([_html_receipt(dollars)]), _no_ocr, converter=converter)
        receipt = result.receipts[0]
        self.assertEqual((receipt.amount, receipt.currency, receipt.original),
                         (1250.0, 'USD', 50.0))
        self.assertEqual([i['price'] for i in receipt.scan.items], [1125.0, 125.0])
        self.assertEqual(receipt.description, 'Uber Receipts (USD 50.00 at 25.00)')

    def test_existing_transaction_matches_a_converted_receipt_within_tolerance(self):
        dollars = UBER_HTML.replace('K ', '$')
        converter = fx.Converter(_SettingsConn(), fetch=_rates(USD=0.04))
        # The bank charged K1,290 (its own rate plus a fee): within 5% of K1,250.
        result = email_receipts.find_receipt_for(
            MultiIMAP([_html_receipt(dollars)]), 'uber', date(2026, 11, 3),
            1290.0, _no_ocr, converter=converter)
        self.assertEqual(result.status, email_receipts.MATCHED)


class _SettingsConn:
    """Just enough of a connection for database.get/set_setting."""

    def __init__(self):
        self.values = {}

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith('SELECT'):
            value = self.values.get(params[0])
            return mock.Mock(fetchone=lambda: {'value': value} if value else None)
        self.values[params[0]] = params[1]
        return mock.Mock()


def _rates(**rates):
    calls = []

    def fetch(base):
        calls.append(base)
        return rates
    fetch.calls = calls
    return fetch


def _offline(base):
    raise OSError('no network')


class ConverterTests(unittest.TestCase):
    def test_rates_are_fetched_once_and_reused_offline_later(self):
        conn = _SettingsConn()
        fetch = _rates(USD=0.04)
        first = fx.Converter(conn, fetch=fetch, today=date(2026, 11, 3))
        self.assertEqual(first.to_local(10, 'USD'), (250.0, 25.0))
        self.assertEqual(first.to_local(20, 'USD'), (500.0, 25.0))
        self.assertEqual(fetch.calls, ['ZMW'])  # one request per scan

        same_day = fx.Converter(conn, fetch=fetch, today=date(2026, 11, 3))
        same_day.to_local(10, 'USD')
        self.assertEqual(fetch.calls, ['ZMW'])  # today's rates are cached

        next_day = fx.Converter(conn, fetch=_offline, today=date(2026, 11, 4))
        self.assertEqual(next_day.to_local(10, 'USD'), (250.0, 25.0))
        self.assertTrue(next_day.stale)
        self.assertEqual(next_day.as_of, '2026-11-03')

    def test_no_rates_and_no_cache_converts_nothing(self):
        converter = fx.Converter(_SettingsConn(), fetch=_offline)
        self.assertIsNone(converter.to_local(10, 'USD'))
        self.assertIsNone(converter.as_of)

    def test_tolerance_is_exact_for_kwacha_and_five_percent_when_converted(self):
        self.assertEqual(fx.tolerance(1000, converted=False), 0.01)
        self.assertEqual(fx.tolerance(1000, converted=True), 50.0)


if __name__ == '__main__':
    unittest.main()
