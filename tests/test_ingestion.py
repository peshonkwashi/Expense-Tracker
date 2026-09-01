"""Unit tests for the Data Acquisition Layer (section 5.10.2)."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from services import ingestion  # noqa: E402
from tests.helpers import insert_transaction, temp_database, write_csv  # noqa: E402


class ColumnResolutionTests(unittest.TestCase):
    def test_resolves_standard_headers(self):
        resolved = ingestion.resolve_columns(['Date', 'Description', 'Amount', 'Type'])
        self.assertEqual(resolved['date'], 'Date')
        self.assertEqual(resolved['description'], 'Description')

    def test_resolves_bank_specific_aliases(self):
        resolved = ingestion.resolve_columns(
            ['Transaction Date', 'Narration', 'Debit', 'Credit'])
        self.assertEqual(resolved['date'], 'Transaction Date')
        self.assertEqual(resolved['description'], 'Narration')
        self.assertEqual(resolved['debit'], 'Debit')

    def test_ignores_case_and_separators(self):
        resolved = ingestion.resolve_columns(['value_date', 'TRANSACTION_TYPE'])
        self.assertEqual(resolved['date'], 'value_date')
        self.assertEqual(resolved['type'], 'TRANSACTION_TYPE')


class AmountParsingTests(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(ingestion._parse_amount('1200.50'), 1200.50)

    def test_thousands_separator(self):
        self.assertEqual(ingestion._parse_amount('12,500.00'), 12500.00)

    def test_currency_prefix(self):
        self.assertEqual(ingestion._parse_amount('ZMW 340.00'), 340.00)

    def test_bracketed_negative(self):
        self.assertEqual(ingestion._parse_amount('(250.00)'), -250.00)

    def test_blank_returns_none(self):
        self.assertIsNone(ingestion._parse_amount(''))
        self.assertIsNone(ingestion._parse_amount('-'))

    def test_garbage_returns_none(self):
        self.assertIsNone(ingestion._parse_amount('not a number'))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.context = temp_database()
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def _import(self, rows, headers=('Date', 'Description', 'Amount', 'Type'),
                today=None):
        path = write_csv(headers, rows)
        try:
            return ingestion.import_statement(self.conn, self.user_id, path,
                                              today=today)
        finally:
            os.unlink(path)

    def test_imports_valid_rows(self):
        report = self._import([
            ('2026-01-05', 'Shoprite Manda Hill', '1200.00', 'Debit'),
            ('2026-01-06', 'ZESCO Prepaid Units', '300.00', 'Debit'),
        ])
        self.assertEqual(report['imported'], 2)
        self.assertEqual(report['rejected'], [])

    def test_rejects_bad_rows_individually(self):
        report = self._import([
            ('2026-01-05', 'Shoprite Manda Hill', '1200.00', 'Debit'),
            ('not-a-date', 'Broken Row', '100.00', 'Debit'),
            ('2026-01-07', '', '100.00', 'Debit'),
            ('2026-01-08', 'Zero Row', '0', 'Debit'),
        ])
        self.assertEqual(report['imported'], 1)
        self.assertEqual(len(report['rejected']), 3)

    def test_reuploading_the_same_file_is_idempotent(self):
        rows = [('2026-01-05', 'Shoprite Manda Hill', '1200.00', 'Debit'),
                ('2026-01-06', 'KFC Cairo Road', '150.00', 'Debit')]
        first = self._import(rows)
        second = self._import(rows)
        self.assertEqual(first['imported'], 2)
        self.assertEqual(second['imported'], 0)
        self.assertEqual(second['duplicates'], 2)

        total = self.conn.execute(
            'SELECT COUNT(*) AS n FROM Transaction_Record').fetchone()['n']
        self.assertEqual(total, 2)

    def test_genuine_same_day_repeat_is_kept(self):
        # Two identical bus fares on one day are two transactions, not a duplicate.
        report = self._import([
            ('2026-01-05', 'Taxi Fare Town', '50.00', 'Debit'),
            ('2026-01-05', 'Taxi Fare Town', '50.00', 'Debit'),
        ])
        self.assertEqual(report['imported'], 2)

    def test_salary_credit_is_flagged(self):
        report = self._import([
            ('2026-01-25', 'SALARY CREDIT EMPLOYER', '10000.00', 'Credit'),
            ('2026-01-26', 'Refund from shop', '120.00', 'Credit'),
        ])
        self.assertEqual(report['salary_rows'], 1)
        flagged = self.conn.execute(
            'SELECT COUNT(*) AS n FROM Transaction_Record WHERE is_salary = 1'
        ).fetchone()['n']
        self.assertEqual(flagged, 1)

    def test_separate_debit_credit_columns(self):
        path = write_csv(('Date', 'Narration', 'Debit', 'Credit'), [
            ('2026-02-01', 'Shoprite Manda Hill', '450.00', ''),
            ('2026-02-25', 'SALARY CREDIT EMPLOYER', '', '10000.00'),
        ])
        try:
            report = ingestion.import_statement(self.conn, self.user_id, path)
        finally:
            os.unlink(path)
        self.assertEqual(report['imported'], 2)
        types = [row['transaction_type'] for row in self.conn.execute(
            'SELECT transaction_type FROM Transaction_Record ORDER BY transaction_date')]
        self.assertEqual(types, ['DEBIT', 'CREDIT'])

    def test_signed_amount_without_type_column(self):
        path = write_csv(('Date', 'Description', 'Amount'), [
            ('2026-03-01', 'Shoprite Manda Hill', '-450.00'),
            ('2026-03-25', 'SALARY CREDIT EMPLOYER', '10000.00'),
        ])
        try:
            ingestion.import_statement(self.conn, self.user_id, path)
        finally:
            os.unlink(path)
        rows = self.conn.execute(
            'SELECT transaction_type, amount FROM Transaction_Record '
            'ORDER BY transaction_date').fetchall()
        self.assertEqual(rows[0]['transaction_type'], 'DEBIT')
        self.assertEqual(rows[0]['amount'], 450.00)
        self.assertEqual(rows[1]['transaction_type'], 'CREDIT')

    def test_missing_required_column_rejects_whole_file(self):
        path = write_csv(('Date', 'Amount'), [('2026-01-01', '100')])
        try:
            with self.assertRaises(ingestion.CsvValidationError):
                ingestion.import_statement(self.conn, self.user_id, path)
        finally:
            os.unlink(path)

    def test_empty_file_rejected(self):
        handle = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False)
        handle.close()
        try:
            with self.assertRaises(ingestion.CsvValidationError):
                ingestion.import_statement(self.conn, self.user_id, handle.name)
        finally:
            os.unlink(handle.name)

    def test_sql_injection_in_description_is_stored_literally(self):
        # Parameterised queries mean this is data, never SQL (NFR-05).
        payload = "Shoprite'; DROP TABLE Transaction_Record; --"
        report = self._import([('2026-01-05', payload, '100.00', 'Debit')])
        self.assertEqual(report['imported'], 1)
        stored = self.conn.execute(
            'SELECT description FROM Transaction_Record').fetchone()['description']
        self.assertEqual(stored, payload)


class DateRangeTests(unittest.TestCase):
    """FR-01 issue #1: implausible dates must be rejected, not stored."""

    TODAY = date(2026, 6, 15)

    def test_valid_recent_date_passes(self):
        self.assertIsNone(ingestion.date_out_of_range(date(2026, 5, 1), self.TODAY))

    def test_future_date_is_rejected(self):
        reason = ingestion.date_out_of_range(date(2062, 1, 5), self.TODAY)
        self.assertIsNotNone(reason)
        self.assertIn('future', reason)

    def test_far_past_date_is_rejected(self):
        reason = ingestion.date_out_of_range(date(1985, 1, 5), self.TODAY)
        self.assertIsNotNone(reason)
        self.assertIn('years old', reason)

    def test_today_is_allowed(self):
        self.assertIsNone(ingestion.date_out_of_range(self.TODAY, self.TODAY))

    def test_small_future_grace_is_allowed(self):
        # A pending/value-dated row a day or two ahead is tolerated.
        within = self.TODAY + timedelta(days=config.FUTURE_DATE_GRACE_DAYS)
        self.assertIsNone(ingestion.date_out_of_range(within, self.TODAY))

    def test_beyond_grace_is_rejected(self):
        beyond = self.TODAY + timedelta(days=config.FUTURE_DATE_GRACE_DAYS + 1)
        self.assertIsNotNone(ingestion.date_out_of_range(beyond, self.TODAY))

    def test_boundary_just_inside_the_age_floor_passes(self):
        earliest = self.TODAY - timedelta(
            days=int(365.25 * config.MAX_TRANSACTION_AGE_YEARS) - 1)
        self.assertIsNone(ingestion.date_out_of_range(earliest, self.TODAY))


