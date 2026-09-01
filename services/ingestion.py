"""Data Acquisition Layer (section 3.6.1, FR-01).

Validates, parses, deduplicates and stores uploaded bank statements. Bank CSV
exports are not standardised, so column names are resolved through an alias
table and both statement conventions are supported:

  * a single Amount column plus a Type column (Debit/Credit), and
  * separate Debit and Credit columns, or a signed Amount.

Rows that cannot be parsed are rejected individually with a reason rather than
aborting the whole upload, and every reason is reported back to the user.
"""
import hashlib
import warnings
from collections import Counter
from datetime import datetime

import pandas as pd

import database
from ml import categorization

# Canonical field -> accepted header names (compared case- and space-insensitively).
COLUMN_ALIASES = {
    'date': ['date', 'transactiondate', 'transdate', 'postingdate', 'valuedate',
             'datetime', 'bookingdate'],
    'description': ['description', 'narration', 'details', 'particulars', 'merchant',
                    'reference', 'transactiondetails', 'memo'],
    'amount': ['amount', 'value', 'transactionamount'],
    'type': ['type', 'transactiontype', 'drcr', 'debitcredit', 'indicator'],
    'debit': ['debit', 'withdrawal', 'moneyout', 'debitamount'],
    'credit': ['credit', 'deposit', 'moneyin', 'creditamount'],
}

CREDIT_TOKENS = {'credit', 'cr', 'c', 'deposit', 'in', 'income'}
DEBIT_TOKENS = {'debit', 'dr', 'd', 'withdrawal', 'out', 'expense', 'payment'}

MAX_ROWS = 20000


class CsvValidationError(Exception):
    """The file cannot be processed at all (bad headers, unreadable, empty)."""


def _canonical(header):
    return ''.join(str(header).lower().split()).replace('_', '').replace('-', '')


def resolve_columns(headers):
    """Map canonical field names onto the actual headers present in the file."""
    lookup = {_canonical(header): header for header in headers}
    resolved = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[field] = lookup[alias]
                break
    return resolved


