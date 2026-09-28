"""Unit tests for the mock OCR / scan-receipt service (section 5.10.2).

scan_receipt is pure (no database), so these tests are property-based: the
exact items vary with a shuffle, but the scenario mapping and the reconciliation
to the transaction total are guaranteed and are what these assert.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ocr  # noqa: E402


def _total(items):
    return round(sum(i['price'] * 1 for i in items), 2)


def _names(items):
    return {i['name'] for i in items}


class ScenarioMappingTests(unittest.TestCase):
    def test_pharmacy_merchant_maps_to_healthcare(self):
        self.assertEqual(ocr.scenario_for('Link Pharmacy Kabulonga'), 'Healthcare')
        self.assertEqual(ocr.scenario_for('Medical Clinic Visit'), 'Healthcare')

    def test_supermarket_merchant_maps_to_groceries(self):
        self.assertEqual(ocr.scenario_for('Shoprite Manda Hill'), 'Groceries')
        self.assertEqual(ocr.scenario_for('Pick n Pay Levy'), 'Groceries')

    def test_fast_food_is_the_exception_and_wins_over_category(self):
        # Even if the transaction was filed under Groceries, a KFC receipt is
        # a fast-food menu, not a basket of groceries.
        self.assertEqual(ocr.scenario_for('KFC Cairo Road', category='Groceries'),
                         'Dining Out')
        self.assertEqual(ocr.scenario_for('Hungry Lion'), 'Dining Out')

    def test_unknown_merchant_falls_back_to_stored_category(self):
        self.assertEqual(ocr.scenario_for('Zzz Unknown Shop', category='Groceries'),
                         'Groceries')

    def test_non_itemisable_merchant_is_not_a_basket_scenario(self):
        # Fuel and utilities are recognised but are not itemisable baskets.
        self.assertNotIn(ocr.scenario_for('Fuel Puma Kabulonga'), ocr.SCENARIO_ITEMS)
        self.assertNotIn(ocr.scenario_for('ZESCO Prepaid Units'), ocr.SCENARIO_ITEMS)


class ScanReceiptTests(unittest.TestCase):
    def test_items_reconcile_to_the_transaction_total(self):
        for desc in ('Shoprite Manda Hill', 'Link Pharmacy', 'KFC Cairo Road',
                     'Fuel Puma', 'ZESCO Prepaid'):
            with self.subTest(desc=desc):
                items = ocr.scan_receipt('x.jpg', target_amount=437.50,
                                         description=desc)
                self.assertEqual(_total(items), 437.50)

    def test_pharmacy_yields_medication_items(self):
        items = ocr.scan_receipt('x.jpg', target_amount=400.0,
                                 description='Link Pharmacy')
        allowed = {name for name, _ in ocr.SCENARIO_ITEMS['Healthcare']} | {'Other Items'}
        self.assertTrue(_names(items).issubset(allowed))
        self.assertTrue(_names(items) - {'Other Items'})  # at least one real item

    def test_supermarket_yields_grocery_items(self):
        items = ocr.scan_receipt('x.jpg', target_amount=500.0,
                                 description='Shoprite Manda Hill')
        allowed = {name for name, _ in ocr.SCENARIO_ITEMS['Groceries']} | {'Other Items'}
        self.assertTrue(_names(items).issubset(allowed))

    def test_fast_food_yields_menu_items(self):
        items = ocr.scan_receipt('x.jpg', target_amount=300.0,
                                 description='KFC Cairo Road', category='Groceries')
        allowed = {name for name, _ in ocr.SCENARIO_ITEMS['Dining Out']} | {'Other Items'}
        self.assertTrue(_names(items).issubset(allowed))

    def test_fuel_is_a_single_representative_line(self):
        items = ocr.scan_receipt('x.jpg', target_amount=850.0,
                                 description='Fuel Puma Kabulonga')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['price'], 850.0)
        self.assertIn('Fuel', items[0]['name'])

    def test_utility_single_line(self):
        items = ocr.scan_receipt('x.jpg', target_amount=300.0,
                                 description='ZESCO Prepaid Units')
        self.assertEqual(len(items), 1)
        self.assertEqual(_total(items), 300.0)

    def test_tiny_amount_still_reconciles(self):
        # Smaller than any single grocery item: one reconciling line.
        items = ocr.scan_receipt('x.jpg', target_amount=5.0,
                                 description='Shoprite')
        self.assertEqual(_total(items), 5.0)

    def test_missing_amount_uses_default_and_reconciles(self):
        items = ocr.scan_receipt('x.jpg', target_amount=None,
                                 description='Shoprite')
        self.assertEqual(_total(items), ocr.DEFAULT_TARGET)

    def test_zero_or_negative_amount_uses_default(self):
        items = ocr.scan_receipt('x.jpg', target_amount=0, description='Shoprite')
        self.assertEqual(_total(items), ocr.DEFAULT_TARGET)


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

    def test_scan_receipt_falls_back_to_mock_when_ocr_unavailable(self):
        # No real image on disk -> real OCR is skipped, mock is used and its
        # prices reconcile to the transaction total.
        items = ocr.scan_receipt('does-not-exist.jpg', target_amount=180.0,
                                  description='Link Pharmacy')
        self.assertEqual(round(sum(i['price'] for i in items), 2), 180.0)


if __name__ == '__main__':
    unittest.main()
