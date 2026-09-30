"""Route-level tests: authentication, rendering and the security controls.

Uses Flask's test client against a throwaway database so no test touches the
real expense_tracker.db.
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tests.helpers import build_statement  # noqa: E402

PASSWORD = 'test-password-123'
SALARY = 12000.0
SALARY_DAY = 25


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        handle.close()
        self.db_path = handle.name
        self.model_dir = tempfile.mkdtemp()

        self._original = (config.DATABASE_PATH, config.CATEGORISER_PATH)
        config.DATABASE_PATH = self.db_path
        config.CATEGORISER_PATH = os.path.join(self.model_dir, 'categoriser.joblib')

        import database
        database.init_db()

        import app as app_module
        self.app_module = app_module
        app_module.app.config['TESTING'] = True
        # CSRF protection is enforced in production but disabled here so the
        # route tests can POST without a token, which is standard practice.
        app_module.app.config['WTF_CSRF_ENABLED'] = False
        self.client = app_module.app.test_client()

    def tearDown(self):
        config.DATABASE_PATH, config.CATEGORISER_PATH = self._original
        for path in (self.db_path, os.path.join(self.model_dir, 'categoriser.joblib')):
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        try:
            os.rmdir(self.model_dir)
        except OSError:
            pass

    def complete_setup(self):
        return self.client.post('/setup', data={
            'full_name': 'Test User', 'salary_amount': str(int(SALARY)),
            'salary_day': str(SALARY_DAY), 'password': PASSWORD,
            'password_confirm': PASSWORD, 'accept_notice': '1',
        }, follow_redirects=True)


class SetupTests(RouteTestCase):
    def test_fresh_install_redirects_to_setup(self):
        response = self.client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/setup', response.headers['Location'])

    def test_setup_creates_profile_and_signs_in(self):
        response = self.complete_setup()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Upload a bank statement', response.data)

    def test_setup_rejects_mismatched_passwords(self):
        response = self.client.post('/setup', data={
            'full_name': 'Test User', 'salary_amount': '12000',
            'salary_day': '25', 'password': PASSWORD,
            'password_confirm': 'something-else', 'accept_notice': '1',
        }, follow_redirects=True)
        self.assertIn(b'do not match', response.data)

    def test_setup_rejects_a_short_password(self):
        response = self.client.post('/setup', data={
            'full_name': 'Test User', 'salary_amount': '12000',
            'salary_day': '25', 'password': 'short', 'password_confirm': 'short',
            'accept_notice': '1',
        }, follow_redirects=True)
        self.assertIn(b'at least', response.data)

    def test_setup_rejects_an_invalid_salary_day(self):
        response = self.client.post('/setup', data={
            'full_name': 'Test User', 'salary_amount': '12000',
            'salary_day': '45', 'password': PASSWORD,
            'password_confirm': PASSWORD, 'accept_notice': '1',
        }, follow_redirects=True)
        self.assertIn(b'between 1 and 31', response.data)

    def test_data_notice_must_be_acknowledged(self):
        response = self.client.post('/setup', data={
            'full_name': 'Test User', 'salary_amount': '12000',
            'salary_day': '25', 'password': PASSWORD,
            'password_confirm': PASSWORD,
        }, follow_redirects=True)
        self.assertIn(b'acknowledge the data handling notice', response.data)


class AuthenticationTests(RouteTestCase):
    def test_protected_pages_require_a_password(self):
        self.complete_setup()
        self.client.get('/logout')
        for path in ('/', '/transactions', '/budget', '/savings', '/reports',
                     '/household', '/settings'):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login', response.headers['Location'])

    def test_wrong_password_is_rejected(self):
        self.complete_setup()
        self.client.get('/logout')
        response = self.client.post('/login', data={'password': 'wrong'},
                                    follow_redirects=True)
        self.assertIn(b'Incorrect password', response.data)

    def test_correct_password_signs_in(self):
        self.complete_setup()
        self.client.get('/logout')
        response = self.client.post('/login', data={'password': PASSWORD},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Hello', response.data)

    def test_login_does_not_follow_an_external_redirect(self):
        # A crafted ?next= must not bounce the user off-site after sign-in.
        self.complete_setup()
        self.client.get('/logout')
        response = self.client.post('/login?next=https://example.com/steal',
                                    data={'password': PASSWORD},
                                    follow_redirects=False)
        self.assertNotIn('example.com', response.headers.get('Location', ''))

    def test_password_is_not_stored_in_plain_text(self):
        self.complete_setup()
        import database
        conn = database.get_db_connection()
        stored = database.get_setting(conn, 'password_hash')
        conn.close()
        self.assertIsNotNone(stored)
        self.assertNotIn(PASSWORD, stored)


class PageRenderTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.complete_setup()

    def test_all_pages_render_before_any_data_exists(self):
        for path in ('/', '/transactions', '/budget', '/savings', '/reports',
                     '/household', '/settings'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b'Traceback', response.data)

    def test_learning_phase_is_announced_before_recommendations(self):
        response = self.client.get('/budget')
        self.assertIn(b'Not ready yet', response.data)

    def test_upload_rejects_a_non_csv_file(self):
        response = self.client.post('/transactions', data={
            'file': (io.BytesIO(b'not a csv'), 'statement.txt'),
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Only .csv', response.data)

    def test_upload_rejects_a_csv_without_required_columns(self):
        response = self.client.post('/transactions', data={
            'file': (io.BytesIO(b'Foo,Bar\n1,2\n'), 'statement.csv'),
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Missing required column', response.data)


class FullFlowTests(RouteTestCase):
    @classmethod
    def setUpClass(cls):
        # Generated to match this test's own profile. Reading the checked-in
        # sample file made the suite depend on whatever parameters it was last
        # generated with — a statement at a different salary silently stops
        # matching the +/-10% salary-detection band.
        cls.statement = build_statement(salary=SALARY, salary_day=SALARY_DAY)
        with open(cls.statement, 'rb') as handle:
            cls.payload = handle.read()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.statement):
            os.unlink(cls.statement)

    def setUp(self):
        super().setUp()
        self.complete_setup()
        self.upload = self.client.post('/transactions', data={
            'file': (io.BytesIO(self.payload), 'sample_bank_statement.csv'),
        }, content_type='multipart/form-data', follow_redirects=True)

    def test_upload_reports_what_it_did(self):
        self.assertEqual(self.upload.status_code, 200)
        self.assertIn(b'Imported', self.upload.data)
        self.assertIn(b'salary credit', self.upload.data)

    def test_dashboard_shows_a_budget_after_upload(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Recommended savings', response.data)
        self.assertNotIn(b'learning phase in progress', response.data)

    def test_budget_page_lists_allocations(self):
        response = self.client.get('/budget')
        self.assertIn(b'Category allocations', response.data)
        self.assertIn(b'Groceries', response.data)

    def test_reports_page_shows_a_month(self):
        response = self.client.get('/reports')
        self.assertIn(b'Money in', response.data)

    def test_recategorisation_sticks_and_is_attributed_to_the_user(self):
        import database
        conn = database.get_db_connection()
        row = conn.execute(
            "SELECT transaction_id FROM Transaction_Record "
            "WHERE transaction_type = 'DEBIT' LIMIT 1").fetchone()
        conn.close()

        response = self.client.post(
            f"/transactions/{row['transaction_id']}/category",
            data={'category_name': 'Education'}, follow_redirects=True)
        self.assertIn(b'Recategorised as Education', response.data)

        conn = database.get_db_connection()
        updated = conn.execute(
            'SELECT c.category_name, t.category_source FROM Transaction_Record t '
            'JOIN Category c ON t.category_id = c.category_id '
            'WHERE t.transaction_id = ?', (row['transaction_id'],)).fetchone()
        conn.close()
        self.assertEqual(updated['category_name'], 'Education')
        self.assertEqual(updated['category_source'], 'user')

    def test_correction_triggers_automatic_retrain_and_reevaluation(self):
        # A correction must retrain the categoriser AND re-run the forecast
        # hold-out. The import pipeline never backtests, so a FORECASTING metric
        # appearing only after a correction proves the auto re-evaluation ran.
        import database
        conn = database.get_db_connection()
        self.assertIsNone(database.latest_metric(conn, 'FORECASTING'))
        row = conn.execute(
            "SELECT transaction_id FROM Transaction_Record "
            "WHERE transaction_type = 'DEBIT' LIMIT 1").fetchone()
        conn.close()

        response = self.client.post(
            f"/transactions/{row['transaction_id']}/category",
            data={'category_name': 'Education'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        conn = database.get_db_connection()
        after = database.latest_metric(conn, 'FORECASTING')
        conn.close()
        self.assertIsNotNone(after)

    def test_recategorisation_rejects_an_unknown_category(self):
        import database
        conn = database.get_db_connection()
        row = conn.execute(
            'SELECT transaction_id FROM Transaction_Record LIMIT 1').fetchone()
        conn.close()
        response = self.client.post(
            f"/transactions/{row['transaction_id']}/category",
            data={'category_name': 'Nonsense'}, follow_redirects=True)
        self.assertIn(b'Unknown category', response.data)

    def test_recategorising_a_missing_transaction_is_a_404(self):
        response = self.client.post('/transactions/999999/category',
                                    data={'category_name': 'Groceries'})
        self.assertEqual(response.status_code, 404)

    def test_reuploading_the_same_statement_reports_duplicates(self):
        response = self.client.post('/transactions', data={
            'file': (io.BytesIO(self.payload), 'sample_bank_statement.csv'),
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'duplicate', response.data)

    def test_savings_goal_can_be_added_and_removed(self):
        from datetime import date, timedelta
        target = (date.today() + timedelta(days=180)).isoformat()
        response = self.client.post('/savings', data={
            'goal_name': 'Emergency fund', 'target_amount': '15000',
            'target_date': target,
        }, follow_redirects=True)
        self.assertIn(b'Emergency fund', response.data)

        import database
        conn = database.get_db_connection()
        goal = conn.execute('SELECT goal_id FROM SavingsGoal').fetchone()
        conn.close()

        response = self.client.post(f"/savings/{goal['goal_id']}/delete",
                                    follow_redirects=True)
        self.assertIn(b'removed', response.data)

    def test_savings_goal_rejects_a_past_date(self):
        from datetime import date, timedelta
        past = (date.today() - timedelta(days=10)).isoformat()
        response = self.client.post('/savings', data={
            'goal_name': 'Too late', 'target_amount': '1000',
            'target_date': past,
        }, follow_redirects=True)
        self.assertIn(b'must be in the future', response.data)

    def test_retrain_runs_and_reports_metrics(self):
        response = self.client.post('/settings/retrain', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Retrained', response.data)
        self.assertIn(b'Forecast hold-out', response.data)

    def test_database_export_returns_the_file(self):
        response = self.client.get('/settings/export')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        response.close()  # release the file handle send_file opened


class NewFeatureTests(RouteTestCase):
    """Mock AI assistant, mock bank sync, and shared/household budgets."""

    @classmethod
    def setUpClass(cls):
        cls.statement = build_statement(salary=SALARY, salary_day=SALARY_DAY)
        with open(cls.statement, 'rb') as handle:
            cls.payload = handle.read()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.statement):
            os.unlink(cls.statement)

    def setUp(self):
        super().setUp()
        self.complete_setup()
        self.client.post('/transactions', data={
            'file': (io.BytesIO(self.payload), 'sample_bank_statement.csv'),
        }, content_type='multipart/form-data', follow_redirects=True)

    def _txn_count(self):
        import database
        conn = database.get_db_connection()
        n = conn.execute('SELECT COUNT(*) AS n FROM Transaction_Record').fetchone()['n']
        conn.close()
        return n

    def test_bank_sync_downloads_new_transactions(self):
        before = self._txn_count()
        response = self.client.post('/sync-bank', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Bank Sync Successful', response.data)
        self.assertGreater(self._txn_count(), before)

    def test_mobile_money_sync_imports_transactions(self):
        before = self._txn_count()
        response = self.client.post('/sync-momo', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Mobile Money Sync Successful', response.data)
        self.assertGreater(self._txn_count(), before)

    def test_assistant_answers_total_spend(self):
        response = self.client.post(
            '/ask-assistant', json={'question': 'how much have I spent in total?'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['answer'])

    def test_assistant_budget_question_uses_the_recommender(self):
        # Exercises the build_budget / learning_status wiring that previously
        # imported from the wrong modules and 500'd.
        response = self.client.post(
            '/ask-assistant', json={'question': 'what is my budget this month?'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['answer'])

    def test_assistant_handles_an_unrecognised_question(self):
        response = self.client.post(
            '/ask-assistant', json={'question': 'tell me a joke'})
        self.assertEqual(response.status_code, 200)
        self.assertIn("couldn't quite catch", response.get_json()['answer'])

    def test_auto_fetch_itemises_recent_transactions(self):
        response = self.client.post('/auto-fetch-receipts', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Auto-Fetch Complete', response.data)
        import database
        conn = database.get_db_connection()
        n = conn.execute('SELECT COUNT(*) AS n FROM Receipt_Item').fetchone()['n']
        conn.close()
        self.assertGreater(n, 0)

    def test_auto_fetch_only_itemises_email_receipt_merchants(self):
        # The fetcher must skip cash/fuel/utility merchants and only itemise
        # transactions from merchants that email a digital receipt.
        from services import ocr
        self.client.post('/auto-fetch-receipts', follow_redirects=True)
        import database
        conn = database.get_db_connection()
        itemised = conn.execute(
            'SELECT DISTINCT t.description FROM Receipt_Item r '
            'JOIN Transaction_Record t ON r.transaction_id = t.transaction_id'
        ).fetchall()
        conn.close()
        self.assertGreater(len(itemised), 0)
        for row in itemised:
            with self.subTest(merchant=row['description']):
                self.assertTrue(ocr.emails_receipt(row['description']))

    def test_bank_sync_is_idempotent_within_a_day(self):
        first = self.client.post('/sync-bank', follow_redirects=True)
        self.assertIn(b'Bank Sync Successful', first.data)
        count_after_first = self._txn_count()
        second = self.client.post('/sync-bank', follow_redirects=True)
        self.assertIn(b'already up to date', second.data)
        self.assertEqual(self._txn_count(), count_after_first)

    def test_sync_triggers_retrain_and_forecast_reevaluation(self):
        # The import pipeline never backtests, so a FORECASTING metric appearing
        # after a sync proves the sync now retrains + re-evaluates like an upload.
        import database
        conn = database.get_db_connection()
        self.assertIsNone(database.latest_metric(conn, 'FORECASTING'))
        conn.close()
        self.client.post('/sync-bank', follow_redirects=True)
        conn = database.get_db_connection()
        self.assertIsNotNone(database.latest_metric(conn, 'FORECASTING'))
        conn.close()

    def test_assistant_forecasts_next_month(self):
        response = self.client.post(
            '/ask-assistant', json={'question': "what's my forecast for next month?"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['answer'])

    def test_assistant_reports_subscriptions(self):
        response = self.client.post(
            '/ask-assistant', json={'question': 'what are my subscriptions?'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['answer'])

    def test_assistant_reports_savings_goals(self):
        from datetime import date, timedelta
        target = (date.today() + timedelta(days=180)).isoformat()
        self.client.post('/savings', data={
            'goal_name': 'Emergency fund', 'target_amount': '15000',
            'target_date': target}, follow_redirects=True)
        response = self.client.post(
            '/ask-assistant', json={'question': 'how are my savings goals doing?'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Emergency fund', response.get_json()['answer'])

    def test_household_leave_removes_the_partner(self):
        self.client.post('/household', data={
            'partner_name': 'Alex', 'partner_salary': '5000'}, follow_redirects=True)
        import database
        conn = database.get_db_connection()
        self.assertEqual(
            conn.execute('SELECT COUNT(*) AS n FROM User').fetchone()['n'], 2)
        conn.close()

        response = self.client.post('/household/leave', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Invite your partner', response.data)  # single-user view
        conn = database.get_db_connection()
        self.assertEqual(
            conn.execute('SELECT COUNT(*) AS n FROM User').fetchone()['n'], 1)
        conn.close()

    def test_settings_title_block_is_not_corrupted(self):
        # A copy-paste once jammed the auto-fetcher card into {% block title %},
        # rendering the whole card as the page <title>. Guard against a repeat.
        response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<title>Settings</title>', response.data)
        self.assertEqual(response.data.count(b'Email Receipt Auto-Fetcher'), 1)

    def test_household_invite_creates_partner_and_settles(self):
        first = self.client.get('/household')
        self.assertEqual(first.status_code, 200)
        self.assertIn(b'Invite your partner', first.data)

        invited = self.client.post('/household', data={
            'partner_name': 'Alex', 'partner_salary': '5000',
        }, follow_redirects=True)
        self.assertEqual(invited.status_code, 200)
        self.assertIn(b'Household Settlement', invited.data)
        self.assertIn(b'Alex', invited.data)


class DemoFeatureFlagTests(RouteTestCase):
    """DEMO_FEATURES gates all the mock extensions on/off together."""

    def setUp(self):
        super().setUp()
        self.complete_setup()

    def test_demo_entry_points_present_by_default(self):
        dashboard = self.client.get('/').data
        self.assertIn(b'AI Financial Assistant', dashboard)
        self.assertIn(b'Household', dashboard)  # nav link
        self.assertEqual(self.client.get('/household').status_code, 200)
        self.assertIn(b'Sync with Bank', self.client.get('/transactions').data)

    def test_disabled_hides_ui_and_404s_the_routes(self):
        import config
        original = config.DEMO_FEATURES
        config.DEMO_FEATURES = False
        try:
            dashboard = self.client.get('/').data
            self.assertNotIn(b'AI Financial Assistant', dashboard)
            self.assertNotIn(b'Household', dashboard)
            self.assertNotIn(b'Sync with Bank',
                             self.client.get('/transactions').data)
            self.assertNotIn(b'Email Receipt Auto-Fetcher',
                             self.client.get('/settings').data)
            for path in ('/sync-bank', '/sync-momo', '/ask-assistant',
                         '/auto-fetch-receipts', '/household/leave'):
                self.assertEqual(self.client.post(path).status_code, 404, path)
            self.assertEqual(self.client.get('/household').status_code, 404)
            # The core CSV upload path (FR-01) is never gated.
            self.assertIn(b'Manual CSV Upload',
                          self.client.get('/transactions').data)
        finally:
            config.DEMO_FEATURES = original


if __name__ == '__main__':
    unittest.main()