class DateRangeImportTests(unittest.TestCase):
    def setUp(self):
        self.context = temp_database()
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def _import(self, rows, today=None):
        path = write_csv(('Date', 'Description', 'Amount', 'Type'), rows)
        try:
            return ingestion.import_statement(self.conn, self.user_id, path,
                                              today=today)
        finally:
            os.unlink(path)

    def test_bad_date_row_is_rejected_but_good_rows_still_import(self):
        report = self._import([
            ('2026-05-01', 'Shoprite Manda Hill', '1200.00', 'Debit'),
            ('2062-01-01', 'Typo Year Row', '300.00', 'Debit'),
            ('2026-05-03', 'Fuel Puma', '800.00', 'Debit'),
        ], today=date(2026, 6, 15))
        self.assertEqual(report['imported'], 2)
        self.assertEqual(len(report['rejected']), 1)
        self.assertIn('future', report['rejected'][0][1])

    def test_a_typo_year_does_not_reach_the_database(self):
        self._import([('2062-01-01', 'Typo Year Row', '300.00', 'Debit')],
                     today=date(2026, 6, 15))
        span = self.conn.execute(
            'SELECT MAX(transaction_date) AS latest FROM Transaction_Record'
        ).fetchone()['latest']
        self.assertIsNone(span)  # nothing stored, so no decade-long history


