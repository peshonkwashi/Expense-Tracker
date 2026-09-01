"""Shared test fixtures: an isolated database and CSV builders.

Every test runs against its own temporary SQLite file so no test can see, or
corrupt, the user's real expense_tracker.db.
"""
import contextlib
import csv
import itertools
import os
import subprocess
import sys
import tempfile

import config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_row_counter = itertools.count()


@contextlib.contextmanager
def temp_database(full_name='Test User', salary=10000.0, salary_day=25):
    """Yield (connection, user_id) against a throwaway database file."""
    import database

    handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    handle.close()

    original_path = config.DATABASE_PATH
    original_model = config.CATEGORISER_PATH
    model_dir = tempfile.mkdtemp()

    config.DATABASE_PATH = handle.name
    config.CATEGORISER_PATH = os.path.join(model_dir, 'categoriser.joblib')

    try:
        database.init_db()
        conn = database.get_db_connection()
        cursor = conn.execute(
            'INSERT INTO User (full_name, salary_amount, salary_day) VALUES (?, ?, ?)',
            (full_name, salary, salary_day))
        conn.commit()
        yield conn, cursor.lastrowid
        conn.close()
    finally:
        config.DATABASE_PATH = original_path
        config.CATEGORISER_PATH = original_model
        for path in (handle.name, os.path.join(model_dir, 'categoriser.joblib')):
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        try:
            os.rmdir(model_dir)
        except OSError:
            pass


def write_csv(headers, rows):
    """Write a CSV to a temp file and return its path. Caller unlinks it."""
    handle = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False,
                                         newline='', encoding='utf-8')
    writer = csv.writer(handle)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    handle.close()
    return handle.name


def insert_transaction(conn, user_id, date, description, amount,
                       txn_type='DEBIT', category='Groceries', is_salary=0):
    """Insert a transaction directly, bypassing the CSV path."""
    import database
    conn.execute(
        'INSERT INTO Transaction_Record (transaction_date, description, amount, '
        'transaction_type, is_salary, category_source, import_hash, user_id, '
        'category_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (date, description, amount, txn_type, is_salary, 'rule',
         f'{date}-{description}-{amount}-{txn_type}-{next(_row_counter)}',
         user_id, database.category_id_for(conn, category)))
    conn.commit()


def build_statement(months=8, salary=12000.0, salary_day=25, seed=42):
    """Generate a synthetic statement into a temp file. Caller unlinks it.

    Tests generate their own data rather than reading sample_data/, so that
    regenerating the sample file with different parameters cannot break them.
    """
    handle = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
    handle.close()
    subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, 'generate_sample_data.py'),
         '--months', str(months), '--salary', str(salary),
         '--salary-day', str(salary_day), '--seed', str(seed),
         '--output', handle.name],
        check=True, capture_output=True, cwd=PROJECT_ROOT)
    return handle.name
