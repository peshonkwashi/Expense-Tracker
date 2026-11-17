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
        import security
        # Login-throttle state is process-global; clear it so one test's
        # deliberate wrong passwords cannot lock the next test out.
        security.reset_login_attempts()
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
        self.client.post('/logout')
        for path in ('/', '/transactions', '/budget', '/savings', '/reports',
                     '/household', '/settings'):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login', response.headers['Location'])

    def test_wrong_password_is_rejected(self):
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/login', data={'password': 'wrong'},
                                    follow_redirects=True)
        self.assertIn(b'Incorrect password', response.data)

    def test_correct_password_signs_in(self):
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/login', data={'password': PASSWORD},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Hello', response.data)

    def test_login_does_not_follow_an_external_redirect(self):
        # A crafted ?next= must not bounce the user off-site after sign-in.
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/login?next=https://example.com/steal',
                                    data={'password': PASSWORD},
                                    follow_redirects=False)
        self.assertNotIn('example.com', response.headers.get('Location', ''))

    def test_login_rejects_a_protocol_relative_redirect(self):
        # '//host' passes a naive startswith('/') check but browsers resolve it
        # to an external site — the open redirect the original check missed.
        self.complete_setup()
        self.client.post('/logout')
        for target in ('//evil.example', '//evil.example/path', '/\\evil.example'):
            with self.subTest(target=target):
                response = self.client.post('/login?next=' + target,
                                            data={'password': PASSWORD},
                                            follow_redirects=False)
                location = response.headers.get('Location', '')
                self.assertNotIn('evil.example', location)
                self.client.post('/logout')

    def test_login_keeps_a_same_site_redirect(self):
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/login?next=/budget',
                                    data={'password': PASSWORD},
                                    follow_redirects=False)
        self.assertTrue(response.headers['Location'].endswith('/budget'))

    def test_logout_requires_post(self):
        # A GET link could be triggered by any page (<img src="/logout">).
        self.complete_setup()
        self.assertEqual(self.client.get('/logout').status_code, 405)
        # ...and the user is still signed in afterwards.
        self.assertEqual(self.client.get('/', follow_redirects=False).status_code, 200)

    def test_repeated_wrong_passwords_lock_the_client_out(self):
        self.complete_setup()
        self.client.post('/logout')
        for _ in range(config.MAX_LOGIN_ATTEMPTS):
            self.client.post('/login', data={'password': 'wrong-guess'})
        # Even the correct password is refused while the lockout runs.
        response = self.client.post('/login', data={'password': PASSWORD},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 429)
        self.assertIn(b'Too many incorrect attempts', response.data)

    def test_successful_login_resets_the_failure_count(self):
        self.complete_setup()
        self.client.post('/logout')
        for _ in range(config.MAX_LOGIN_ATTEMPTS - 1):
            self.client.post('/login', data={'password': 'wrong-guess'})
        self.client.post('/login', data={'password': PASSWORD})
        self.client.post('/logout')
        # One more miss after the reset must not trigger a lockout.
        response = self.client.post('/login', data={'password': 'wrong-guess'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Incorrect password', response.data)

    def test_assistant_escapes_user_controlled_names(self):
        # Category and goal names are user-entered and are interpolated into
        # the assistant's HTML answer, so they must arrive escaped (no XSS).
        if not config.DEMO_FEATURES:
            self.skipTest('assistant is a demo feature')
        self.complete_setup()
        payload = '<img src=x onerror=alert(1)>'
        self.client.post('/savings', data={'goal_name': payload,
                                           'target_amount': '1000',
                                           'target_date': '2030-01-01'})
        response = self.client.post('/ask-assistant',
                                    json={'question': 'how are my goals'})
        answer = response.get_json()['answer']
        self.assertNotIn('<img', answer)
        self.assertIn('&lt;img', answer)

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

    def test_auto_fetch_without_imap_credentials_reports_setup_and_adds_nothing(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {'IMAP_EMAIL': '', 'IMAP_PASSWORD': ''}):
            response = self.client.post('/auto-fetch-receipts', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'not set up', response.data)
        self.assertEqual(self._receipt_item_count(), 0)

    def test_auto_fetch_itemises_only_receipts_it_actually_found(self):
        # The fetcher searches only merchants that email receipts, itemises only
        # those whose attachment was found and read, and never invents items.
        from unittest import mock
        from services import ocr

        found = []

        def fake_fetch(mail, keyword, days_back=7):
            if found:
                return None  # only the first merchant has an email receipt
            handle = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            handle.close()
            found.append(keyword)
            return handle.name

        scanned = ocr.ReceiptScan(items=[
            {'name': 'Real Line', 'price': 12.5, 'quantity': 1}], total=12.5)
        with mock.patch.dict(os.environ, {'IMAP_EMAIL': 'me@example.com',
                                          'IMAP_PASSWORD': 'secret'}), \
                mock.patch('services.imap_fetcher.connect_imap',
                           return_value=mock.Mock()), \
                mock.patch('services.imap_fetcher.fetch_receipt_attachment',
                           side_effect=fake_fetch), \
                mock.patch('services.ocr.scan_receipt', return_value=scanned):
            response = self.client.post('/auto-fetch-receipts', follow_redirects=True)

        self.assertIn(b'Itemised 1 emailed receipt', response.data)
        import database
        conn = database.get_db_connection()
        rows = conn.execute(
            'SELECT t.description, r.item_name FROM Receipt_Item r '
            'JOIN Transaction_Record t ON r.transaction_id = t.transaction_id'
        ).fetchall()
        conn.close()
        self.assertEqual([r['item_name'] for r in rows], ['Real Line'])
        self.assertTrue(ocr.emails_receipt(rows[0]['description']))

    def _receipt_item_count(self):
        import database
        conn = database.get_db_connection()
        n = conn.execute('SELECT COUNT(*) AS n FROM Receipt_Item').fetchone()['n']
        conn.close()
        return n

    def _a_debit(self):
        import database
        conn = database.get_db_connection()
        row = conn.execute(
            "SELECT transaction_id, amount FROM Transaction_Record "
            "WHERE transaction_type = 'DEBIT' AND is_subscription = 0 "
            "ORDER BY transaction_id LIMIT 1").fetchone()
        conn.close()
        return row['transaction_id'], float(row['amount'])

    def _upload(self, txn_id):
        return self.client.post(f'/transactions/{txn_id}/receipt', data={
            'receipt': (io.BytesIO(b'not really an image'), 'receipt.png'),
        }, content_type='multipart/form-data', follow_redirects=True)

    def test_receipt_scan_failure_is_reported_and_adds_no_items(self):
        from unittest import mock
        from services import ocr
        txn_id, _ = self._a_debit()
        with mock.patch('services.ocr.scan_receipt',
                        side_effect=ocr.ReceiptScanError('Tesseract OCR is not installed.')):
            response = self._upload(txn_id)
        self.assertIn(b'Tesseract OCR is not installed.', response.data)
        self.assertIn(b'add the items manually', response.data)
        self.assertEqual(self._receipt_item_count(), 0)

    def test_receipt_with_no_readable_lines_adds_no_items(self):
        from unittest import mock
        from services import ocr
        txn_id, _ = self._a_debit()
        with mock.patch('services.ocr.scan_receipt', return_value=ocr.ReceiptScan()):
            response = self._upload(txn_id)
        self.assertIn(b'No priced items could be read', response.data)
        self.assertEqual(self._receipt_item_count(), 0)

    def test_receipt_scan_stores_items_and_flags_a_short_total(self):
        from unittest import mock
        from services import ocr
        txn_id, amount = self._a_debit()
        scan = ocr.ReceiptScan(items=[
            {'name': 'Bread', 'price': 1.0, 'quantity': 1}], total=amount)
        with mock.patch('services.ocr.scan_receipt', return_value=scan):
            response = self._upload(txn_id)
        self.assertIn(b'1 item(s) extracted', response.data)
        self.assertIn(b'Some lines may have been missed', response.data)
        self.assertEqual(self._receipt_item_count(), 1)

    def test_manual_item_can_be_added_and_deleted(self):
        txn_id, _ = self._a_debit()
        response = self.client.post(f'/transactions/{txn_id}/receipt/items', data={
            'item_name': 'Cooking Oil 2L', 'quantity': '2', 'amount': '170.00',
        }, follow_redirects=True)
        self.assertIn(b'Cooking Oil 2L', response.data)
        import database
        conn = database.get_db_connection()
        item = conn.execute('SELECT * FROM Receipt_Item').fetchone()
        conn.close()
        self.assertEqual((item['quantity'], item['amount']), (2, 170.0))

        self.client.post(
            f"/transactions/{txn_id}/receipt/items/{item['item_id']}/delete")
        self.assertEqual(self._receipt_item_count(), 0)

    def test_manual_item_rejects_invalid_input(self):
        txn_id, _ = self._a_debit()
        for data in ({'item_name': '', 'amount': '10'},
                     {'item_name': 'Milk', 'amount': 'abc'},
                     {'item_name': 'Milk', 'amount': '-5'},
                     {'item_name': 'Milk', 'amount': '5', 'quantity': '0'}):
            with self.subTest(data=data):
                self.client.post(f'/transactions/{txn_id}/receipt/items',
                                 data=data, follow_redirects=True)
                self.assertEqual(self._receipt_item_count(), 0)

    def test_receipt_item_routes_404_for_unknown_transaction_or_item(self):
        txn_id, _ = self._a_debit()
        self.assertEqual(self.client.post('/transactions/999999/receipt/items', data={
            'item_name': 'Milk', 'amount': '5'}).status_code, 404)
        self.assertEqual(self.client.post(
            f'/transactions/{txn_id}/receipt/items/999999/delete').status_code, 404)

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


class PWATests(RouteTestCase):
    """Progressive Web App manifest, Service Worker, icons and offline page."""

    def test_manifest_is_served_with_required_pwa_fields(self):
        response = self.client.get('/static/manifest.json')
        self.assertEqual(response.status_code, 200)
        manifest = response.get_json()
        self.assertEqual(manifest['short_name'], 'Expense Tracker')
        self.assertEqual(manifest['start_url'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['theme_color'], '#14532d')
        sizes = {icon['sizes'] for icon in manifest['icons']}
        self.assertTrue({'192x192', '512x512', 'any'}.issubset(sizes))
        response.close()

    def test_manifest_declares_app_shortcuts(self):
        response = self.client.get('/static/manifest.json')
        urls = {s['url'] for s in response.get_json().get('shortcuts', [])}
        response.close()
        self.assertTrue({'/', '/transactions', '/reports'}.issubset(urls))

    def test_service_worker_is_served_at_root_scope(self):
        response = self.client.get('/sw.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn('javascript', response.content_type)
        self.assertEqual(response.headers.get('Service-Worker-Allowed'), '/')
        self.assertIn('no-cache', response.headers.get('Cache-Control', ''))
        self.assertIn(b'expense-tracker-shell', response.data)
        response.close()

    def test_offline_fallback_page_renders_without_login(self):
        response = self.client.get('/offline')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'You are offline', response.data)

    def test_pwa_icons_and_head_links_exist(self):
        setup_page = self.client.get('/setup').data
        self.assertIn(b'rel="manifest"', setup_page)
        self.assertIn(b'name="theme-color" content="#14532d"', setup_page)
        self.assertIn(b'rel="apple-touch-icon"', setup_page)
        self.assertIn(b'/static/js/pwa.js', setup_page)

        for icon_path in ('/static/icons/icon-192.png',
                          '/static/icons/icon-512.png'):
            res = self.client.get(icon_path)
            self.assertEqual(res.status_code, 200, icon_path)
            self.assertTrue(res.data.startswith(b'\x89PNG\r\n\x1a\n'))
            res.close()

        svg_res = self.client.get('/static/icons/icon.svg')
        self.assertEqual(svg_res.status_code, 200)
        self.assertIn(b'<svg', svg_res.data)
        svg_res.close()


class UATAndPerformanceTests(RouteTestCase):
    """Automated NFR-01 response-time check and UAT session preparation test."""

    def test_nfr01_core_pages_render_within_three_seconds(self):
        import time
        import database
        from services import ingestion

        self.complete_setup()
        statement = build_statement(months=6)
        try:
            conn = database.get_db_connection()
            try:
                user = database.get_user(conn)
                ingestion.import_statement(conn, user['user_id'], statement)
                ingestion.post_import_pipeline(conn, user['user_id'])
            finally:
                conn.close()
        finally:
            if os.path.exists(statement):
                os.unlink(statement)

        for route in ('/', '/transactions', '/budget', '/savings',
                      '/reports', '/settings'):
            with self.subTest(route=route):
                start = time.perf_counter()
                response = self.client.get(route)
                elapsed = time.perf_counter() - start
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(
                    elapsed, 3.0,
                    f'{route} took {elapsed:.2f}s, exceeding the NFR-01 3.0s limit')

    def test_uat_session_launcher_prepares_isolated_files(self):
        import shutil
        import run_uat_session

        temp_root = tempfile.mkdtemp()
        try:
            info = run_uat_session.prepare_session(
                'P01', salary=12000.0, salary_day=25, months=4,
                reset=True, base_dir=temp_root)
            self.assertEqual(info['participant'], 'P01')
            self.assertTrue(os.path.exists(info['csv_path']))
            self.assertGreater(info['row_count'], 50)
            self.assertTrue(info['db_path'].endswith('uat_P01.db'))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
