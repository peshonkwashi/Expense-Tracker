"""Unit tests for the Machine Learning Processing Layer."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from ml import categorization, evaluation, forecasting, subscriptions  # noqa: E402
from tests.helpers import insert_transaction, temp_database  # noqa: E402


class NormalisationTests(unittest.TestCase):
    def test_strips_punctuation_and_case(self):
        self.assertEqual(categorization.normalise('NETFLIX.COM  *8837'),
                         'netflix com 8837')

    def test_handles_empty_input(self):
        self.assertEqual(categorization.normalise(None), '')
        self.assertEqual(categorization.normalise('  '), '')


class RuleSeedTests(unittest.TestCase):
    def test_known_merchants(self):
        cases = {
            'Shoprite Manda Hill': 'Groceries',
            'ZESCO Prepaid Units': 'Utilities',
            'Fuel Puma Kabulonga': 'Transport',
            'NETFLIX.COM': 'Subscriptions',
            'KFC Cairo Road': 'Dining Out',
            'Rent Payment Landlord': 'Housing',
            'Link Pharmacy': 'Healthcare',
            'Bayport Loan Repayment': 'Loan & Debt',
        }
        for description, expected in cases.items():
            with self.subTest(description=description):
                self.assertEqual(categorization.rule_category(description), expected)

    def test_mobile_money_beats_airtime(self):
        # "Airtel Money" is a wallet transfer, not an airtime purchase.
        self.assertEqual(categorization.rule_category('Airtel Money Transfer'),
                         'Transfers')
        self.assertEqual(categorization.rule_category('Airtel Airtime Recharge'),
                         'Airtime & Data')

    def test_unknown_merchant_is_unmatched(self):
        self.assertIsNone(categorization.rule_category('Zzz Unknown Merchant'))

    def test_classify_falls_back_to_default(self):
        name, confidence, source = categorization.classify('Zzz Unknown Merchant')
        self.assertEqual(name, 'Uncategorised')
        self.assertEqual(source, 'default')
        self.assertEqual(confidence, 0.0)


class TrainingTests(unittest.TestCase):
    def test_refuses_to_train_on_too_little_data(self):
        # Without the seed, a single labelled transaction is too little to train.
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-01', 'Shoprite', 100.0)
            result = categorization.train_categorization_model(
                conn, user_id, include_seed=False)
            self.assertFalse(result['trained'])
            self.assertIn('needed to train', result['reason'])

    def test_seed_lets_the_model_train_from_the_first_upload(self):
        # With the curated seed included (the default), a model exists even
        # when the user has only just started (section 3.7.1).
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-01', 'Shoprite Manda Hill',
                               100.0, category='Groceries')
            result = categorization.train_categorization_model(conn, user_id)
            self.assertTrue(result['trained'], result['reason'])
            self.assertGreater(result['samples'], 20)

    def test_trains_and_records_metrics(self):
        with temp_database() as (conn, user_id):
            merchants = [('Shoprite Manda Hill', 'Groceries'),
                         ('Pick n Pay Levy', 'Groceries'),
                         ('Fuel Puma Kabulonga', 'Transport'),
                         ('Engen Great East Road', 'Transport'),
                         ('ZESCO Prepaid Units', 'Utilities'),
                         ('LWSC Water Bill', 'Utilities')]
            for index in range(6):
                for description, category in merchants:
                    insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                       description, 100.0 + index, category=category)

            result = categorization.train_categorization_model(conn, user_id)
            self.assertTrue(result['trained'], result['reason'])
            self.assertIn(result['algorithm'], {'MultinomialNB', 'RandomForest'})
            self.assertIsNotNone(result['f1'])

            metric = conn.execute(
                "SELECT * FROM ModelMetrics WHERE model_type = 'CATEGORISATION'"
            ).fetchone()
            self.assertIsNotNone(metric)
            self.assertEqual(metric['sample_count'], result['samples'])

    def test_trained_model_generalises_to_an_unseen_merchant(self):
        with temp_database() as (conn, user_id):
            for index in range(12):
                insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                   'Shoprite Manda Hill', 100.0, category='Groceries')
                insert_transaction(conn, user_id, f'2026-02-{index + 1:02d}',
                                   'Fuel Puma Station', 100.0, category='Transport')
            trained = categorization.train_categorization_model(conn, user_id)
            self.assertTrue(trained['trained'], trained['reason'])
            # "Shoprite Kabwata" was never seen, but shares a token with training data.
            name, confidence, source = categorization.classify('Shoprite Kabwata')
            self.assertEqual(name, 'Groceries')
            self.assertEqual(source, 'model')
            self.assertGreaterEqual(confidence, config.MODEL_CONFIDENCE_FLOOR)
            categorization.reset_model_cache()


class FeatureEngineeringTests(unittest.TestCase):
    """Section 3.7.2: RF weighs amount and date features; NB stays text-only."""

    def test_build_features_extracts_amount_and_date(self):
        frame = categorization.build_features(
            ['Shoprite Manda Hill 8837'], [1200.0], ['2026-05-04'])  # a Monday
        row = frame.iloc[0]
        self.assertEqual(row['text'], 'shoprite manda hill 8837')
        self.assertEqual(row['amount'], 1200.0)
        self.assertEqual(row['day_of_month'], 4)
        self.assertEqual(row['day_of_week'], 0)  # 2026-05-04 is a Monday

    def test_build_features_defaults_when_amount_and_date_missing(self):
        frame = categorization.build_features(['Some Shop'])
        row = frame.iloc[0]
        self.assertEqual(row['amount'], 0.0)
        self.assertEqual(row['day_of_week'], 0)
        self.assertEqual(row['day_of_month'], 1)

    def test_naive_bayes_is_text_only(self):
        nb = categorization._candidate_pipelines()['MultinomialNB']
        columns = nb.named_steps['features'].transformers
        used = {name for name, _, _ in columns}
        self.assertEqual(used, {'tfidf'})

    def test_random_forest_uses_text_and_non_text_features(self):
        rf = categorization._candidate_pipelines()['RandomForest']
        columns = rf.named_steps['features'].transformers
        used = {name for name, _, _ in columns}
        self.assertEqual(used, {'tfidf', 'amount', 'day_of_week', 'day_of_month'})

    def test_random_forest_feature_matrix_includes_non_text_columns(self):
        with temp_database() as (conn, user_id):
            for index in range(12):
                insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                   'Shoprite Manda Hill', 100.0, category='Groceries')
                insert_transaction(conn, user_id, f'2026-02-{index + 1:02d}',
                                   'Fuel Puma Station', 100.0, category='Transport')
            frame = categorization.training_frame(conn, user_id)
            rf = categorization._candidate_pipelines()['RandomForest']
            rf.fit(frame[categorization.FEATURE_COLUMNS], frame['category_name'])
            names = list(rf.named_steps['features'].get_feature_names_out())
            self.assertTrue(any(n.startswith('amount__') for n in names))
            self.assertTrue(any('day_of_week' in n for n in names))
            self.assertTrue(any(n.startswith('tfidf__') for n in names))

    def test_classify_accepts_amount_and_date(self):
        with temp_database() as (conn, user_id):
            for index in range(12):
                insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                   'Shoprite Manda Hill', 120.0, category='Groceries')
                insert_transaction(conn, user_id, f'2026-02-{index + 1:02d}',
                                   'Fuel Puma Station', 800.0, category='Transport')
            categorization.train_categorization_model(conn, user_id)
            name, confidence, source = categorization.classify(
                'Shoprite Kabwata', 130.0, '2026-03-05')
            self.assertEqual(name, 'Groceries')
            self.assertEqual(source, 'model')
            categorization.reset_model_cache()


class EvaluationTests(unittest.TestCase):
    """Section 3.7.4: the cross-validated NB-vs-RF comparison used by the report."""

    def _labelled_frame(self, conn, user_id):
        merchants = [('Shoprite Manda Hill', 'Groceries'),
                     ('Pick n Pay Levy', 'Groceries'),
                     ('Fuel Puma Kabulonga', 'Transport'),
                     ('Engen Great East Road', 'Transport'),
                     ('ZESCO Prepaid Units', 'Utilities'),
                     ('LWSC Water Bill', 'Utilities')]
        for index in range(6):
            for description, category in merchants:
                insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                   description, 100.0 + index, category=category)
        return categorization.training_frame(conn, user_id)

    def test_evaluates_both_candidates(self):
        with temp_database() as (conn, user_id):
            frame = self._labelled_frame(conn, user_id)
            results, n_splits = evaluation.evaluate_candidates(frame)
            self.assertEqual(set(results), {'MultinomialNB', 'RandomForest'})
            self.assertGreaterEqual(n_splits, 2)
            for r in results.values():
                self.assertTrue(0.0 <= r['macro_f1'] <= 1.0)
                self.assertIn('report', r)
                # Confusion matrix is square over the label set.
                self.assertEqual(r['confusion'].shape,
                                 (len(r['labels']), len(r['labels'])))

    def test_best_model_and_target(self):
        with temp_database() as (conn, user_id):
            frame = self._labelled_frame(conn, user_id)
            results, _ = evaluation.evaluate_candidates(frame)
            name, f1 = evaluation.best_model(results)
            self.assertIn(name, results)
            self.assertEqual(f1, max(r['macro_f1'] for r in results.values()))
            self.assertEqual(evaluation.meets_target(0.90), True)
            self.assertEqual(evaluation.meets_target(0.50), False)

    def test_empty_frame_raises(self):
        import pandas as pd
        with self.assertRaises(ValueError):
            evaluation.evaluate_candidates(pd.DataFrame())

    def test_single_category_raises(self):
        with temp_database() as (conn, user_id):
            for index in range(6):
                insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                   'Shoprite Manda Hill', 100.0, category='Groceries')
            # Evaluate the user's own data in isolation (no seed), which has
            # only one category, so cross-validation cannot run.
            frame = categorization.training_frame(conn, user_id, include_seed=False)
            with self.assertRaises(ValueError):
                evaluation.evaluate_candidates(frame)


class SeedDatasetTests(unittest.TestCase):
    """Section 3.7.1: the curated labelled seed dataset for model bootstrapping."""

    def setUp(self):
        categorization.reset_seed_cache()

    def tearDown(self):
        categorization.reset_seed_cache()

    def test_seed_loads_with_the_feature_columns(self):
        seed = categorization.load_seed_frame()
        self.assertFalse(seed.empty)
        for column in categorization.FEATURE_COLUMNS + ['category_name']:
            self.assertIn(column, seed.columns)
        self.assertTrue((seed['category_source'] == 'seed').all())
        # Text is preprocessed (lowercased, punctuation stripped).
        self.assertTrue(seed['text'].str.islower().all())

    def test_every_seed_category_exists_in_the_schema(self):
        with temp_database() as (conn, _user_id):
            valid = {row['category_name'] for row in conn.execute(
                'SELECT category_name FROM Category')}
        seed = categorization.load_seed_frame()
        self.assertTrue(set(seed['category_name']).issubset(valid))

    def test_training_frame_is_user_data_only_by_default(self):
        # Default is the user's own data (the measure NFR-02 is about); the seed
        # is appended only on request.
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-01', 'Shoprite Manda Hill',
                               100.0, category='Groceries')
            default = categorization.training_frame(conn, user_id)
            with_seed = categorization.training_frame(conn, user_id,
                                                      include_seed=True)
            self.assertEqual(len(default), 1)
            self.assertGreater(len(with_seed), len(default))

    def test_mature_user_is_trained_without_the_seed(self):
        # Enough of their own data across categories: no bootstrap, so the
        # reported F1 reflects the user's real transactions.
        merchants = [('Shoprite Manda Hill', 'Groceries'),
                     ('Fuel Puma Kabulonga', 'Transport'),
                     ('ZESCO Prepaid Units', 'Utilities')]
        with temp_database() as (conn, user_id):
            for index in range(10):
                for description, category in merchants:
                    insert_transaction(conn, user_id, f'2026-01-{index + 1:02d}',
                                       description, 100.0 + index, category=category)
            result = categorization.train_categorization_model(conn, user_id)
            self.assertTrue(result['trained'], result['reason'])
            self.assertFalse(result['bootstrapped'])

    def test_seed_disabled_by_config(self):
        import config
        original = config.USE_SEED_DATASET
        config.USE_SEED_DATASET = False
        categorization.reset_seed_cache()
        try:
            self.assertTrue(categorization.load_seed_frame().empty)
        finally:
            config.USE_SEED_DATASET = original
            categorization.reset_seed_cache()


class SubscriptionTests(unittest.TestCase):
    def _rows(self, entries):
        return [{'transaction_id': index, 'transaction_date': when,
                 'description': description, 'amount': amount}
                for index, (when, description, amount) in enumerate(entries, 1)]

    def test_detects_a_stable_monthly_charge(self):
        detected = subscriptions.find_recurring(self._rows([
            ('2026-01-08', 'NETFLIX.COM 8837', 189.00),
            ('2026-02-08', 'NETFLIX.COM 9021', 189.00),
            ('2026-03-08', 'NETFLIX.COM 1130', 189.00),
        ]))
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0]['monthly_amount'], 189.00)
        self.assertEqual(detected[0]['annualised'], 2268.00)

    def test_ignores_variable_amounts_at_the_same_merchant(self):
        # Three supermarket trips a month apart are not a subscription.
        detected = subscriptions.find_recurring(self._rows([
            ('2026-01-08', 'Shoprite Manda Hill', 450.00),
            ('2026-02-08', 'Shoprite Manda Hill', 1300.00),
            ('2026-03-08', 'Shoprite Manda Hill', 780.00),
        ]))
        self.assertEqual(detected, [])

    def test_ignores_charges_that_are_not_monthly(self):
        detected = subscriptions.find_recurring(self._rows([
            ('2026-01-01', 'Gym Day Pass', 50.00),
            ('2026-01-08', 'Gym Day Pass', 50.00),
            ('2026-01-15', 'Gym Day Pass', 50.00),
        ]))
        self.assertEqual(detected, [])

    def test_ignores_a_single_charge(self):
        detected = subscriptions.find_recurring(self._rows([
            ('2026-01-08', 'NETFLIX.COM', 189.00),
            ('2026-02-08', 'NETFLIX.COM', 189.00),
        ]))
        self.assertEqual(detected, [])

    def test_tolerates_a_small_price_change(self):
        detected = subscriptions.find_recurring(self._rows([
            ('2026-01-08', 'Spotify Premium', 79.00),
            ('2026-02-08', 'Spotify Premium', 82.00),
            ('2026-03-08', 'Spotify Premium', 79.00),
        ]))
        self.assertEqual(len(detected), 1)


class ForecastingTests(unittest.TestCase):
    def _seed_months(self, conn, user_id, category, amounts, start_month=1):
        for offset, amount in enumerate(amounts):
            month = start_month + offset
            insert_transaction(conn, user_id, f'2026-{month:02d}-10',
                               f'{category} purchase', amount, category=category)

    def test_excludes_the_in_progress_month(self):
        with temp_database() as (conn, user_id):
            today = date(2026, 6, 15)
            self._seed_months(conn, user_id, 'Groceries', [1000, 1000, 1000, 1000, 1000])
            insert_transaction(conn, user_id, '2026-06-02', 'Groceries purchase',
                               50.0, category='Groceries')

            matrix = forecasting.monthly_matrix(conn, user_id, today=today)
            self.assertNotIn('2026-06', [str(period) for period in matrix.index])
            # The partial month must not drag the forecast down towards 50.
            forecasts = forecasting.generate_forecasts(conn, user_id, today=today)
            self.assertGreater(forecasts['Groceries']['amount'], 800)

    def test_missing_months_are_filled_with_zero_not_skipped(self):
        with temp_database() as (conn, user_id):
            insert_transaction(conn, user_id, '2026-01-10', 'Groceries purchase',
                               1000.0, category='Groceries')
            insert_transaction(conn, user_id, '2026-04-10', 'Groceries purchase',
                               1000.0, category='Groceries')
            matrix = forecasting.monthly_matrix(conn, user_id, today=date(2026, 6, 1))
            self.assertEqual(len(matrix), 4)
            self.assertEqual(float(matrix['Groceries'].iloc[1]), 0.0)

    def test_short_history_uses_the_mean_not_arima(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Groceries', [900, 1100])
            forecasts = forecasting.generate_forecasts(conn, user_id,
                                                       today=date(2026, 4, 1))
            self.assertEqual(forecasts['Groceries']['method'], 'mean')
            self.assertAlmostEqual(forecasts['Groceries']['amount'], 1000.0, places=2)

    def test_long_history_selects_a_measured_method(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Groceries',
                              [1000, 1150, 980, 1210, 1050, 1120])
            forecasts = forecasting.generate_forecasts(conn, user_id,
                                                       today=date(2026, 8, 1))
            self.assertIn(forecasts['Groceries']['method'], forecasting.CANDIDATES)
            self.assertGreater(forecasts['Groceries']['amount'], 0)
            self.assertIn('lowest error', forecasts['Groceries']['basis'])

    def test_selection_prefers_arima_on_an_autoregressive_series(self):
        # A clean alternating pattern is exactly what AR(1) is for.
        method, error = forecasting.select_method(
            [1500, 300, 1500, 300, 1500, 300, 1500])
        self.assertEqual(method, 'arima')
        self.assertLess(error, 50)

    def test_selection_falls_back_when_history_is_short(self):
        method, error = forecasting.select_method([1000, 1100, 1050, 1080])
        self.assertEqual(method, 'mean')
        self.assertIsNone(error)

    def test_forecasts_are_never_negative(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Groceries', [2000, 1500, 1000, 500, 100])
            forecasts = forecasting.generate_forecasts(conn, user_id,
                                                       today=date(2026, 7, 1))
            self.assertGreaterEqual(forecasts['Groceries']['amount'], 0)

    def test_dormant_category_is_dropped(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Entertainment', [500, 400])
            self._seed_months(conn, user_id, 'Groceries',
                              [900, 900, 900, 900, 900, 900])
            forecasts = forecasting.generate_forecasts(conn, user_id,
                                                       today=date(2026, 8, 1))
            self.assertIn('Groceries', forecasts)
            self.assertNotIn('Entertainment', forecasts)

    def test_backtest_reports_mae(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Groceries',
                              [1000, 1000, 1000, 1000, 1000])
            evaluation = forecasting.backtest(conn, user_id, today=date(2026, 7, 1))
            self.assertTrue(evaluation['evaluated'], evaluation['reason'])
            self.assertLess(evaluation['mae'], 50)
            self.assertTrue(evaluation['meets_target'])

    def test_backtest_needs_enough_history(self):
        with temp_database() as (conn, user_id):
            self._seed_months(conn, user_id, 'Groceries', [1000])
            evaluation = forecasting.backtest(conn, user_id, today=date(2026, 3, 1))
            self.assertFalse(evaluation['evaluated'])


if __name__ == '__main__':
    unittest.main()
