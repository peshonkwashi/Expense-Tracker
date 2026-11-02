"""Unit tests for the receipt OCR service (section 5.10.2).

The text parser and total extraction are pure and tested directly. The scan
itself is tested for its error paths and, when Tesseract is installed, end to
end on a receipt image rendered here.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ocr  # noqa: E402


class ReceiptTextParsingTests(unittest.TestCase):
    """Real-OCR text parsing (no Tesseract binary needed)."""

    def test_extracts_named_items_with_prices(self):
        items = ocr.parse_receipt_text('Bread 700g 25.00\nFresh Milk 2L 40.00')
        self.assertEqual([(i['name'], i['price']) for i in items],
                         [('Bread 700g', 25.00), ('Fresh Milk 2L', 40.00)])

    def test_total_tax_and_payment_lines_are_not_items(self):
        text = ('Bread 25.00\nMilk 40.00\nSUBTOTAL 65.00\nVAT 10.40\n'
                'TOTAL 75.40\nCASH 100.00\nCHANGE 24.60')
        names = {i['name'] for i in ocr.parse_receipt_text(text)}
        self.assertEqual(names, {'Bread', 'Milk'})

    def test_takes_the_last_price_as_the_line_total(self):
        # "2 @ 12.50" unit price then the 25.00 line total: keep the total.
        items = ocr.parse_receipt_text('Eggs 2 12.50 25.00')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['price'], 25.00)

    def test_unit_price_gives_the_quantity(self):
        for line in ('Eggs 2 12.50 25.00', 'Eggs 2 x 12.50 25.00',
                     'Eggs 2x 12.50 25.00', 'Eggs 2 @ 12.50 25.00'):
            with self.subTest(line=line):
                [item] = ocr.parse_receipt_text(line)
                self.assertEqual((item['name'], item['quantity'], item['price']),
                                 ('Eggs', 2, 25.00))

    def test_unrelated_second_number_is_not_a_quantity(self):
        # 700g is part of the name and 9.99 does not divide 25.00 evenly.
        [item] = ocr.parse_receipt_text('Bread 700g 9.99 25.00')
        self.assertEqual(item['quantity'], 1)

    def test_currency_markers_are_stripped_from_names(self):
        items = ocr.parse_receipt_text('Bread K25.00\nMilk ZMW 40.00')
        self.assertEqual([i['name'] for i in items], ['Bread', 'Milk'])

    def test_thousands_separator_is_handled(self):
        items = ocr.parse_receipt_text('Sofa Set 1,250.00')
        self.assertEqual(items[0]['price'], 1250.00)

    def test_lines_without_a_price_are_ignored(self):
        text = 'SHOPRITE MANDA HILL\nTel: 0211 123456\n2026-09-28\nBread 25.00'
        items = ocr.parse_receipt_text(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'Bread')

    def test_implausible_prices_are_dropped(self):
        # A misread barcode/total in the millions is not a real line price.
        self.assertEqual(ocr.parse_receipt_text('Item 9999999.00'), [])

    def test_empty_text_returns_no_items(self):
        self.assertEqual(ocr.parse_receipt_text(''), [])
        self.assertEqual(ocr.parse_receipt_text(None), [])


class ReceiptTotalTests(unittest.TestCase):
    def test_grand_total_is_found(self):
        text = ('Bread 25.00\nMilk 40.00\nSUBTOTAL 65.00\nVAT 10.40\n'
                'TOTAL 75.40\nCASH 100.00\nCHANGE 24.60')
        self.assertEqual(ocr.parse_receipt_total(text), 75.40)

    def test_subtotal_vat_and_item_count_are_not_the_total(self):
        text = 'SUB-TOTAL 65.00\nTOTAL VAT 10.40\nTOTAL ITEMS 2'
        self.assertIsNone(ocr.parse_receipt_total(text))

    def test_amount_due_counts_as_a_total(self):
        self.assertEqual(ocr.parse_receipt_total('AMOUNT DUE K1,240.50'), 1240.50)

    def test_no_total_line(self):
        self.assertIsNone(ocr.parse_receipt_total('Bread 25.00'))


class ScanErrorTests(unittest.TestCase):
    """scan_receipt never makes items up: problems raise ReceiptScanError."""

    def test_missing_file_raises(self):
        with self.assertRaises(ocr.ReceiptScanError):
            ocr.scan_receipt('does-not-exist.jpg')
        with self.assertRaises(ocr.ReceiptScanError):
            ocr.scan_receipt(None)

    def test_missing_tesseract_raises_an_actionable_error(self):
        with _temp_file(b'x', '.png') as path, \
                mock.patch.object(ocr, 'find_tesseract', return_value=None):
            with self.assertRaises(ocr.ReceiptScanError) as caught:
                ocr.scan_receipt(path)
        self.assertIn('Tesseract', str(caught.exception))

    def test_unreadable_image_raises(self):
        with _temp_file(b'definitely not an image', '.png') as path, \
                mock.patch.object(ocr, 'find_tesseract', return_value='tesseract'):
            with self.assertRaises(ocr.ReceiptScanError) as caught:
                ocr.scan_receipt(path)
        self.assertIn('could not be opened', str(caught.exception))

    def test_pdf_without_poppler_raises(self):
        with _temp_file(b'%PDF-1.4', '.pdf') as path, \
                mock.patch.object(ocr, 'find_tesseract', return_value='tesseract'), \
                mock.patch.object(ocr, 'find_poppler', return_value=None):
            with self.assertRaises(ocr.ReceiptScanError) as caught:
                ocr.scan_receipt(path)
        self.assertIn('poppler', str(caught.exception))


class _temp_file:
    """A temporary file with the given bytes, deleted afterwards (Windows-safe)."""

    def __init__(self, data, suffix):
        self.data, self.suffix = data, suffix

    def __enter__(self):
        handle = tempfile.NamedTemporaryFile(suffix=self.suffix, delete=False)
        handle.write(self.data)
        handle.close()
        self.path = handle.name
        return self.path

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _render_receipt(lines):
    """Draw receipt text into a PNG the way a flatbed scan would look."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype('consola.ttf', 28)
    except OSError:
        try:
            font = ImageFont.truetype('DejaVuSansMono.ttf', 28)
        except OSError:
            font = ImageFont.load_default(size=28)
    img = Image.new('RGB', (900, 60 + 44 * len(lines)), 'white')
    draw = ImageDraw.Draw(img)
    for row, line in enumerate(lines):
        draw.text((40, 30 + 44 * row), line, fill='black', font=font)
    handle = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    handle.close()
    img.save(handle.name)
    return handle.name