def _parse_amount(value):
    """Parse a money value, tolerating thousands separators and (123.45)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {'nan', 'none', '-', ''}:
        return None
    negative = text.startswith('(') and text.endswith(')')
    text = text.strip('()')
    for token in [',', ' ', 'ZMW', 'zmw', 'K', 'k']:
        text = text.replace(token, '')
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value):
    """Parse a statement date, returning a date or None.

    ISO dates are matched exactly first. Anything else is handed to pandas with
    dayfirst=True, because Zambian bank statements use DD/MM/YYYY and 05/03 has
    to mean the 5th of March, not the 3rd of May.
    """
    text = str(value or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        parsed = pd.to_datetime(text, errors='coerce', dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()


def _row_type(row, columns):
    """Determine (transaction_type, amount) for one row, or (None, reason)."""
    if 'debit' in columns or 'credit' in columns:
        debit = _parse_amount(row.get(columns.get('debit'))) if 'debit' in columns else None
        credit = _parse_amount(row.get(columns.get('credit'))) if 'credit' in columns else None
        if debit:
            return 'DEBIT', abs(debit), None
        if credit:
            return 'CREDIT', abs(credit), None
        if 'amount' not in columns:
            return None, None, 'Both debit and credit columns are empty'

    amount = _parse_amount(row.get(columns.get('amount')))
    if amount is None:
        return None, None, 'Amount is missing or not a number'

    if 'type' in columns:
        token = str(row.get(columns['type']) or '').strip().lower()
        if token in CREDIT_TOKENS:
            return 'CREDIT', abs(amount), None
        if token in DEBIT_TOKENS:
            return 'DEBIT', abs(amount), None
        if token:
            return None, None, f"Unrecognised transaction type '{token}'"

    # No usable type column: fall back to the sign convention, where a negative
    # amount is money leaving the account.
    if amount < 0:
        return 'DEBIT', abs(amount), None
    return 'CREDIT', abs(amount), None


def _import_hash(user_id, date, description, amount, txn_type, occurrence):
    """Stable dedup key.

    The occurrence counter lets a statement legitimately contain the same
    charge twice on one day (two identical bus fares) while a re-uploaded file
    still deduplicates cleanly.
    """
    raw = f'{user_id}|{date}|{categorization.normalise(description)}|' \
          f'{amount:.2f}|{txn_type}|{occurrence}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def read_statement(filepath):
    """Read a CSV into a DataFrame, raising CsvValidationError when unusable."""
    try:
        frame = pd.read_csv(filepath, dtype=str, keep_default_na=False,
                            skip_blank_lines=True)
    except pd.errors.EmptyDataError:
        raise CsvValidationError('The file is empty.')
    except UnicodeDecodeError:
        try:
            frame = pd.read_csv(filepath, dtype=str, keep_default_na=False,
                                encoding='latin-1')
        except Exception as exc:
            raise CsvValidationError(f'The file could not be decoded: {exc}')
    except Exception as exc:
        raise CsvValidationError(f'The file could not be read as CSV: {exc}')

    if frame.empty:
        raise CsvValidationError('The file contains no data rows.')
    if len(frame) > MAX_ROWS:
        raise CsvValidationError(
            f'The file has {len(frame)} rows; the limit is {MAX_ROWS}.')

    columns = resolve_columns(frame.columns)
    missing = [field for field in ('date', 'description') if field not in columns]
    if 'amount' not in columns and not ({'debit', 'credit'} & set(columns)):
        missing.append('amount')
    if missing:
        raise CsvValidationError(
            'Missing required column(s): ' + ', '.join(missing) +
            '. Expected headers like Date, Description, Amount, Type. '
            f'Found: {", ".join(str(c) for c in frame.columns)}')
    return frame, columns


def import_statement(conn, user_id, filepath, salary_amount=None):
    """Import one statement file.

    Returns a report dict with counts and per-row rejection reasons. Raises
    CsvValidationError when the file itself is unusable.
    """
    frame, columns = read_statement(filepath)

    report = {'imported': 0, 'duplicates': 0, 'rejected': [], 'rows': len(frame),
              'categorised': Counter(), 'credits': 0, 'salary_rows': 0}
    occurrences = Counter()

    # One transaction, one INSERT, all inside a single transaction block so a
    # failure part-way through leaves no partial import behind (NFR-06).
    try:
        for position, raw in enumerate(frame.to_dict('records'), start=2):
            date_value = parse_date(raw.get(columns['date']))
            if date_value is None:
                report['rejected'].append(
                    (position, f"Unreadable date '{raw.get(columns['date'])}'"))
                continue
            date_string = date_value.isoformat()

            description = str(raw.get(columns['description']) or '').strip()
            if not description:
                report['rejected'].append((position, 'Description is blank'))
                continue

            txn_type, amount, reason = _row_type(raw, columns)
            if txn_type is None:
                report['rejected'].append((position, reason))
                continue
            if amount == 0:
                report['rejected'].append((position, 'Amount is zero'))
                continue

            key = (date_string, categorization.normalise(description),
                   round(amount, 2), txn_type)
            occurrences[key] += 1
            digest = _import_hash(user_id, date_string, description, amount,
                                  txn_type, occurrences[key])

            is_salary = 0
            if txn_type == 'CREDIT':
                report['credits'] += 1
                # A credit within 10% of the declared salary is the salary
                # deposit that anchors the cycle analysis (FR-04).
                if salary_amount and abs(amount - salary_amount) <= salary_amount * 0.10:
                    is_salary = 1
                    report['salary_rows'] += 1
                category_name, confidence, source = 'Uncategorised', None, 'default'
            else:
                category_name, confidence, source = categorization.classify(description)
                report['categorised'][category_name] += 1

            cursor = conn.execute(
                'INSERT OR IGNORE INTO Transaction_Record '
                '(transaction_date, description, amount, transaction_type, '
                ' is_salary, category_source, category_confidence, import_hash, '
                ' user_id, category_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (date_string, description, round(amount, 2), txn_type, is_salary,
                 source, confidence, digest, user_id,
                 database.category_id_for(conn, category_name)))

            if cursor.rowcount == 0:
                report['duplicates'] += 1
            else:
                report['imported'] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return report


def post_import_pipeline(conn, user_id):
    """Stage 2/3 of the data flow (section 5.6.5), run after every import.

    Retrains the categoriser on the enlarged labelled set, applies it to rows
    that were previously unlabelled, then re-detects subscriptions.
    """
    from ml import subscriptions

    training = categorization.train_categorization_model(conn, user_id)
    relabelled = 0
    if training['trained']:
        relabelled = categorization.recategorise_uncategorised(conn, user_id)
    detected = subscriptions.detect_subscriptions(conn, user_id)
    return {'training': training, 'relabelled': relabelled,
            'subscriptions': len(detected)}
