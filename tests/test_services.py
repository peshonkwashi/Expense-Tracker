"""Unit tests for the behavioural and recommendation layers."""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from services import behavioural, recommendation  # noqa: E402
from tests.helpers import insert_transaction, temp_database  # noqa: E402


class SalaryCycleTests(unittest.TestCase):
    def test_last_salary_date_in_the_same_month(self):
        self.assertEqual(behavioural.last_salary_date(date(2026, 3, 28), 25),
                         date(2026, 3, 25))

    def test_last_salary_date_rolls_back_a_month(self):
        self.assertEqual(behavioural.last_salary_date(date(2026, 3, 10), 25),
                         date(2026, 2, 25))

    def test_rolls_back_across_a_year_boundary(self):
        self.assertEqual(behavioural.last_salary_date(date(2026, 1, 3), 25),
                         date(2025, 12, 25))

    def test_pay_day_31_clamps_to_short_months(self):
        # Someone paid on the 31st is paid on the 30th in April.
        self.assertEqual(behavioural.last_salary_date(date(2026, 4, 30), 31),
                         date(2026, 4, 30))
        self.assertEqual(behavioural.last_salary_date(date(2026, 3, 1), 31),
                         date(2026, 2, 28))

    def test_cycle_position_counts_from_payday(self):
        self.assertEqual(behavioural.cycle_position(date(2026, 3, 25), 25), 0)
        self.assertEqual(behavioural.cycle_position(date(2026, 3, 30), 25), 5)

    def test_cycle_week_buckets(self):
        self.assertEqual(behavioural.cycle_week(date(2026, 3, 25), 25), 1)
        self.assertEqual(behavioural.cycle_week(date(2026, 3, 31), 25), 1)
        self.assertEqual(behavioural.cycle_week(date(2026, 4, 2), 25), 2)

    def test_cycle_week_folds_the_tail_of_the_cycle_into_week_four(self):
        # Day 28+ must not become a fifth, two-day bucket.
        self.assertEqual(behavioural.cycle_week(date(2026, 4, 21), 25), 4)
        self.assertEqual(behavioural.cycle_week(date(2026, 4, 24), 25), 4)


