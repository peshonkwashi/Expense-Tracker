"""Unit tests for the Data Acquisition Layer (section 5.10.2)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ingestion  # noqa: E402
from tests.helpers import temp_database, write_csv  # noqa: E402


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

    def _import(self, rows, headers=('Date', 'Description', 'Amount', 'Type')):
        path = write_csv(headers, rows)
        try:
            return ingestion.import_statement(self.conn, self.user_id, path,
                                              salary_amount=10000.0)
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
            report = ingestion.import_statement(self.conn, self.user_id, path,
                                                salary_amount=10000.0)
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
            ingestion.import_statement(self.conn, self.user_id, path,
                                       salary_amount=10000.0)
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


if __name__ == '__main__':
    unittest.main()
