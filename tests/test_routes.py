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
                     '/settings'):
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
                     '/settings'):
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


if __name__ == '__main__':
    unittest.main()
