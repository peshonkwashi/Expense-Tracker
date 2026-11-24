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


class NewAssistantCapabilitiesTests(unittest.TestCase):
    def test_new_intents_and_slots(self):
        self.assertEqual(parse('what if i cancel netflix?').intent, 'whatif')
        self.assertEqual(parse('if i cut dining out by k300').intent, 'whatif')
        self.assertEqual(parse('where can i cut back?').intent, 'cutback')
        self.assertEqual(parse('what am i overspending on?').intent, 'cutback')
        self.assertEqual(parse('how much can i spend per day until payday?').intent, 'left')
        self.assertEqual(parse('mtn momo vs airtel money this month').intent, 'channels')
        self.assertEqual(parse('how much cash did i spend this week?').channel, 'cash')
        self.assertEqual(parse('what did i buy at shoprite?').intent, 'receipt')

        item_query = assistant.parse(
            'how much did i spend on bread?', CATEGORIES, DESCRIPTIONS,
            TODAY, PAY_DAY, item_names={'white bread', 'fresh milk'})
        self.assertEqual(item_query.intent, 'receipt')
        self.assertEqual(item_query.receipt_item, 'bread')

    def test_end_to_end_answers_for_whatif_cutback_receipt_and_channels(self):
        from datetime import timedelta
        from tests.helpers import insert_transaction, temp_database
        with temp_database(salary=12000.0, salary_day=25) as (conn, uid):
            user = conn.execute('SELECT * FROM User WHERE user_id = ?', (uid,)).fetchone()
            # Insert a Shoprite debit with an itemised receipt, an MTN MoMo debit,
            # an Airtel Money debit, and a savings goal.
            insert_transaction(conn, uid, '2026-11-10', 'Shoprite Cairo Rd', 450.0,
                               category='Groceries')
            txn_id = conn.execute('SELECT MAX(transaction_id) FROM Transaction_Record').fetchone()[0]
            conn.execute("UPDATE Transaction_Record SET import_hash = 'sync-bank-1' "
                         "WHERE transaction_id = ?", (txn_id,))
            conn.execute('INSERT INTO Receipt_Item (transaction_id, item_name, amount, quantity) '
                         'VALUES (?, ?, ?, ?)', (txn_id, 'White Bread', 35.0, 2))
            insert_transaction(conn, uid, '2026-11-11', 'MTN MoMo Airtime', 100.0,
                               category='Airtime & Data')
            mtn_id = conn.execute('SELECT MAX(transaction_id) FROM Transaction_Record').fetchone()[0]
            conn.execute("UPDATE Transaction_Record SET import_hash = 'sync-momo-mtn-1' "
                         "WHERE transaction_id = ?", (mtn_id,))
            insert_transaction(conn, uid, '2026-11-12', 'Airtel Money ZESCO', 200.0,
                               category='Utilities')
            airtel_id = conn.execute('SELECT MAX(transaction_id) FROM Transaction_Record').fetchone()[0]
            conn.execute("UPDATE Transaction_Record SET import_hash = 'sync-momo-airtel-1' "
                         "WHERE transaction_id = ?", (airtel_id,))
            conn.execute('INSERT INTO SavingsGoal (goal_name, target_amount, target_date, user_id) '
                         'VALUES (?, ?, ?, ?)',
                         ('Emergency Fund', 3000.0, (TODAY + timedelta(days=180)).isoformat(), uid))
            conn.commit()

            # 1. What-if savings simulator
            whatif = assistant.answer(conn, user, 'if I cut groceries by K300', today=TODAY)
            self.assertIn('K3,600.00', whatif.answer)
            self.assertIn('Emergency Fund', whatif.answer)
            self.assertIn('/savings', whatif.answer)

            # 2. Cutback / overspending
            cutback = assistant.answer(conn, user, 'where can I cut back?', today=TODAY)
            self.assertIn('Groceries', cutback.answer)

            # 3. Receipt item Q&A
            bread = assistant.answer(conn, user, 'how much did I spend on bread?', today=TODAY)
            self.assertIn('K35.00', bread.answer)
            self.assertIn('Bread', bread.answer)

            shoprite_items = assistant.answer(conn, user, 'what did I buy at Shoprite?', today=TODAY)
            self.assertIn('White Bread', shoprite_items.answer)
            self.assertIn('/transactions?q=shoprite', shoprite_items.answer)

            # 4. Multi-wallet & channel breakdown
            channels = assistant.answer(conn, user, 'MTN MoMo vs Airtel Money this month', today=TODAY)
            self.assertIn('MTN MoMo', channels.answer)
            self.assertIn('Airtel Money', channels.answer)
            self.assertIn('K100.00', channels.answer)
            self.assertIn('K200.00', channels.answer)



class FixedAssistantBehaviourTests(unittest.TestCase):
    """Problems found by probing the new intents against the demo data."""

    def test_what_if_i_buy_is_an_affordability_question(self):
        query = parse('what if I buy a phone for K2500?')
        self.assertEqual(query.intent, 'afford')
        self.assertEqual(query.amount, 2500.0)

    def test_saving_what_ifs_stay_what_ifs(self):
        self.assertEqual(parse('what if I save K500 a month').intent, 'whatif')
        self.assertEqual(parse('what if I cut dining out by K300').intent, 'whatif')

    def test_category_is_kept_alongside_a_cash_or_wallet_channel(self):
        query = parse('how much did I spend on groceries with cash')
        self.assertEqual((query.channel, query.categories), ('cash', ['Groceries']))
        # A wallet name is never mistaken for a category ("Airtime & Data").
        query = parse('how much did I spend via airtel money')
        self.assertEqual((query.channel, query.categories), ('airtel', []))

    def test_subscription_found_by_a_word_of_the_question(self):
        from tests.helpers import insert_transaction, temp_database
        with temp_database(salary=12000.0, salary_day=25) as (conn, uid):
            user = conn.execute('SELECT * FROM User WHERE user_id = ?', (uid,)).fetchone()
            for month in ('2026-08', '2026-09', '2026-10', '2026-11'):
                insert_transaction(conn, uid, f'{month}-05', 'DSTV Multichoice Premium',
                                   360.0, category='Entertainment')
            conn.commit()
            reply = assistant.answer(conn, user, 'what if I cancel DSTV?', today=TODAY)
            self.assertIn('DSTV Multichoice Premium', reply.answer)
            self.assertIn('K4,320.00', reply.answer)
            missing = assistant.answer(conn, user, 'what if I cancel my gym', today=TODAY)
            self.assertIn("find a recurring charge", missing.answer)
            self.assertIn('gym', missing.answer)

    def test_receipt_items_without_a_quantity_count_as_one(self):
        from tests.helpers import insert_transaction, temp_database
        with temp_database(salary=12000.0, salary_day=25) as (conn, uid):
            user = conn.execute('SELECT * FROM User WHERE user_id = ?', (uid,)).fetchone()
            insert_transaction(conn, uid, '2026-11-10', 'Shoprite Cairo Rd', 89.0,
                               category='Groceries')
            txn = conn.execute('SELECT MAX(transaction_id) FROM Transaction_Record').fetchone()[0]
            conn.execute('INSERT INTO Receipt_Item (transaction_id, item_name, amount, quantity) '
                         'VALUES (?, ?, ?, NULL)', (txn, 'Cooking oil', 89.0))
            conn.commit()
            for question in ('receipt items', 'how much did I spend on oil',
                             'what did I buy at shoprite'):
                with self.subTest(question=question):
                    reply = assistant.answer(conn, user, question, today=TODAY)
                    self.assertIn('89.00', reply.answer)


if __name__ == '__main__':
    unittest.main()
