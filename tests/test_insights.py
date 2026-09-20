"""Unit tests for the Smart Suggestions engine (section 5.10.2)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import insights  # noqa: E402
from tests.helpers import insert_transaction, temp_database  # noqa: E402


def _row(description, amount, category_name, category_type, is_subscription=0):
    return {'description': description, 'amount': amount,
            'category_name': category_name, 'category_type': category_type,
            'is_subscription': is_subscription}


class HighFrequencyTests(unittest.TestCase):
    def test_more_than_threshold_visits_triggers(self):
        rows = [_row('Shoprite Manda Hill', 200.0, 'Groceries', 'ESSENTIAL')
                for _ in range(5)]
        out = insights.high_frequency_suggestions(rows)
        self.assertEqual(len(out), 1)
        self.assertIn('5 times', out[0]['message'])
        self.assertIn('Shoprite', out[0]['title'])

    def test_at_threshold_does_not_trigger(self):
        rows = [_row('Shoprite Manda Hill', 200.0, 'Groceries', 'ESSENTIAL')
                for _ in range(4)]  # threshold is "more than 4"
        self.assertEqual(insights.high_frequency_suggestions(rows), [])

    def test_non_trip_category_is_ignored(self):
        # Buying airtime six times is not a "trip" to consolidate.
        rows = [_row('Airtel Airtime', 50.0, 'Airtime & Data', 'ESSENTIAL')
                for _ in range(6)]
        self.assertEqual(insights.high_frequency_suggestions(rows), [])

    def test_distinct_merchants_do_not_aggregate(self):
        # Genuinely different shops, one visit each — not a frequency pattern.
        # (Names must differ in words, not just a trailing number, since the
        # merchant key strips reference digits.)
        names = ['Alpha Store', 'Bravo Mart', 'Charlie Shop', 'Delta Retail',
                 'Echo Boutique', 'Foxtrot Outlet']
        rows = [_row(name, 100.0, 'Shopping', 'DISCRETIONARY') for name in names]
        self.assertEqual(insights.high_frequency_suggestions(rows), [])


class ConcentrationTests(unittest.TestCase):
    def test_single_merchant_over_half_triggers(self):
        rows = [
            _row('KFC Cairo Road', 300.0, 'Dining Out', 'DISCRETIONARY'),
            _row('KFC Cairo Road', 200.0, 'Dining Out', 'DISCRETIONARY'),
            _row('Pizza Place', 100.0, 'Dining Out', 'DISCRETIONARY'),
        ]
        out = insights.concentration_suggestions(rows)
        self.assertEqual(len(out), 1)
        self.assertIn('Dining Out', out[0]['title'])
        self.assertIn('KFC', out[0]['message'])

    def test_balanced_category_does_not_trigger(self):
        rows = [
            _row('KFC', 100.0, 'Dining Out', 'DISCRETIONARY'),
            _row('Pizza Place', 100.0, 'Dining Out', 'DISCRETIONARY'),
            _row('Cafe Mocha', 100.0, 'Dining Out', 'DISCRETIONARY'),
        ]
        self.assertEqual(insights.concentration_suggestions(rows), [])

    def test_essential_categories_are_ignored(self):
        # Groceries dominated by one supermarket is normal, not a concern.
        rows = [
            _row('Shoprite', 800.0, 'Groceries', 'ESSENTIAL'),
            _row('Local Shop', 100.0, 'Groceries', 'ESSENTIAL'),
        ]
        self.assertEqual(insights.concentration_suggestions(rows), [])

    def test_single_transaction_is_not_a_pattern(self):
        rows = [_row('KFC', 300.0, 'Dining Out', 'DISCRETIONARY')]
        self.assertEqual(insights.concentration_suggestions(rows), [])

    def test_uncategorised_is_ignored(self):
        rows = [
            _row('Mystery A', 500.0, 'Uncategorised', 'DISCRETIONARY'),
            _row('Mystery A', 300.0, 'Uncategorised', 'DISCRETIONARY'),
        ]
        self.assertEqual(insights.concentration_suggestions(rows), [])

    def test_dominant_subscription_gets_subscription_advice(self):
        # Netflix dominates the Subscriptions category: "shop around" is wrong,
        # so the advice reframes to keep/downgrade/cancel with the annual cost.
        rows = [
            _row('NETFLIX.COM', 200.0, 'Subscriptions', 'DISCRETIONARY', is_subscription=1),
            _row('Spotify', 80.0, 'Subscriptions', 'DISCRETIONARY', is_subscription=1),
        ]
        out = insights.concentration_suggestions(rows)
        self.assertEqual(len(out), 1)
        message = out[0]['message']
        self.assertNotIn('Shopping around', message)
        self.assertIn('cancelling', message)
        self.assertIn('2,400.00', message)  # 200 * 12 annualised

    def test_dominant_non_subscription_still_says_shop_around(self):
        rows = [
            _row('KFC Cairo Road', 300.0, 'Dining Out', 'DISCRETIONARY'),
            _row('Pizza Place', 100.0, 'Dining Out', 'DISCRETIONARY'),
        ]
        out = insights.concentration_suggestions(rows)
        self.assertEqual(len(out), 1)
        self.assertIn('Shopping around', out[0]['message'])


class SubscriptionAuditTests(unittest.TestCase):
    def setUp(self):
        self.context = temp_database(salary=10000.0, salary_day=25)
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def _subscription(self, desc, amount, when='2026-05-10'):
        insert_transaction(self.conn, self.user_id, when, desc, amount,
                           category='Subscriptions')
        self.conn.execute(
            'UPDATE Transaction_Record SET is_subscription = 1 WHERE description = ?',
            (desc,))
        self.conn.commit()

    def test_heavy_subscription_load_triggers(self):
        # 3 subs totalling 600 = 6% of a 10,000 salary, above the 5% threshold.
        self._subscription('Netflix', 200.0)
        self._subscription('DStv', 250.0)
        self._subscription('Spotify', 150.0)
        out = insights.subscription_audit_suggestions(
            self.conn, self.user_id, '2026-05', 10000.0)
        self.assertEqual(len(out), 1)
        self.assertIn('3 recurring charges', out[0]['message'])

    def test_below_salary_share_does_not_trigger(self):
        # Two small subs (100 total = 1% of salary) is not worth nudging.
        self._subscription('Spotify', 50.0)
        self._subscription('News App', 50.0)
        out = insights.subscription_audit_suggestions(
            self.conn, self.user_id, '2026-05', 10000.0)
        self.assertEqual(out, [])

    def test_single_subscription_does_not_trigger(self):
        self._subscription('Netflix', 900.0)  # big, but only one
        out = insights.subscription_audit_suggestions(
            self.conn, self.user_id, '2026-05', 10000.0)
        self.assertEqual(out, [])


class GenerateSuggestionsTests(unittest.TestCase):
    def setUp(self):
        self.context = temp_database(salary=10000.0, salary_day=25)
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_all_three_rules_and_warnings_come_first(self):
        # High frequency: 5 grocery trips to one shop.
        for _ in range(5):
            insert_transaction(self.conn, self.user_id, '2026-05-05',
                               'Shoprite Manda Hill', 200.0, category='Groceries')
        # Concentration: Dining Out dominated by KFC.
        for _ in range(3):
            insert_transaction(self.conn, self.user_id, '2026-05-06',
                               'KFC Cairo Road', 150.0, category='Dining Out')
        insert_transaction(self.conn, self.user_id, '2026-05-07',
                           'Pizza Place', 60.0, category='Dining Out')
        # Subscriptions: 3 charges over the salary-share threshold.
        for desc, amt in [('Netflix', 200.0), ('DStv', 250.0), ('Spotify', 150.0)]:
            insert_transaction(self.conn, self.user_id, '2026-05-08', desc, amt,
                               category='Subscriptions')
            self.conn.execute('UPDATE Transaction_Record SET is_subscription = 1 '
                              'WHERE description = ?', (desc,))
        self.conn.commit()

        out = insights.generate_suggestions(self.conn, self.user_id, '2026-05')
        self.assertGreaterEqual(len(out), 3)
        for item in out:
            self.assertIn('title', item)
            self.assertIn('message', item)
            self.assertIn(item['type'], {'warning', 'info'})
        # Warnings (concentration, subscriptions) sort ahead of info (frequency).
        types = [item['type'] for item in out]
        self.assertEqual(types, sorted(types, key=lambda t: {'warning': 0, 'info': 1}[t]))

    def test_no_data_yields_no_suggestions(self):
        out = insights.generate_suggestions(self.conn, self.user_id, '2026-05')
        self.assertEqual(out, [])

    def test_salary_defaults_from_user_when_not_passed(self):
        for desc, amt in [('Netflix', 300.0), ('DStv', 300.0)]:
            insert_transaction(self.conn, self.user_id, '2026-05-08', desc, amt,
                               category='Subscriptions')
            self.conn.execute('UPDATE Transaction_Record SET is_subscription = 1 '
                              'WHERE description = ?', (desc,))
        self.conn.commit()
        # 600 / 10,000 salary = 6% > 5%; salary read from the User row.
        out = insights.generate_suggestions(self.conn, self.user_id, '2026-05')
        self.assertTrue(any('recurring charges' in s['message'] for s in out))


if __name__ == '__main__':
    unittest.main()