class AmountCeilingTests(unittest.TestCase):
    """FR-01 issue #2: a misparsed number must not become a real transaction."""

    def setUp(self):
        self.context = temp_database()
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def _import(self, rows, today=date(2026, 6, 15)):
        path = write_csv(('Date', 'Description', 'Amount', 'Type'), rows)
        try:
            return ingestion.import_statement(self.conn, self.user_id, path,
                                              today=today)
        finally:
            os.unlink(path)

    def test_absurd_amount_is_rejected(self):
        # A phone number that landed in the amount column: ~2.6e11.
        report = self._import([('2026-05-01', 'Bad Row', '260977123456', 'Debit')])
        self.assertEqual(report['imported'], 0)
        self.assertEqual(len(report['rejected']), 1)
        self.assertIn('exceeds the plausible maximum', report['rejected'][0][1])

    def test_large_but_legitimate_amount_is_kept(self):
        # A big-but-real transaction (e.g. a house deposit) must still import.
        report = self._import([('2026-05-01', 'Property Deposit', '250000.00', 'Debit')])
        self.assertEqual(report['imported'], 1)

    def test_ceiling_boundary(self):
        just_over = f'{config.MAX_TRANSACTION_AMOUNT + 1:.2f}'
        report = self._import([('2026-05-01', 'Over Ceiling', just_over, 'Debit')])
        self.assertEqual(report['imported'], 0)

    def test_bad_amount_does_not_distort_totals(self):
        self._import([
            ('2026-05-01', 'Shoprite Manda Hill', '1200.00', 'Debit'),
            ('2026-05-02', 'Misparsed Reference', '999999999999', 'Debit'),
        ])
        total = self.conn.execute(
            'SELECT SUM(amount) AS t FROM Transaction_Record').fetchone()['t']
        self.assertEqual(total, 1200.00)


