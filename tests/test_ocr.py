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


if __name__ == '__main__':
    unittest.main()