class LearningPhaseTests(unittest.TestCase):
    def test_no_data_means_not_started(self):
        with temp_database() as (conn, user_id):
            user = conn.execute('SELECT * FROM User').fetchone()
            status = behavioural.learning_status(conn, user)
            self.assertFalse(status['complete'])
            self.assertEqual(status['days_observed'], 0)

    def test_incomplete_when_span_is_too_short(self):
        with temp_database() as (conn, user_id):
            today = date(2026, 3, 20)
            for day in range(1, 20):
                insert_transaction(conn, user_id, f'2026-03-{day:02d}',
                                   'Shoprite Manda Hill', 100.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            status = behavioural.learning_status(conn, user, today=today)
            self.assertFalse(status['complete'])
            self.assertIn('more day', status['message'])

    def test_incomplete_when_too_few_transactions(self):
        # A long span with almost no transactions is not enough to model.
        with temp_database() as (conn, user_id):
            today = date(2026, 4, 15)
            insert_transaction(conn, user_id, '2026-01-01', 'Shoprite', 100.0)
            insert_transaction(conn, user_id, '2026-04-01', 'Shoprite', 100.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            status = behavioural.learning_status(conn, user, today=today)
            self.assertFalse(status['complete'])
            self.assertIn('more are needed', status['message'])

    def test_completes_with_enough_span_and_volume(self):
        with temp_database() as (conn, user_id):
            today = date(2026, 3, 5)
            for day in range(1, 21):
                insert_transaction(conn, user_id, f'2026-01-{day:02d}',
                                   'Shoprite Manda Hill', 100.0)
            insert_transaction(conn, user_id, '2026-03-01', 'Shoprite', 100.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            status = behavioural.learning_status(conn, user, today=today)
            self.assertTrue(status['complete'])
            self.assertEqual(status['progress'], 100.0)


class CycleProfileTests(unittest.TestCase):
    def test_weeks_are_averaged_per_cycle_not_summed_over_history(self):
        # Three completed cycles spending 300 each in week 1 is a typical cycle
        # of 300, not 900. Summing made an 8-month history read in the tens of
        # thousands against a four-figure salary.
        with temp_database(salary=8000.0, salary_day=25) as (conn, user_id):
            for month in (1, 2, 3):
                insert_transaction(conn, user_id, f'2026-{month:02d}-26',
                                   'Shoprite Manda Hill', 300.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user, today=date(2026, 5, 10))

            self.assertEqual(profile['cycles'], 3)
            self.assertEqual(profile['weeks']['Week 1'], 300.0)
            self.assertEqual(profile['total'], 300.0)

    def test_a_typical_cycle_is_comparable_to_the_salary(self):
        with temp_database(salary=8000.0, salary_day=25) as (conn, user_id):
            for month in range(1, 7):
                insert_transaction(conn, user_id, f'2026-{month:02d}-26',
                                   'Shoprite Manda Hill', 2000.0)
                insert_transaction(conn, user_id, f'2026-{month:02d}-05',
                                   'Fuel Puma Kabulonga', 1500.0,
                                   category='Transport')
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user, today=date(2026, 8, 10))
            self.assertLess(profile['total'], 8000.0)

    def test_in_progress_cycle_is_excluded_from_the_average(self):
        with temp_database(salary=8000.0, salary_day=25) as (conn, user_id):
            for month in (1, 2, 3):
                insert_transaction(conn, user_id, f'2026-{month:02d}-26',
                                   'Shoprite Manda Hill', 300.0)
            # A cycle two days old with almost nothing spent in it yet.
            insert_transaction(conn, user_id, '2026-04-26',
                               'Shoprite Manda Hill', 10.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user, today=date(2026, 4, 27))

            self.assertEqual(profile['cycles'], 3)
            self.assertEqual(profile['weeks']['Week 1'], 300.0)

    def test_falls_back_to_the_current_cycle_when_it_is_all_there_is(self):
        with temp_database(salary=8000.0, salary_day=25) as (conn, user_id):
            insert_transaction(conn, user_id, '2026-04-26',
                               'Shoprite Manda Hill', 250.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user, today=date(2026, 4, 27))
            self.assertTrue(profile['partial_only'])
            self.assertEqual(profile['weeks']['Week 1'], 250.0)

    def test_no_data_gives_an_empty_profile(self):
        with temp_database() as (conn, user_id):
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user)
            self.assertEqual(profile['total'], 0)
            self.assertFalse(profile['has_surge'])

    def test_detects_a_post_salary_surge(self):
        with temp_database(salary_day=25) as (conn, user_id):
            for _ in range(5):
                insert_transaction(conn, user_id, '2026-03-26',
                                   'Mr Price Clothing', 500.0, category='Shopping')
            insert_transaction(conn, user_id, '2026-03-20', 'Shoprite', 200.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user)
            self.assertTrue(profile['has_surge'])
            self.assertGreater(profile['surge_share'], 80)

    def test_even_spending_is_not_flagged(self):
        with temp_database(salary_day=25) as (conn, user_id):
            for day in (26, 3, 10, 17):
                month = 3 if day == 26 else 4
                insert_transaction(conn, user_id, f'2026-{month:02d}-{day:02d}',
                                   'Shoprite Manda Hill', 250.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            profile = behavioural.cycle_profile(conn, user)
            self.assertFalse(profile['has_surge'])


class BudgetTests(unittest.TestCase):
    def _seed(self, conn, user_id, category, amounts, start_month=1):
        for offset, amount in enumerate(amounts):
            insert_transaction(conn, user_id, f'2026-{start_month + offset:02d}-10',
                               f'{category} spend', amount, category=category)

    def test_essentials_receive_a_buffer(self):
        with temp_database(salary=20000.0) as (conn, user_id):
            self._seed(conn, user_id, 'Groceries', [1000, 1000, 1000, 1000, 1000])
            user = conn.execute('SELECT * FROM User').fetchone()
            budget = recommendation.build_budget(conn, user, today=date(2026, 6, 5))
            groceries = next(r for r in budget['rows'] if r['category'] == 'Groceries')
            self.assertAlmostEqual(groceries['recommended'],
                                   groceries['forecast'] * (1 + config.ESSENTIAL_BUFFER),
                                   places=2)

    def test_discretionary_is_trimmed_when_salary_cannot_cover_it(self):
        with temp_database(salary=5000.0) as (conn, user_id):
            self._seed(conn, user_id, 'Housing', [4000, 4000, 4000, 4000, 4000])
            self._seed(conn, user_id, 'Entertainment', [2000, 2000, 2000, 2000, 2000])
            user = conn.execute('SELECT * FROM User').fetchone()
            budget = recommendation.build_budget(conn, user, today=date(2026, 6, 5))

            self.assertTrue(budget['overcommitted'])
            entertainment = next(r for r in budget['rows']
                                 if r['category'] == 'Entertainment')
            self.assertLess(entertainment['recommended'], entertainment['forecast'])
            # The plan must never allocate more than the salary.
            self.assertLessEqual(budget['allocated_total'], budget['salary'] + 0.01)

    def test_comfortable_salary_is_not_trimmed(self):
        with temp_database(salary=30000.0) as (conn, user_id):
            self._seed(conn, user_id, 'Groceries', [1000] * 5)
            self._seed(conn, user_id, 'Entertainment', [500] * 5)
            user = conn.execute('SELECT * FROM User').fetchone()
            budget = recommendation.build_budget(conn, user, today=date(2026, 6, 5))
            self.assertFalse(budget['overcommitted'])
            self.assertGreater(budget['recommended_savings'], 0)

    def test_every_allocation_carries_an_explanation(self):
        with temp_database() as (conn, user_id):
            self._seed(conn, user_id, 'Groceries', [1000] * 5)
            user = conn.execute('SELECT * FROM User').fetchone()
            budget = recommendation.build_budget(conn, user, today=date(2026, 6, 5))
            for row in budget['rows']:
                self.assertTrue(row['explanation'].strip())

    def test_recommendations_are_persisted_and_updated_not_duplicated(self):
        with temp_database() as (conn, user_id):
            self._seed(conn, user_id, 'Groceries', [1000] * 5)
            user = conn.execute('SELECT * FROM User').fetchone()
            budget = recommendation.build_budget(conn, user, today=date(2026, 6, 5))
            recommendation.persist_recommendations(conn, user_id, budget)
            recommendation.persist_recommendations(conn, user_id, budget)
            count = conn.execute(
                'SELECT COUNT(*) AS n FROM BudgetRecommendation').fetchone()['n']
            self.assertEqual(count, len(budget['rows']))


class NudgeTests(unittest.TestCase):
    def _budget(self, spent, recommended=1000.0):
        return {
            'salary': 10000.0, 'month': '2026-06', 'overcommitted': False,
            'scale': 1.0, 'essential_total': 5000.0, 'goal_total': 0.0,
            'recommended_savings': 1000.0,
            'rows': [{
                'category': 'Dining Out', 'recommended': recommended, 'spent': spent,
                'used_pct': spent / recommended * 100,
                'remaining': recommended - spent,
                'status': ('over' if spent >= recommended
                           else 'warning' if spent / recommended >= config.NUDGE_THRESHOLD
                           else 'ok'),
                'explanation': 'because',
            }],
        }

    def test_no_nudge_below_the_threshold(self):
        nudges = recommendation.generate_nudges(self._budget(500.0), None, None)
        self.assertEqual(nudges, [])

    def test_warning_at_the_threshold(self):
        nudges = recommendation.generate_nudges(self._budget(800.0), None, None)
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]['severity'], 'warning')

    def test_critical_when_over_budget(self):
        nudges = recommendation.generate_nudges(self._budget(1100.0), None, None)
        self.assertEqual(nudges[0]['severity'], 'critical')

    def test_critical_nudges_sort_first(self):
        budget = self._budget(1100.0)
        subs = {'count': 3, 'monthly_total': 500.0, 'annual_total': 6000.0}
        nudges = recommendation.generate_nudges(budget, None, subs)
        self.assertEqual(nudges[0]['severity'], 'critical')
        self.assertEqual(nudges[-1]['severity'], 'info')


