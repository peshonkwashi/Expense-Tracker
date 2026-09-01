"""Integration test: CSV upload through to dashboard output (section 5.10.2).

Walks the full data flow described in section 5.6.5 — ingestion, ML processing,
forecasting, recommendation — against the synthetic statement, and asserts the
NFR-02 and NFR-03 acceptance criteria on the result.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from ml import forecasting, subscriptions  # noqa: E402
from services import behavioural, ingestion, recommendation  # noqa: E402
from tests.helpers import build_statement, temp_database  # noqa: E402


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.statement = build_statement()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.statement):
            os.unlink(cls.statement)

    def setUp(self):
        self.context = temp_database(salary=12000.0, salary_day=25)
        self.conn, self.user_id = self.context.__enter__()
        self.report = ingestion.import_statement(
            self.conn, self.user_id, self.statement, salary_amount=12000.0)
        self.pipeline = ingestion.post_import_pipeline(self.conn, self.user_id)
        self.user = self.conn.execute('SELECT * FROM User').fetchone()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_statement_imports_cleanly(self):
        self.assertGreater(self.report['imported'], 150)
        self.assertEqual(self.report['rejected'], [])
        self.assertGreater(self.report['salary_rows'], 5)

    def test_almost_everything_gets_a_category(self):
        uncategorised = self.conn.execute(
            "SELECT COUNT(*) AS n FROM Transaction_Record t "
            "JOIN Category c ON t.category_id = c.category_id "
            "WHERE t.transaction_type = 'DEBIT' AND c.category_name = 'Uncategorised'"
        ).fetchone()['n']
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM Transaction_Record "
            "WHERE transaction_type = 'DEBIT'").fetchone()['n']
        self.assertLess(uncategorised / total, 0.05)

    def test_categorisation_meets_the_nfr02_target(self):
        training = self.pipeline['training']
        self.assertTrue(training['trained'], training['reason'])
        self.assertIsNotNone(training['f1'])
        self.assertGreaterEqual(training['f1'], config.TARGET_F1,
                                f"macro F1 {training['f1']:.3f} is below the "
                                f"NFR-02 target of {config.TARGET_F1}")

    def test_learning_phase_completes_on_eight_months_of_history(self):
        status = behavioural.learning_status(self.conn, self.user)
        self.assertTrue(status['complete'], status['message'])

    def test_subscriptions_are_detected(self):
        detected = subscriptions.detect_subscriptions(self.conn, self.user_id)
        labels = ' '.join(item['key'] for item in detected)
        self.assertIn('netflix', labels)
        self.assertGreaterEqual(len(detected), 3)

    def test_forecasts_cover_the_main_categories(self):
        forecasts = forecasting.generate_forecasts(self.conn, self.user_id)
        for category in ('Groceries', 'Transport', 'Housing'):
            self.assertIn(category, forecasts)
            self.assertGreater(forecasts[category]['amount'], 0)

    def test_forecast_error_meets_the_nfr03_target(self):
        evaluation = forecasting.backtest(self.conn, self.user_id)
        self.assertTrue(evaluation['evaluated'], evaluation['reason'])
        self.assertIsNotNone(evaluation['mae_ratio'])
        self.assertLessEqual(
            evaluation['mae_ratio'], config.MAX_MAE_RATIO,
            f"forecast MAE is {evaluation['mae_ratio'] * 100:.1f}% of actual, "
            f"above the NFR-03 target of {config.MAX_MAE_RATIO * 100:.0f}%")

    def test_budget_fits_the_salary_or_reports_a_deficit(self):
        budget = recommendation.build_budget(self.conn, self.user)
        self.assertGreater(len(budget['rows']), 3)
        if budget['essentials_exceed_income']:
            # Essentials above income cannot be allocated away; the system must
            # say so rather than quietly producing an impossible plan.
            self.assertGreater(budget['deficit'], 0)
        else:
            self.assertLessEqual(budget['allocated_total'], budget['salary'] + 0.01)

    def test_dashboard_data_assembles_without_error(self):
        budget = recommendation.build_budget(self.conn, self.user)
        recommendation.persist_recommendations(self.conn, self.user_id, budget)
        cycle = behavioural.cycle_profile(self.conn, self.user)
        subs = subscriptions.subscription_summary(
            subscriptions.detect_subscriptions(self.conn, self.user_id))
        nudges = recommendation.generate_nudges(budget, cycle, subs)

        self.assertIsInstance(nudges, list)
        for nudge in nudges:
            self.assertIn(nudge['severity'], {'critical', 'warning', 'info'})
            self.assertTrue(nudge['message'])
            self.assertTrue(nudge['why'])

        position = recommendation.savings_position(self.conn, self.user, budget)
        self.assertGreaterEqual(position['pool'], 0)


if __name__ == '__main__':
    unittest.main()