class SalaryDetectionTests(unittest.TestCase):
    """FR-01 issue #3: identify the salary precisely, not every large credit."""

    def setUp(self):
        # Salary 10,000 on day 25 (temp_database defaults).
        self.context = temp_database(salary=10000.0, salary_day=25)
        self.conn, self.user_id = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def _credit(self, when, amount, desc='CREDIT'):
        insert_transaction(self.conn, self.user_id, when, desc, amount,
                           txn_type='CREDIT', category='Uncategorised')

    def _flagged_dates(self):
        return {r['transaction_date'] for r in self.conn.execute(
            'SELECT transaction_date FROM Transaction_Record WHERE is_salary = 1')}

    def test_day_of_month_distance_wraps(self):
        self.assertEqual(ingestion._day_of_month_distance(25, 25), 0)
        self.assertEqual(ingestion._day_of_month_distance(1, 30), 2)   # month wrap
        self.assertEqual(ingestion._day_of_month_distance(10, 25), 15)

    def test_salary_flagged_refund_ignored(self):
        self._credit('2026-04-25', 10000.0, 'SALARY EMPLOYER')
        self._credit('2026-04-26', 10000.0, 'Refund from shop')  # same size...
        # ...but the refund is a day off the pay day and loses the tie-break;
        # only one credit per month is chosen.
        chosen = ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(self._flagged_dates(), {'2026-04-25'})

    def test_one_salary_per_month_across_months(self):
        self._credit('2026-04-25', 10000.0)
        self._credit('2026-05-25', 10000.0)
        self._credit('2026-06-24', 10050.0)
        chosen = ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(len(chosen), 3)

    def test_salary_sized_credit_far_from_payday_is_not_salary(self):
        # Exactly salary-sized, but on the 10th when pay day is the 25th:
        # distance 15 > window, so it is a coincidental credit, not salary.
        self._credit('2026-04-10', 10000.0, 'Big refund')
        chosen = ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(chosen, [])

    def test_bonus_outside_tolerance_is_not_salary(self):
        self._credit('2026-04-25', 10000.0, 'SALARY')
        self._credit('2026-04-25', 20000.0, 'Annual bonus')  # 2x, out of band
        ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(self._flagged_dates(), {'2026-04-25'})
        # And the bonus row specifically is not flagged.
        bonus = self.conn.execute(
            "SELECT is_salary FROM Transaction_Record WHERE amount = 20000.0"
        ).fetchone()['is_salary']
        self.assertEqual(bonus, 0)

    def test_two_salary_sized_credits_same_month_picks_nearest_payday(self):
        self._credit('2026-04-22', 10000.0, 'Credit A')   # dist 3
        self._credit('2026-04-25', 10000.0, 'Credit B')   # dist 0 -> salary
        ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(self._flagged_dates(), {'2026-04-25'})

    def test_minor_amount_variation_still_matches(self):
        # Overtime/tax nudges net pay a little; within tolerance it still counts.
        self._credit('2026-04-25', 10800.0)   # +8%, inside 15% band
        chosen = ingestion.detect_salary(self.conn, self.user_id)
        self.assertEqual(len(chosen), 1)

    def test_redetection_is_idempotent(self):
        self._credit('2026-04-25', 10000.0)
        self._credit('2026-04-26', 10000.0)
        ingestion.detect_salary(self.conn, self.user_id)
        ingestion.detect_salary(self.conn, self.user_id)
        count = self.conn.execute(
            'SELECT COUNT(*) AS n FROM Transaction_Record WHERE is_salary = 1'
        ).fetchone()['n']
        self.assertEqual(count, 1)

    def test_month_end_pay_day_wraps_to_first(self):
        with temp_database(salary=10000.0, salary_day=30) as (conn, uid):
            insert_transaction(conn, uid, '2026-05-01', 'SALARY', 10000.0,
                               txn_type='CREDIT', category='Uncategorised')
            chosen = ingestion.detect_salary(conn, uid)
            self.assertEqual(len(chosen), 1)


if __name__ == '__main__':
    unittest.main()