class SavingsTests(unittest.TestCase):
    def test_pool_is_allocated_by_due_date_not_duplicated(self):
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-25', 'SALARY', 10000.0,
                               txn_type='CREDIT', category='Uncategorised',
                               is_salary=1)
            insert_transaction(conn, user_id, '2026-01-26', 'Shoprite', 4000.0)

            soon = (date.today() + timedelta(days=60)).isoformat()
            later = (date.today() + timedelta(days=300)).isoformat()
            conn.execute('INSERT INTO SavingsGoal (goal_name, target_amount, '
                         'target_date, user_id) VALUES (?, ?, ?, ?)',
                         ('Emergency fund', 5000.0, soon, user_id))
            conn.execute('INSERT INTO SavingsGoal (goal_name, target_amount, '
                         'target_date, user_id) VALUES (?, ?, ?, ?)',
                         ('Laptop', 5000.0, later, user_id))
            conn.commit()

            user = conn.execute('SELECT * FROM User').fetchone()
            position = recommendation.savings_position(conn, user, None)

            self.assertEqual(position['pool'], 6000.0)
            first, second = position['goals']
            self.assertEqual(first['goal_name'], 'Emergency fund')
            self.assertEqual(first['saved'], 5000.0)
            self.assertEqual(first['progress'], 100.0)
            # The same Kwacha must not be counted twice.
            self.assertEqual(second['saved'], 1000.0)
            self.assertEqual(first['saved'] + second['saved'], position['pool'])

    def test_monthly_requirement_is_shortfall_over_months(self):
        with temp_database() as (conn, user_id):
            target = (date.today() + timedelta(days=120)).isoformat()
            conn.execute('INSERT INTO SavingsGoal (goal_name, target_amount, '
                         'target_date, user_id) VALUES (?, ?, ?, ?)',
                         ('Car', 12000.0, target, user_id))
            conn.commit()
            user = conn.execute('SELECT * FROM User').fetchone()
            position = recommendation.savings_position(conn, user, None)
            goal = position['goals'][0]
            self.assertGreater(goal['monthly_required'], 0)
            self.assertAlmostEqual(
                goal['monthly_required'] * goal['months_remaining'], 12000.0,
                delta=goal['months_remaining'])

    def test_pool_never_goes_negative(self):
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-26', 'Shoprite', 4000.0)
            user = conn.execute('SELECT * FROM User').fetchone()
            position = recommendation.savings_position(conn, user, None)
            self.assertEqual(position['pool'], 0.0)


class MonthlyReportTests(unittest.TestCase):
    def test_reports_income_expenditure_and_variance(self):
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-05-25', 'SALARY', 10000.0,
                               txn_type='CREDIT', category='Uncategorised',
                               is_salary=1)
            insert_transaction(conn, user_id, '2026-05-02', 'Shoprite', 1200.0)
            user = conn.execute('SELECT * FROM User').fetchone()

            report = recommendation.monthly_report(conn, user, '2026-05')
            self.assertEqual(report['income'], 10000.0)
            self.assertEqual(report['expenditure'], 1200.0)
            self.assertEqual(report['net'], 8800.0)
            self.assertFalse(report['has_budget'])

    def test_salary_credit_is_excluded_from_category_spend(self):
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-05-25', 'SALARY', 10000.0,
                               txn_type='CREDIT', category='Uncategorised',
                               is_salary=1)
            spend = recommendation.actual_spend(conn, user_id, '2026-05')
            self.assertEqual(spend, {})


if __name__ == '__main__':
    unittest.main()
