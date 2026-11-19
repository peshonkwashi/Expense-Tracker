"""Unit tests for the on-device assistant's question parser (no database)."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import assistant  # noqa: E402

TODAY = date(2026, 11, 19)
PAY_DAY = 25
CATEGORIES = ['Groceries', 'Transport', 'Dining Out', 'Airtime & Data', 'Shopping',
              'Housing', 'Utilities']
DESCRIPTIONS = {'shoprite cairo rd', 'shoprite manda hill', 'netflix com subscription',
                'rent payment landlord', 'yango ride'}


def parse(text, context=None):
    return assistant.parse(text, CATEGORIES, DESCRIPTIONS, TODAY, PAY_DAY, context)


class PeriodTests(unittest.TestCase):
    def period(self, text):
        return assistant.parse_period(text, TODAY, PAY_DAY)

    def test_default_is_this_calendar_month_and_not_explicit(self):
        period, explicit = self.period('how much did i spend')
        self.assertEqual((period.start, period.end), (date(2026, 11, 1), date(2026, 11, 30)))
        self.assertFalse(explicit)

    def test_relative_periods(self):
        cases = {
            'last month': (date(2026, 10, 1), date(2026, 10, 31)),
            'yesterday': (date(2026, 11, 18), date(2026, 11, 18)),
            'last 3 months': (date(2026, 8, 22), TODAY),
            'this cycle': (date(2026, 10, 25), TODAY),
            'since payday': (date(2026, 10, 25), TODAY),
            'last cycle': (date(2026, 9, 25), date(2026, 10, 24)),
            'this year': (date(2026, 1, 1), TODAY),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                period, explicit = self.period(text)
                self.assertEqual((period.start, period.end), expected)
                self.assertTrue(explicit)

    def test_named_month_defaults_to_the_most_recent_one(self):
        self.assertEqual(self.period('in october')[0].start, date(2026, 10, 1))
        self.assertEqual(self.period('in december')[0].start, date(2025, 12, 1))
        self.assertEqual(self.period('march 2025')[0].start, date(2025, 3, 1))

    def test_may_the_verb_is_not_may_the_month(self):
        self.assertFalse(self.period('may i ask how much i spent')[1])
        self.assertEqual(self.period('spent in may')[0].start, date(2026, 5, 1))


class SlotTests(unittest.TestCase):
    def test_categories_and_synonyms(self):
        self.assertEqual(parse('spent on dining out').categories, ['Dining Out'])
        self.assertEqual(parse('airtime and data').categories, ['Airtime & Data'])
        self.assertEqual(parse('my taxi costs').categories, ['Transport'])
        self.assertEqual(sorted(parse('food this month').categories),
                         ['Dining Out', 'Groceries'])

    def test_merchant_is_found_in_descriptions_ignoring_common_words(self):
        self.assertEqual(parse('how much at shoprite').merchant, 'shoprite')
        self.assertEqual(parse('shoprite manda hill spending').merchant,
                         'shoprite manda hill')
        self.assertIsNone(parse('how much did i spend').merchant)

    def test_amounts(self):
        for text, amount in {'can i afford k800': 800, 'afford 1,200.50': 1200.5,
                             'can i afford 2.5k': 2500, 'afford zmw 300': 300}.items():
            with self.subTest(text=text):
                self.assertEqual(parse(text).amount, amount)
        self.assertIsNone(parse('spent in the last 3 months').amount)
        self.assertIsNone(parse('spent in october 2026').amount)


class IntentTests(unittest.TestCase):
    def test_intents(self):
        cases = {
            'how much did i spend on transport': 'spend',
            'how much do i have left': 'left',
            'can i afford a jacket': 'afford',
            'compare groceries with last month': 'compare',
            'my biggest expenses': 'top',
            'forecast for next month': 'forecast',
            'how are my goals': 'goals',
            'my subscriptions': 'subscriptions',
            'when is payday': 'payday',
            'show my recent transactions': 'recent',
            'how many times did i go to shoprite': 'count',
            'daily average this cycle': 'average',
            'income last month': 'income',
            'help': 'help',
        }
        for text, intent in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse(text).intent, intent)

    def test_unrecognised_question_has_no_intent(self):
        self.assertIsNone(parse('tell me a joke').intent)

    def test_compare_baseline_is_not_the_period_asked_about(self):
        query = parse('compare groceries with last month')
        self.assertEqual(query.period.label, 'this month')
        self.assertEqual(query.period.previous().label, 'last month')
        self.assertEqual(parse('dining out vs last cycle').period.kind, 'cycle')


class FollowUpTests(unittest.TestCase):
    def context(self, text):
        query = parse(text)
        return {'intent': query.intent, 'categories': query.categories,
                'category_label': query.category_label, 'merchant': query.merchant,
                'period': [query.period.start.isoformat(), query.period.end.isoformat(),
                           query.period.label, query.period.kind]}

    def test_and_last_month_keeps_the_subject_and_intent(self):
        query = parse('and last month?', self.context('how much on transport'))
        self.assertEqual((query.intent, query.categories, query.period.label),
                         ('spend', ['Transport'], 'last month'))

    def test_what_about_another_category_keeps_the_period(self):
        query = parse('what about groceries?', self.context('transport last month'))
        self.assertEqual((query.categories, query.period.label),
                         (['Groceries'], 'last month'))

    def test_compare_chip_compares_the_previous_subject(self):
        query = parse('compare with last month', self.context('how much on groceries'))
        self.assertEqual((query.intent, query.categories, query.period.label),
                         ('compare', ['Groceries'], 'this month'))

    def test_a_new_full_question_does_not_inherit(self):
        query = parse('how much do i have left', self.context('transport last month'))
        self.assertEqual((query.intent, query.categories), ('left', []))


if __name__ == '__main__':
    unittest.main()
