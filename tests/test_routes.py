"""Route-level tests: authentication, rendering and the security controls.

Uses Flask's test client against a throwaway database so no test touches the
real expense_tracker.db.
"""
import io
import re
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

    def test_lockout_expires_after_the_lockout_period(self):
        # The lockout must be temporary: once LOGIN_LOCKOUT_SECONDS pass, the
        # correct password works again. Time is advanced by patching the
        # monotonic clock the throttle reads, rather than really waiting.
        from unittest import mock
        import security
        self.complete_setup()
        self.client.post('/logout')
        for _ in range(config.MAX_LOGIN_ATTEMPTS):
            self.client.post('/login', data={'password': 'wrong-guess'})
        locked = self.client.post('/login', data={'password': PASSWORD})
        self.assertEqual(locked.status_code, 429)

        later = security.time.monotonic() + config.LOGIN_LOCKOUT_SECONDS + 1
        with mock.patch.object(security.time, 'monotonic', return_value=later):
            response = self.client.post('/login', data={'password': PASSWORD},
                                        follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/login', response.headers['Location'])

    def _set_last_seen(self, minutes_ago):
        """Back-date the session's last activity, as if the user went idle."""
        from datetime import datetime, timedelta, timezone
        with self.client.session_transaction() as sess:
            sess['last_seen'] = (datetime.now(timezone.utc)
                                 - timedelta(minutes=minutes_ago)).isoformat()

    def test_idle_session_is_signed_out_after_the_timeout(self):
        # Security promise shown on the login and Settings pages: signed out
        # after SESSION_TIMEOUT_MINUTES of inactivity.
        self.complete_setup()
        self._set_last_seen(config.SESSION_TIMEOUT_MINUTES + 1)
        response = self.client.get('/budget', follow_redirects=True)
        self.assertIn(b'Signed out after', response.data)
        self.assertIn(b'Unlock', response.data)  # back on the sign-in page
        # The expired session is cleared, not just redirected once.
        again = self.client.get('/budget', follow_redirects=False)
        self.assertIn('/login', again.headers['Location'])

    def test_active_session_survives_and_refreshes_its_idle_timer(self):
        # Activity just inside the timeout keeps the user signed in, and each
        # request restarts the idle clock (last_seen moves to "now").
        from datetime import datetime, timezone
        self.complete_setup()
        self._set_last_seen(config.SESSION_TIMEOUT_MINUTES - 1)
        response = self.client.get('/budget', follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as sess:
            seen = datetime.fromisoformat(sess['last_seen'])
        self.assertLess((datetime.now(timezone.utc) - seen).total_seconds(), 60)

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

    def test_reset_password_succeeds_with_matching_profile_details(self):
        self.complete_setup()
        self.client.post('/logout')
        login_page = self.client.get('/login').data
        self.assertIn(b'Forgot your password?', login_page)

        new_password = 'new-strong-password-456'
        response = self.client.post('/reset-password', data={
            'full_name': '  test user ',
            'salary_amount': str(int(SALARY)),
            'salary_day': str(SALARY_DAY),
            'password': new_password,
            'password_confirm': new_password,
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Your password has been reset', response.data)

        # Old password must no longer unlock the app.
        old_attempt = self.client.post('/login', data={'password': PASSWORD},
                                       follow_redirects=True)
        self.assertIn(b'Incorrect password', old_attempt.data)

        # New password signs in cleanly.
        new_attempt = self.client.post('/login', data={'password': new_password},
                                       follow_redirects=True)
        self.assertIn(b'Hello', new_attempt.data)

    def test_reset_password_rejects_wrong_profile_details(self):
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/reset-password', data={
            'full_name': 'Test User',
            'salary_amount': '9999',
            'salary_day': str(SALARY_DAY),
            'password': 'new-strong-password-456',
            'password_confirm': 'new-strong-password-456',
        }, follow_redirects=True)
        self.assertIn(b'do not match the account on this device', response.data)
        # Original password still works.
        ok = self.client.post('/login', data={'password': PASSWORD},
                              follow_redirects=True)
        self.assertIn(b'Hello', ok.data)

    def test_change_password_from_settings_updates_password(self):
        self.complete_setup()
        # Wrong current password is rejected.
        bad = self.client.post('/settings/password', data={
            'current_password': 'wrong-current-password',
            'new_password': 'updated-password-789',
            'password_confirm': 'updated-password-789',
        }, follow_redirects=True)
        self.assertIn(b'Current password is incorrect', bad.data)

        # Correct current password updates the hash.
        good = self.client.post('/settings/password', data={
            'current_password': PASSWORD,
            'new_password': 'updated-password-789',
            'password_confirm': 'updated-password-789',
        }, follow_redirects=True)
        self.assertIn(b'Your password has been updated', good.data)

        self.client.post('/logout')
        signed_in = self.client.post('/login',
                                     data={'password': 'updated-password-789'},
                                     follow_redirects=True)
        self.assertIn(b'Hello', signed_in.data)

    def test_reset_with_a_non_ascii_name_is_a_mismatch_not_a_crash(self):
        # secrets.compare_digest raises TypeError on non-ASCII str.
        self.complete_setup()
        self.client.post('/logout')
        response = self.client.post('/reset-password', data={
            'full_name': 'Tést Üser', 'salary_amount': str(int(SALARY)),
            'salary_day': str(SALARY_DAY), 'password': 'new-strong-password-456',
            'password_confirm': 'new-strong-password-456'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'do not match the account on this device', response.data)

    def test_changing_the_password_signs_out_other_devices_but_not_this_one(self):
        self.complete_setup()
        phone = self.client.application.test_client()
        phone.post('/login', data={'password': PASSWORD})
        self.assertEqual(phone.get('/settings').status_code, 200)

        self.client.post('/settings/password', data={
            'current_password': PASSWORD, 'new_password': 'updated-password-789',
            'password_confirm': 'updated-password-789'})

        self.assertEqual(self.client.get('/settings').status_code, 200)
        signed_out = phone.get('/settings', follow_redirects=True)
        self.assertIn(b'please sign in again', signed_out.data)
        self.assertIn(b'Welcome back', signed_out.data)

    def test_reset_signs_out_every_existing_session(self):
        self.complete_setup()
        phone = self.client.application.test_client()
        phone.post('/login', data={'password': PASSWORD})
        self.client.post('/logout')
        self.client.post('/reset-password', data={
            'full_name': 'Test User', 'salary_amount': str(int(SALARY)),
            'salary_day': str(SALARY_DAY), 'password': 'new-strong-password-456',
            'password_confirm': 'new-strong-password-456'})
        self.assertEqual(phone.get('/settings').status_code, 302)

    def test_wrong_current_password_counts_towards_the_lockout(self):
        self.complete_setup()
        for _ in range(config.MAX_LOGIN_ATTEMPTS):
            self.client.post('/settings/password', data={
                'current_password': 'not-it', 'new_password': 'updated-password-789',
                'password_confirm': 'updated-password-789'})
        locked = self.client.post('/settings/password', data={
            'current_password': PASSWORD, 'new_password': 'updated-password-789',
            'password_confirm': 'updated-password-789'}, follow_redirects=True)
        self.assertIn(b'Too many incorrect attempts', locked.data)
        self.client.post('/logout')
        still_old = self.client.post('/login', data={'password': PASSWORD})
        self.assertEqual(still_old.status_code, 429)  # locked, and unchanged

    def test_new_password_must_differ_from_the_current_one(self):
        self.complete_setup()
        response = self.client.post('/settings/password', data={
            'current_password': PASSWORD, 'new_password': PASSWORD,
            'password_confirm': PASSWORD}, follow_redirects=True)
        self.assertIn(b'same as the current one', response.data)

    def test_password_forms_include_show_password_checkbox(self):
        # Fresh install: /setup has the checkbox with an inline toggle fallback.
        setup_html = self.client.get('/setup').data
        self.assertIn(b'data-toggle-passwords', setup_html)
        self.assertIn(b'onchange=', setup_html)
        self.assertIn(b'Show password', setup_html)

        self.complete_setup()
        # Signed-in: /settings Change password card has the checkbox.
        settings_html = self.client.get('/settings').data
        self.assertIn(b'data-toggle-passwords', settings_html)

        # Signed-out: /login and /reset-password both have the checkbox.
        self.client.post('/logout')
        for path in ('/login', '/reset-password'):
            with self.subTest(path=path):
                html = self.client.get(path).data
                self.assertIn(b'data-toggle-passwords', html)
                self.assertIn(b'Show password', html)


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


class EmailReceiptTransactionTests(RouteTestCase):
    """Receipts with no matching transaction become transactions, and a later
    statement upload merges with them instead of duplicating them."""

    def setUp(self):
        super().setUp()
        self.complete_setup()
        from datetime import date, timedelta
        self.receipt_day = date.today() - timedelta(days=5)

    def _scan(self, receipts):
        from unittest import mock
        from services import email_receipts
        mailbox = mock.Mock()
        mailbox.search.return_value = ('OK', [b''])  # pass 1 finds nothing
        with mock.patch.dict(os.environ, {'IMAP_EMAIL': 'me@example.com',
                                          'IMAP_PASSWORD': 'secret'}), \
                mock.patch('services.imap_fetcher.connect_imap',
                           return_value=mailbox), \
                mock.patch('services.email_receipts.read_new_receipts',
                           return_value=email_receipts.SweepResult(receipts=receipts)):
            return self.client.post('/auto-fetch-receipts', follow_redirects=True)

    def _uber_receipt(self, key='<trip-1@uber.com>'):
        from services import email_receipts, ocr
        return email_receipts.NewReceipt(
            key, self.receipt_day, 'Uber', 50.0,
            ocr.ReceiptScan(items=[{'name': 'Trip fare', 'price': 45.0, 'quantity': 1},
                                   {'name': 'Booking fee', 'price': 5.0, 'quantity': 1}],
                            total=50.0))

    def _rows(self):
        import database
        conn = database.get_db_connection()
        rows = conn.execute(
            'SELECT t.transaction_id, t.transaction_date, t.description, t.amount, '
            '  c.category_name, (SELECT COUNT(*) FROM Receipt_Item r '
            '                    WHERE r.transaction_id = t.transaction_id) AS items '
            'FROM Transaction_Record t LEFT JOIN Category c '
            '  ON t.category_id = c.category_id').fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _upload_csv(self, text):
        return self.client.post('/transactions', data={
            'file': (io.BytesIO(text.encode()), 'statement.csv'),
        }, content_type='multipart/form-data', follow_redirects=True)

    def test_scan_adds_a_purchase_with_no_transactions_yet(self):
        response = self._scan([self._uber_receipt()])
        self.assertIn(b'Added 1 purchase(s) from your email receipts', response.data)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['description'], 'Uber')
        self.assertEqual(rows[0]['amount'], 50.0)
        self.assertEqual(rows[0]['items'], 2)
        self.assertEqual(rows[0]['transaction_date'], self.receipt_day.isoformat())
        self.assertIsNotNone(rows[0]['category_name'])

    def test_rescanning_the_same_email_adds_nothing(self):
        self._scan([self._uber_receipt()])
        response = self._scan([self._uber_receipt()])
        self.assertEqual(len(self._rows()), 1)
        self.assertIn(b'No new receipt emails', response.data)

    def test_receipt_matching_a_bank_transaction_only_itemises_it(self):
        from datetime import timedelta
        bank_day = self.receipt_day + timedelta(days=1)
        self._upload_csv(f'Date,Description,Amount\n{bank_day},UBER *TRIP,-50.00\n')
        self._scan([self._uber_receipt()])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['description'], 'UBER *TRIP')
        self.assertEqual(rows[0]['items'], 2)

    def test_statement_upload_merges_with_an_email_added_purchase(self):
        from datetime import timedelta
        self._scan([self._uber_receipt()])
        bank_day = self.receipt_day + timedelta(days=2)
        statement = f'Date,Description,Amount\n{bank_day},UBER *TRIP HELP.UBER.COM,-50.00\n'

        response = self._upload_csv(statement)
        self.assertIn(b'1 matched purchase(s) already added from your email', response.data)
        rows = self._rows()
        self.assertEqual(len(rows), 1)  # merged, not duplicated
        self.assertEqual(rows[0]['description'], 'UBER *TRIP HELP.UBER.COM')
        self.assertEqual(rows[0]['transaction_date'], bank_day.isoformat())
        self.assertEqual(rows[0]['items'], 2)  # the receipt items survive

        again = self._upload_csv(statement)  # re-uploading stays a duplicate
        self.assertIn(b'1 duplicate(s) skipped', again.data)
        self.assertEqual(len(self._rows()), 1)

    def test_different_amount_is_not_merged(self):
        from datetime import timedelta
        self._scan([self._uber_receipt()])
        bank_day = self.receipt_day + timedelta(days=1)
        self._upload_csv(f'Date,Description,Amount\n{bank_day},UBER *TRIP,-55.00\n')
        self.assertEqual(len(self._rows()), 2)

    def _dollar_receipt(self):
        from services import email_receipts, ocr
        # $50 converted at K25 per dollar: an estimate of K1,250.
        return email_receipts.NewReceipt(
            '<order-9@amazon.com>', self.receipt_day, 'Amazon', 1250.0,
            ocr.ReceiptScan(items=[{'name': 'Book', 'price': 1250.0, 'quantity': 1}],
                            total=1250.0),
            currency='USD', original=50.0, rate=25.0)

    def test_converted_purchase_merges_with_the_bank_amount_within_tolerance(self):
        from datetime import timedelta
        self._scan([self._dollar_receipt()])
        self.assertEqual(self._rows()[0]['description'], 'Amazon (USD 50.00 at 25.00)')

        bank_day = self.receipt_day + timedelta(days=2)
        # The bank's own rate plus a fee: K1,290, 3.2% above the estimate.
        response = self._upload_csv(
            f'Date,Description,Amount\n{bank_day},AMAZON MKTPLACE US,-1290.00\n')
        self.assertIn(b'1 matched purchase(s) already added', response.data)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['description'], rows[0]['amount']),
                         ('AMAZON MKTPLACE US', 1290.0))  # the bank's figure wins
        self.assertEqual(rows[0]['items'], 1)

    def test_converted_purchase_far_from_the_bank_amount_is_not_merged(self):
        from datetime import timedelta
        self._scan([self._dollar_receipt()])
        bank_day = self.receipt_day + timedelta(days=1)
        self._upload_csv(f'Date,Description,Amount\n{bank_day},AMAZON,-1400.00\n')
        self.assertEqual(len(self._rows()), 2)  # 12% apart: a different purchase

    def test_kwacha_purchase_still_needs_an_exact_amount(self):
        from datetime import timedelta
        self._scan([self._uber_receipt()])
        bank_day = self.receipt_day + timedelta(days=1)
        self._upload_csv(f'Date,Description,Amount\n{bank_day},UBER *TRIP,-51.00\n')
        self.assertEqual(len(self._rows()), 2)  # 2% apart, but not converted


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
        self.assertIn(b'Insights updated', response.data)
        self.assertIn(b'Spending predictions checked', response.data)
        # Plain language only: the confirmation messages carry no ML jargon
        # (the technical figures stay on the model cards, outside the flashes).
        import re
        flashes = re.search(rb'<ul class="flashes">(.*?)</ul>', response.data, re.S)
        self.assertIsNotNone(flashes)
        for jargon in (b'F1', b'hold-out', b'MAE', b'Retrained'):
            self.assertNotIn(jargon, flashes.group(1))

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

    def _connect_bank(self, bank='zanaco'):
        return self.client.post('/sync/bank/connect', data={
            'bank': bank, 'consent': '1'}, follow_redirects=True)

    def _connect_momo(self, wallet='mtn', number='0961234567'):
        return self.client.post('/sync/momo/connect', data={
            'wallet': wallet, 'number': number, 'consent': '1'}, follow_redirects=True)

    def test_bank_sync_downloads_new_transactions(self):
        before = self._txn_count()
        response = self._connect_bank()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Connected to Zanaco', response.data)
        self.assertGreater(self._txn_count(), before)

    def test_mobile_money_sync_imports_transactions(self):
        before = self._txn_count()
        response = self._connect_momo()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Connected to MTN MoMo', response.data)
        self.assertGreater(self._txn_count(), before)

    def test_bank_connect_shows_a_read_only_consent_screen_first(self):
        page = self.client.get('/sync/bank/connect?bank=fnb')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'FNB Zambia', page.data)
        self.assertIn(b'Allow read-only access', page.data)
        self.assertIn(b'never be able to', page.data)
        # Without ticking consent, nothing connects or imports.
        before = self._txn_count()
        self.client.post('/sync/bank/connect', data={'bank': 'fnb'})
        self.assertEqual(self._txn_count(), before)
        self.assertNotIn(b'Connected: FNB', self.client.get('/transactions').data)

    def test_unknown_bank_is_rejected(self):
        response = self.client.get('/sync/bank/connect?bank=evilbank',
                                   follow_redirects=True)
        self.assertIn(b'Choose your bank', response.data)

    def test_connected_bank_shows_a_badge_with_a_masked_account(self):
        import re
        self._connect_bank('absa')
        page = self.client.get('/transactions').data.decode()
        self.assertRegex(page, r'Connected: Absa \(•••• \d{4}\)')
        self.assertIn('ABSA POS', page)  # rows worded the way that bank shows them
        self.assertIn('Disconnect', page)

    def test_sync_now_requires_a_connection(self):
        response = self.client.post('/sync-bank', follow_redirects=True)
        self.assertIn(b'Connect your bank account first', response.data)

    def test_wallet_number_must_match_the_network(self):
        before = self._txn_count()
        cases = {'0971234567': b'is not a MTN MoMo number', '12345': b'Enter a Zambian',
                 '': b'Enter a Zambian'}
        for number, message in cases.items():
            with self.subTest(number=number):
                response = self._connect_momo('mtn', number)
                self.assertIn(message, response.data)
        self.assertEqual(self._txn_count(), before)
        ok = self._connect_momo('airtel', '+260 977 123 456')
        self.assertIn(b'Connected to Airtel Money', ok.data)
        self.assertIn('Connected: Airtel Money (097 ••• 3456)',
                      self.client.get('/transactions').data.decode())

    def test_disconnect_can_remove_only_the_imported_transactions(self):
        before = self._txn_count()
        self._connect_bank()
        self._connect_momo()
        self.assertGreater(self._txn_count(), before)
        response = self.client.post('/sync/bank/disconnect', data={
            'remove_transactions': '1'}, follow_redirects=True)
        self.assertIn(b'Disconnected Zanaco', response.data)
        page = response.data.decode()
        self.assertNotIn('Connected: Zanaco', page)
        self.assertIn('Connected: MTN MoMo', page)  # the other link is untouched
        self.client.post('/sync/momo/disconnect', data={
            'provider': 'mtn', 'remove_transactions': '1'})
        self.assertEqual(self._txn_count(), before)  # back to the pre-demo state

    def test_disconnect_without_removal_keeps_the_transactions(self):
        self._connect_bank()
        count = self._txn_count()
        self.client.post('/sync/bank/disconnect')
        self.assertEqual(self._txn_count(), count)
        # Reconnecting works straight away (the daily sync marker was reset).
        self.assertIn(b'Connected to Zanaco', self._connect_bank().data)
        self.assertGreater(self._txn_count(), count)

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
        from services import email_receipts, ocr

        found = []

        def fake_find(mail, keyword, on_date, amount, scan_file, **kwargs):
            if found:  # only the first merchant has an email receipt
                return email_receipts.EmailReceiptResult(email_receipts.NOT_FOUND)
            found.append(keyword)
            return email_receipts.EmailReceiptResult(
                email_receipts.MATCHED,
                ocr.ReceiptScan(items=[{'name': 'Real Line', 'price': 12.5,
                                        'quantity': 1}], total=12.5))

        with mock.patch.dict(os.environ, {'IMAP_EMAIL': 'me@example.com',
                                          'IMAP_PASSWORD': 'secret'}), \
                mock.patch('services.imap_fetcher.connect_imap',
                           return_value=mock.Mock()), \
                mock.patch('services.email_receipts.find_receipt_for',
                           side_effect=fake_find), \
                mock.patch('services.email_receipts.read_new_receipts',
                           return_value=email_receipts.SweepResult()):
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

    def test_auto_fetch_uses_the_form_credentials_and_never_stores_the_password(self):
        from unittest import mock
        import database
        connect = mock.Mock(side_effect=OSError('offline'))
        with mock.patch.dict(os.environ, {'IMAP_EMAIL': '', 'IMAP_PASSWORD': ''}), \
                mock.patch('services.imap_fetcher.connect_imap', connect):
            response = self.client.post('/auto-fetch-receipts', data={
                'imap_email': 'me@example.com', 'imap_password': 'app-pass-123'},
                follow_redirects=True)
        connect.assert_called_once_with('me@example.com', 'app-pass-123')
        self.assertIn(b'Could not reach your mail server', response.data)
        conn = database.get_db_connection()
        stored = [r['value'] for r in conn.execute('SELECT value FROM AppSetting')]
        conn.close()
        self.assertIn('me@example.com', stored)
        self.assertFalse(any('app-pass-123' in v for v in stored))
        # The remembered address pre-fills the form.
        self.assertIn(b'value="me@example.com"', self.client.get('/settings').data)

    def test_auto_fetch_shows_the_providers_reason_for_a_refused_sign_in(self):
        import imaplib
        from unittest import mock
        refused = imaplib.IMAP4.error(
            b'[ALERT] Application-specific password required')
        with mock.patch.dict(os.environ, {'IMAP_EMAIL': '', 'IMAP_PASSWORD': ''}), \
                mock.patch('services.imap_fetcher.connect_imap', side_effect=refused):
            response = self.client.post('/auto-fetch-receipts', data={
                'imap_email': 'me@gmail.com', 'imap_password': 'normal-password'},
                follow_redirects=True)
        self.assertIn(b'Application-specific password required', response.data)
        self.assertNotIn(b'normal-password', response.data)

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
        first = self.client.post('/sync/bank/connect', data={
            'bank': 'zanaco', 'consent': '1'}, follow_redirects=True)
        self.assertIn(b'Connected to Zanaco', first.data)
        count_after_first = self._txn_count()
        second = self.client.post('/sync-bank', follow_redirects=True)
        self.assertIn(b'already up to date', second.data)
        self.assertEqual(self._txn_count(), count_after_first)

    def test_switching_bank_provider_clears_stale_last_sync(self):
        self.client.post('/sync/bank/connect', data={
            'bank': 'zanaco', 'consent': '1'}, follow_redirects=True)
        count_after_zanaco = self._txn_count()
        switched = self.client.post('/sync/bank/connect', data={
            'bank': 'fnb', 'consent': '1'}, follow_redirects=True)
        self.assertIn(b'Connected to FNB Zambia', switched.data)
        self.assertGreater(self._txn_count(), count_after_zanaco)

    def test_both_mtn_and_airtel_wallets_can_be_connected_simultaneously(self):
        base_count = self._txn_count()
        # Connect MTN MoMo first
        mtn = self.client.post('/sync/momo/connect', data={
            'wallet': 'mtn', 'number': '0961 234 567', 'consent': '1',
        }, follow_redirects=True)
        self.assertIn(b'Connected to MTN MoMo', mtn.data)
        self.assertIn(b'Connect another wallet (Airtel Money)', mtn.data)
        count_mtn = self._txn_count()
        self.assertGreater(count_mtn, base_count)

        # Connect Airtel Money while MTN MoMo stays connected
        airtel = self.client.post('/sync/momo/connect', data={
            'wallet': 'airtel', 'number': '0971 234 567', 'consent': '1',
        }, follow_redirects=True)
        self.assertIn(b'Connected to Airtel Money', airtel.data)
        self.assertIn(b'Connected: MTN MoMo', airtel.data)
        self.assertIn(b'Connected: Airtel Money', airtel.data)
        self.assertIn(b'Sync all wallets (2)', airtel.data)
        count_both = self._txn_count()
        self.assertGreater(count_both, count_mtn)

        # Disconnecting MTN MoMo with removal removes ONLY MTN rows, keeping
        # base statement rows and Airtel Money rows intact.
        disc = self.client.post('/sync/momo/disconnect', data={
            'provider': 'mtn', 'remove_transactions': '1',
        }, follow_redirects=True)
        self.assertIn(b'Disconnected MTN MoMo', disc.data)
        self.assertIn(b'Connected: Airtel Money', disc.data)
        self.assertNotIn(b'Connected: MTN MoMo', disc.data)
        airtel_added = count_both - count_mtn
        self.assertEqual(self._txn_count(), base_count + airtel_added)

    def test_connecting_an_already_connected_wallet_imports_nothing_more(self):
        self._connect_momo('mtn', '0961234567')
        count = self._txn_count()
        again = self._connect_momo('mtn', '0769999999')
        self.assertIn(b'MTN MoMo is already connected (096', again.data)
        self.assertEqual(self._txn_count(), count)

    def test_wallet_disconnect_must_name_the_wallet(self):
        self._connect_momo('mtn')
        response = self.client.post('/sync/momo/disconnect', follow_redirects=True)
        self.assertIn(b'Choose which wallet to disconnect', response.data)
        self.assertIn(b'Connected: MTN MoMo', response.data)

    def test_single_wallet_record_from_before_multi_wallet_is_migrated(self):
        # The earlier format kept one wallet under 'sync_momo_link' and tagged
        # its rows 'sync-momo-<hex>'. It must show as connected, and a
        # per-wallet disconnect must still remove exactly its rows.
        import json
        import database
        conn = database.get_db_connection()
        user = database.get_user(conn)
        database.set_setting(conn, 'sync_momo_link', json.dumps({
            'provider': 'airtel', 'name': 'Airtel Money',
            'number_mask': '097 ••• 1234', 'connected_at': '2026-11-20T10:00'}))
        database.set_setting(conn, 'last_momo_sync', '2026-11-20')
        for i in range(3):
            conn.execute(
                "INSERT INTO Transaction_Record (transaction_date, description, amount, "
                "transaction_type, import_hash, user_id) VALUES "
                "('2026-11-20', 'Airtel Money Send Money', 50, 'DEBIT', ?, ?)",
                (f'sync-momo-legacy{i}', user['user_id']))
        conn.commit()
        conn.close()
        before = self._txn_count()

        page = self.client.get('/transactions').data.decode()
        self.assertIn('Connected: Airtel Money (097 ••• 1234)', page)
        self.client.post('/sync/momo/disconnect', data={
            'provider': 'airtel', 'remove_transactions': '1'})
        self.assertEqual(self._txn_count(), before - 3)
        conn = database.get_db_connection()
        self.assertIsNone(database.get_setting(conn, 'sync_momo_link'))
        conn.close()

    def test_expired_bank_consent_prompts_reauthorisation(self):
        import json
        import database
        self.client.post('/sync/bank/connect', data={
            'bank': 'absa', 'consent': '1'}, follow_redirects=True)
        conn = database.get_db_connection()
        rec = json.loads(database.get_setting(conn, 'sync_bank_link'))
        rec['consent_expires'] = '2020-01-01'
        database.set_setting(conn, 'sync_bank_link', json.dumps(rec))
        conn.commit()
        conn.close()

        res = self.client.post('/sync-bank', follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn('/sync/bank/connect?bank=absa', res.headers['Location'])

    def test_sync_triggers_retrain_and_forecast_reevaluation(self):
        # The import pipeline never backtests, so a FORECASTING metric appearing
        # after a sync proves the sync now retrains + re-evaluates like an upload.
        import database
        conn = database.get_db_connection()
        self.assertIsNone(database.latest_metric(conn, 'FORECASTING'))
        conn.close()
        self.client.post('/sync/bank/connect', data={
            'bank': 'stanbic', 'consent': '1'}, follow_redirects=True)
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
        self.assertEqual(response.data.count(b'<h2>Email receipts</h2>'), 1)

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
        self.assertIn(b'<h2>Financial assistant</h2>', dashboard)
        self.assertIn(b'Household', dashboard)  # nav link
        self.assertEqual(self.client.get('/household').status_code, 200)
        self.assertIn(b'Sync with Bank', self.client.get('/transactions').data)

    def test_disabled_hides_ui_and_404s_the_routes(self):
        import config
        original = config.DEMO_FEATURES
        config.DEMO_FEATURES = False
        try:
            dashboard = self.client.get('/').data
            self.assertNotIn(b'<h2>Financial assistant</h2>', dashboard)
            self.assertNotIn(b'Household', dashboard)
            self.assertNotIn(b'Sync with Bank',
                             self.client.get('/transactions').data)
            self.assertNotIn(b'Email Receipt Auto-Fetcher',
                             self.client.get('/settings').data)
            for path in ('/sync-bank', '/sync-momo', '/sync/bank/connect',
                         '/sync/momo/connect', '/sync/bank/disconnect', '/ask-assistant',
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

    def test_settings_shows_offline_mode_indicator(self):
        # The on-device diagnostic for offline mode (added after the iOS offline
        # fallback failed in a real-device session, defect D15).
        self.complete_setup()
        response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        for marker in (b'Offline mode', b'id="offline-status"',
                       b'id="offline-status-detail"', b'id="offline-status-mode"'):
            self.assertIn(marker, response.data)
        script = self.client.get('/static/js/pwa.js')
        # pwa.js is what fills the indicator in; it must target the same ids.
        self.assertIn(b"getElementById('offline-status')", script.data)
        script.close()

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

    def test_theme_toggle_and_system_default_controls_render(self):
        # Every page loads the zero-flash theme script in <head>, with no
        # separate theme button in the top navigation bar.
        setup_html = self.client.get('/setup').data
        self.assertIn(b'expense-tracker-theme', setup_html)
        self.assertNotIn(b'id="theme-toggle-btn"', setup_html)

        # The Settings Appearance card exposes System default, Light, and Dark.
        self.complete_setup()
        dashboard_html = self.client.get('/').data
        self.assertNotIn(b'id="theme-toggle-btn"', dashboard_html)

        settings_html = self.client.get('/settings').data
        self.assertIn(b'<h2>Appearance</h2>', settings_html)
        for mode in (b'data-theme-set="system"',
                     b'data-theme-set="light"',
                     b'data-theme-set="dark"'):
            self.assertIn(mode, settings_html)

        css_res = self.client.get('/static/css/style.css')
        self.assertIn(b':root[data-theme="dark"]', css_res.data)
        self.assertIn(b'prefers-color-scheme: dark', css_res.data)
        css_res.close()


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

    def test_update_profile_changes_salary_and_pay_day(self):
        self.complete_setup()
        response = self.client.post('/settings/profile', data={
            'full_name': 'Promoted User',
            'salary_amount': '18500.00',
            'salary_day': '28',
            'current_password': PASSWORD,
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Income updated to 18,500.00', response.data)

        dashboard = self.client.get('/').data
        self.assertIn(b'Hello, Promoted', dashboard)
        self.assertIn(b'18,500.00', dashboard)
        self.assertIn(b'paid on day 28', dashboard)

    def test_update_profile_rejects_invalid_values(self):
        self.complete_setup()
        bad = self.client.post('/settings/profile', data={
            'full_name': '',
            'salary_amount': '-500',
            'salary_day': '40',
        }, follow_redirects=True)
        self.assertIn(b'Enter your name', bad.data)
        self.assertIn(b'Salary amount must be greater than zero', bad.data)
        self.assertIn(b'Pay day must be between 1 and 31', bad.data)

    def _profile(self):
        import database
        conn = database.get_db_connection()
        user = database.get_user(conn)
        conn.close()
        return user['full_name'], user['salary_amount'], user['salary_day']

    def test_update_profile_needs_the_current_password(self):
        # The profile is the password-reset key: changing it without the
        # password would let anyone at an unlocked device take over the app.
        self.complete_setup()
        before = self._profile()
        for password in ('', 'wrong-password'):
            with self.subTest(password=password):
                response = self.client.post('/settings/profile', data={
                    'full_name': 'Intruder', 'salary_amount': '1.00',
                    'salary_day': '1', 'current_password': password,
                }, follow_redirects=True)
                self.assertIn(b'Enter your current password', response.data)
                self.assertEqual(self._profile(), before)

    def test_update_profile_with_no_changes_is_a_no_op(self):
        self.complete_setup()
        response = self.client.post('/settings/profile', data={
            'full_name': 'Test User', 'salary_amount': str(SALARY),
            'salary_day': str(SALARY_DAY),
        }, follow_redirects=True)
        self.assertIn(b'Nothing to update', response.data)

    def test_update_profile_rejects_an_implausibly_large_salary(self):
        self.complete_setup()
        response = self.client.post('/settings/profile', data={
            'full_name': 'Test User', 'salary_amount': '1e15',
            'salary_day': str(SALARY_DAY), 'current_password': PASSWORD,
        }, follow_redirects=True)
        self.assertIn(b'Salary amount must be at most', response.data)
        self.assertNotIn(b'must be greater than zero', response.data)

    def test_reset_uses_the_updated_profile_details(self):
        self.complete_setup()
        self.client.post('/settings/profile', data={
            'full_name': 'Test User', 'salary_amount': '18500',
            'salary_day': '28', 'current_password': PASSWORD})
        self.client.post('/logout')
        old = self.client.post('/reset-password', data={
            'full_name': 'Test User', 'salary_amount': str(int(SALARY)),
            'salary_day': str(SALARY_DAY), 'password': 'new-strong-password-456',
            'password_confirm': 'new-strong-password-456'}, follow_redirects=True)
        self.assertIn(b'do not match', old.data)
        new = self.client.post('/reset-password', data={
            'full_name': 'Test User', 'salary_amount': '18500', 'salary_day': '28',
            'password': 'new-strong-password-456',
            'password_confirm': 'new-strong-password-456'}, follow_redirects=True)
        self.assertIn(b'Your password has been reset', new.data)


class AssistantRouteTests(RouteTestCase):
    """The on-device assistant answered over HTTP, with session follow-ups."""

    def setUp(self):
        super().setUp()
        if not config.DEMO_FEATURES:
            self.skipTest('assistant is a demo feature')
        self.complete_setup()

    def _add(self, description, amount, category='', when=None):
        from datetime import date
        return self.client.post('/transactions/add', data={
            'description': description, 'amount': amount,
            'transaction_date': (when or date.today()).isoformat(),
            'transaction_type': 'DEBIT', 'category_name': category,
        }, follow_redirects=True)

    def ask(self, question):
        response = self.client.post('/ask-assistant', json={'question': question})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_category_spend_this_month(self):
        self._add('Yango ride town', '45.00', category='Transport')
        self._add('Bus fare', '15.00', category='Transport')
        self._add('Shoprite bread', '30.00', category='Groceries')
        answer = self.ask('how much did I spend on transport?')['answer']
        self.assertIn('K60.00', answer)
        self.assertIn('2 transactions', answer)

    def test_follow_up_reuses_the_subject_from_the_session(self):
        from datetime import date, timedelta
        last_month = date.today().replace(day=1) - timedelta(days=1)
        self._add('Yango ride town', '45.00', category='Transport')
        self._add('Yango ride home', '80.00', category='Transport', when=last_month)
        self.assertIn('K45.00', self.ask('how much on transport this month?')['answer'])
        follow = self.ask('and last month?')['answer']
        self.assertIn('K80.00', follow)
        self.assertIn('Transport', follow)

    def test_merchant_spend(self):
        self._add('Shoprite Cairo Rd', '120.00', category='Groceries')
        self._add('Pick n Pay Arcades', '70.00', category='Groceries')
        answer = self.ask('how much at shoprite?')['answer']
        self.assertIn('K120.00', answer)
        self.assertNotIn('190.00', answer)

    def test_afford_uses_the_amount(self):
        answer = self.ask('can I afford K999999?')['answer']
        self.assertIn('Not this month', answer)
        self.assertIn('K999,999.00', answer)

    def test_payday_and_suggestions(self):
        reply = self.ask('when is payday?')
        self.assertIn('next payday', reply['answer'])
        self.assertTrue(reply['suggestions'])

    def test_transaction_descriptions_are_escaped(self):
        # Descriptions come from bank files and appear in "recent" and "top
        # places" answers, so they must be escaped like category names.
        self._add('<img src=x onerror=alert(1)>', '10.00', category='Shopping')
        for question in ('show my recent transactions', 'top shops this month'):
            with self.subTest(question=question):
                answer = self.ask(question)['answer']
                self.assertNotIn('<img', answer)
                self.assertIn('&lt;img', answer)

    def test_long_or_odd_input_never_errors(self):
        for question in ('', '   ', '?' * 1000, 'k k k 1e999', '<script>', 'may may may'):
            with self.subTest(question=question[:20]):
                self.assertTrue(self.ask(question)['answer'])


class CashWithdrawalTests(RouteTestCase):
    """Option 1: an ATM or agent withdrawal moves money into the wallet; the
    spending is the cash purchases logged afterwards, so withdrawals never
    count as spending (no double counting)."""

    def setUp(self):
        super().setUp()
        self.complete_setup()

    def _add(self, description, amount, category=''):
        from datetime import date
        return self.client.post('/transactions/add', data={
            'description': description, 'amount': amount,
            'transaction_date': date.today().isoformat(),
            'transaction_type': 'DEBIT', 'category_name': category,
        }, follow_redirects=True)

    def test_withdrawals_are_recognised_by_rule(self):
        from ml import categorization
        for text in ('ATM WITHDRAWAL MANDA HILL', 'Cash Out at Airtel Money Agent',
                     'MTN MoMo Agent cash out', 'Cash withdrawal - Stanbic'):
            with self.subTest(text=text):
                self.assertEqual(categorization.classify(text)[0], 'Cash Withdrawal')
        # Words merely containing "atm" are not withdrawals.
        self.assertNotEqual(categorization.classify('Beauty spa treatment')[0],
                            'Cash Withdrawal')

    def test_withdrawal_plus_cash_purchase_is_counted_once(self):
        import database
        from datetime import date
        from services import recommendation
        self._add('ATM Withdrawal Cairo Road', '500.00')
        self._add('Market vegetables', '200.00', category='Groceries')
        conn = database.get_db_connection()
        user = database.get_user(conn)
        month = date.today().strftime('%Y-%m')
        spent = recommendation.actual_spend(conn, user['user_id'], month)
        conn.close()
        self.assertNotIn('Cash Withdrawal', spent)
        self.assertAlmostEqual(sum(spent.values()), 200.0)

    def test_assistant_total_excludes_withdrawals(self):
        self._add('ATM Withdrawal Cairo Road', '500.00')
        self._add('Market vegetables', '200.00', category='Groceries')
        page = self.client.get('/').data
        token = re.search(rb"'X-CSRFToken': '([^']+)'", page)
        headers = {'X-CSRFToken': token.group(1).decode()} if token else {}
        answer = self.client.post('/ask-assistant', json={
            'question': 'how much have I spent in total?'}, headers=headers).get_json()['answer']
        self.assertIn('200.00', answer)
        self.assertNotIn('700.00', answer)

    def test_existing_withdrawals_labelled_transfers_are_migrated(self):
        import database
        conn = database.get_db_connection()
        user = database.get_user(conn)
        transfers = database.category_id_for(conn, 'Transfers')
        rows = [('ATM WDL 0045 CAIRO RD', 'rule'), ('ATM Cash Withdrawal', 'model'),
                ('ATM Withdrawal kept by me', 'user'), ('Sent to Mobile Wallet', 'rule')]
        for text, source in rows:
            conn.execute(
                "INSERT INTO Transaction_Record (transaction_date, description, amount, "
                "transaction_type, category_id, category_source, import_hash, user_id) "
                "VALUES ('2026-10-01', ?, 100, 'DEBIT', ?, ?, ?, ?)",
                (text, transfers, source, 'legacy-' + text, user['user_id']))
        conn.commit()
        conn.close()
        database.init_db()   # runs the migration, as an app start would
        conn = database.get_db_connection()
        labels = {r['description']: r['category_name'] for r in conn.execute(
            'SELECT t.description, c.category_name FROM Transaction_Record t '
            'JOIN Category c ON t.category_id = c.category_id')}
        conn.close()
        self.assertEqual(labels['ATM WDL 0045 CAIRO RD'], 'Cash Withdrawal')
        self.assertEqual(labels['ATM Cash Withdrawal'], 'Cash Withdrawal')
        self.assertEqual(labels['ATM Withdrawal kept by me'], 'Transfers')  # user's choice
        self.assertEqual(labels['Sent to Mobile Wallet'], 'Transfers')


class FiveImprovementsTests(RouteTestCase):
    """Tests for manual cash transaction entry, merchant search, editing savings
    goals, per-category forecast MAE badge, and database restore."""

    def setUp(self):
        super().setUp()
        self.complete_setup()

    def test_add_single_transaction_auto_categorises_and_stores(self):
        from datetime import date
        today = date.today().isoformat()
        response = self.client.post('/transactions/add', data={
            'description': 'Shoprite Cairo Road',
            'amount': '245.50',
            'transaction_date': today,
            'transaction_type': 'DEBIT',
            'category_name': '',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Shoprite Cairo Road', response.data)
        self.assertIn(b'245.50 ZMW) under Groceries.', response.data)
        self.assertIn(b'id="csv-upload-loading"', response.data)

    def test_search_transactions_filters_by_merchant_and_export(self):
        from datetime import date
        today = date.today().isoformat()
        self.client.post('/transactions/add', data={
            'description': 'Shoprite Manda Hill',
            'amount': '300.00',
            'transaction_date': today,
            'transaction_type': 'DEBIT',
        }, follow_redirects=True)
        self.client.post('/transactions/add', data={
            'description': 'ZESCO Prepaid Token',
            'amount': '150.00',
            'transaction_date': today,
            'transaction_type': 'DEBIT',
        }, follow_redirects=True)

        filtered = self.client.get('/transactions?q=zesco').data
        self.assertIn(b'ZESCO Prepaid Token', filtered)
        self.assertNotIn(b'Shoprite Manda Hill', filtered)

        exported = self.client.get('/transactions/export?q=zesco').data
        self.assertIn(b'ZESCO Prepaid Token', exported)
        self.assertNotIn(b'Shoprite Manda Hill', exported)

    def test_edit_savings_goal_updates_target_and_date(self):
        from datetime import date, timedelta
        first_date = (date.today() + timedelta(days=90)).isoformat()
        updated_date = (date.today() + timedelta(days=180)).isoformat()

        self.client.post('/savings', data={
            'goal_name': 'Laptop Fund',
            'target_amount': '8000',
            'target_date': first_date,
        }, follow_redirects=True)

        import database
        conn = database.get_db_connection()
        goal_id = conn.execute('SELECT goal_id FROM SavingsGoal').fetchone()['goal_id']
        conn.close()

        res = self.client.post(f'/savings/{goal_id}/edit', data={
            'goal_name': 'Workstation Fund',
            'target_amount': '12500',
            'target_date': updated_date,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Workstation Fund', res.data)
        self.assertIn(b'12,500.00', res.data)
        self.assertIn(updated_date.encode('utf-8'), res.data)

    def test_settings_displays_per_category_mae_badge(self):
        import database
        conn = database.get_db_connection()
        user = database.get_user(conn)
        database.record_metric(
            conn, 'FORECASTING',
            algorithm='ARIMA / mean',
            mae=420.0, mae_ratio=0.052, sample_count=8,
            notes='Hold-out month 2026-06; monthly-total MAE; 8 categories evaluated; per-category MAE ratio 0.094',
            user_id=user['user_id'])
        conn.commit()
        conn.close()

        html = self.client.get('/settings').data
        self.assertIn(b'Per-category error (NFR-03)', html)
        self.assertIn(b'9.4%', html)

    def _restore(self, payload, name, password=PASSWORD):
        return self.client.post('/settings/restore', data={
            'backup_file': (io.BytesIO(payload), name),
            'current_password': password,
        }, content_type='multipart/form-data', follow_redirects=True)

    def _cleanup_safety_copies(self):
        import glob
        for bak in glob.glob(f'{self.db_path}.pre-restore-*.bak'):
            try:
                os.unlink(bak)
            except OSError:
                pass

    def test_backup_downloads_as_a_zip_holding_the_database(self):
        # Phones don't recognise .db files; both platforms' Files apps open zips.
        import zipfile
        response = self.client.get('/settings/export')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        self.assertIn('.zip', response.headers['Content-Disposition'])
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith('.db'))
        self.assertEqual(archive.read(names[0])[:16], b'SQLite format 3\x00')

    def test_restore_database_validates_and_restores_backup(self):
        self.addCleanup(self._cleanup_safety_copies)
        backup_zip = self.client.get('/settings/export').data

        # Not a backup: rejected, nothing changes.
        bad = self._restore(b'not a sqlite file', 'bad.db')
        self.assertIn(b'not an Expense Tracker backup', bad.data)

        # A transaction added after the backup was taken...
        from datetime import date
        self.client.post('/transactions/add', data={
            'description': 'Post-Backup Expense', 'amount': '99.00',
            'transaction_date': date.today().isoformat(), 'transaction_type': 'DEBIT'})
        self.assertIn(b'Post-Backup Expense', self.client.get('/transactions').data)

        # ...is gone after restoring the .zip backup.
        good = self._restore(backup_zip, 'expense_tracker-backup.zip')
        self.assertIn(b'Backup restored', good.data)
        self.assertNotIn(b'Post-Backup Expense', self.client.get('/transactions').data)
        import glob
        self.assertEqual(len(glob.glob(f'{self.db_path}.pre-restore-*.bak')), 1)

    def test_restore_accepts_a_plain_db_from_older_backups(self):
        self.addCleanup(self._cleanup_safety_copies)
        import zipfile
        archive = zipfile.ZipFile(io.BytesIO(self.client.get('/settings/export').data))
        raw_db = archive.read(archive.namelist()[0])
        response = self._restore(raw_db, 'old-backup.db')
        self.assertIn(b'Backup restored', response.data)

    def test_restore_needs_the_current_password(self):
        backup_zip = self.client.get('/settings/export').data
        response = self._restore(backup_zip, 'backup.zip', password='wrong-password')
        self.assertIn(b'Enter your current password to restore', response.data)
        import glob
        self.assertEqual(glob.glob(f'{self.db_path}.pre-restore-*.bak'), [])

    def test_restore_refuses_a_database_with_triggers(self):
        # The app never creates triggers; one in an uploaded file would run
        # its own SQL on later writes.
        import sqlite3
        import tempfile
        import zipfile
        raw = zipfile.ZipFile(io.BytesIO(self.client.get('/settings/export').data))
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        handle.write(raw.read(raw.namelist()[0]))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        conn = sqlite3.connect(handle.name)
        conn.execute('CREATE TRIGGER t AFTER INSERT ON Category BEGIN '
                     'DELETE FROM Transaction_Record; END')
        conn.commit()
        conn.close()
        with open(handle.name, 'rb') as f:
            response = self._restore(f.read(), 'tampered.db')
        self.assertIn(b'contains triggers', response.data)

    def test_search_treats_percent_and_underscore_literally(self):
        from datetime import date
        for name in ('Shoprite 50% off', 'Pick n Pay'):
            self.client.post('/transactions/add', data={
                'description': name, 'amount': '10.00',
                'transaction_date': date.today().isoformat(), 'transaction_type': 'DEBIT'})
        self.client.get('/transactions')  # consume the "Added ..." flashes
        page = self.client.get('/transactions?q=%25').data   # q = "%"
        self.assertIn(b'Shoprite 50% off', page)
        self.assertNotIn(b'Pick n Pay', page)
        self.assertIn(b'1 shown', page)

    def test_cash_income_is_filed_as_income(self):
        from datetime import date
        response = self.client.post('/transactions/add', data={
            'description': 'Cash for tutoring', 'amount': '400.00',
            'transaction_date': date.today().isoformat(), 'transaction_type': 'CREDIT',
        }, follow_redirects=True)
        self.assertIn(b'under Income', response.data)

    def test_savings_goal_rejects_nan_and_infinite_targets(self):
        from datetime import date, timedelta
        later = (date.today() + timedelta(days=60)).isoformat()
        for amount in ('nan', 'inf', '1e300'):
            with self.subTest(amount=amount):
                response = self.client.post('/savings', data={
                    'goal_name': 'Bad goal', 'target_amount': amount,
                    'target_date': later}, follow_redirects=True)
                self.assertNotIn(b'Savings goal &#34;Bad goal&#34; added', response.data)
                self.assertNotIn(b'Savings goal "Bad goal" added', response.data)

    def test_per_category_mae_comes_from_its_own_column(self):
        import database
        conn = database.get_db_connection()
        user = database.get_user(conn)
        database.record_metric(conn, 'FORECASTING', algorithm='ARIMA / mean',
                               mae=420.0, mae_ratio=0.052, sample_count=8,
                               notes='Hold-out month 2026-06', user_id=user['user_id'],
                               per_category_mae_ratio=0.081)
        conn.commit()
        conn.close()
        self.assertIn(b'8.1%', self.client.get('/settings').data)


if __name__ == '__main__':
    unittest.main()