@unittest.skipUnless(ocr.find_tesseract(), 'Tesseract is not installed')
class EndToEndOcrTests(unittest.TestCase):
    """Real Tesseract on a rendered receipt."""

    def test_rendered_receipt_is_itemised_and_totalled(self):
        path = _render_receipt([
            'SHOPRITE MANDA HILL',
            'Bread 700g          25.00',
            'Fresh Milk 2L       40.00',
            'Eggs   2 x 27.50    55.00',
            'SUBTOTAL           120.00',
            'TOTAL              120.00',
            'CASH               200.00',
        ])
        try:
            scan = ocr.scan_receipt(path)
        finally:
            os.unlink(path)
        names = [i['name'] for i in scan.items]
        self.assertEqual(len(scan.items), 3, names)
        self.assertEqual(scan.items_total, 120.00)
        self.assertEqual(scan.total, 120.00)
        self.assertEqual(scan.items[2]['quantity'], 2)

    def test_blank_image_yields_no_items(self):
        path = _render_receipt([])
        try:
            scan = ocr.scan_receipt(path)
        finally:
            os.unlink(path)
        self.assertEqual(scan.items, [])


class EmailReceiptFilterTests(unittest.TestCase):
    """The keyword filter that decides which merchants email a receipt."""

    def test_online_and_app_merchants_email_a_receipt(self):
        for merchant in ('Uber Eats', 'Amazon.com', 'Jumia Order', 'Yango Trip',
                         'Shoprite Manda Hill', 'KFC Cairo Road', 'Mr Price Clothing',
                         'Game Store Electronics', 'Betway Deposit',
                         'NETFLIX.COM Subscription', 'MTN Data Bundle'):
            with self.subTest(merchant=merchant):
                self.assertTrue(ocr.emails_receipt(merchant))

    def test_cash_fuel_and_utility_merchants_do_not(self):
        for merchant in ('Fuel Puma Kabulonga', 'Engen Great East Rd',
                         'Total Filling Station', 'ZESCO Prepaid Units',
                         'LWSC Water Bill', 'Taxi Fare Town', 'Barber Shop',
                         'Rent Payment Landlord', 'Bayport Loan Repayment',
                         'Medical Clinic Visit'):
            with self.subTest(merchant=merchant):
                self.assertFalse(ocr.emails_receipt(merchant))

    def test_blank_description_is_not_a_match(self):
        self.assertFalse(ocr.emails_receipt(''))
        self.assertFalse(ocr.emails_receipt(None))


if __name__ == '__main__':
    unittest.main()
