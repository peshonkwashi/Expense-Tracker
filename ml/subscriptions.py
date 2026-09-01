"""Subscription Detection (FR-08).

Addresses the "subscription blindness" bias confirmed by survey questions Q9
and Q10: recurring deductions of a stable amount that the user has stopped
noticing.

A transaction group is a subscription when all three hold:
  * it recurs at least SUBSCRIPTION_MIN_OCCURRENCES times,
  * the amounts stay within +/- SUBSCRIPTION_AMOUNT_TOLERANCE of the median, and
  * the median gap between charges looks monthly (24-38 days).

The amount test is what separates a Netflix charge from three unrelated trips
to the same supermarket, and the interval test separates a monthly debit order
from a weekly habit.
"""
import statistics
from datetime import datetime

import config
from ml.categorization import normalise

RECURRING_QUERY = (
    "SELECT transaction_id, transaction_date, description, amount "
    "FROM Transaction_Record "
    "WHERE user_id = ? AND transaction_type = 'DEBIT' AND is_salary = 0 "
    "ORDER BY transaction_date"
)


def _merchant_key(description):
    """Collapse a description to a merchant key.

    Statement lines usually carry a trailing reference number that differs on
    every charge ("NETFLIX.COM 8837121"), so digits are dropped and only the
    first three words are kept.
    """
    tokens = [token for token in normalise(description).split() if not token.isdigit()]
    return ' '.join(tokens[:3])


def _parse(date_string):
    return datetime.strptime(str(date_string)[:10], '%Y-%m-%d').date()


def find_recurring(rows):
    """Pure detection over row dicts. Returns a list of subscription groups."""
    groups = {}
    for row in rows:
        key = _merchant_key(row['description'])
        if not key:
            continue
        groups.setdefault(key, []).append(row)

    detected = []
    for key, items in groups.items():
        if len(items) < config.SUBSCRIPTION_MIN_OCCURRENCES:
            continue

        amounts = [float(item['amount']) for item in items]
        median_amount = statistics.median(amounts)
        if median_amount <= 0:
            continue
        tolerance = median_amount * config.SUBSCRIPTION_AMOUNT_TOLERANCE
        if any(abs(amount - median_amount) > tolerance for amount in amounts):
            continue

        dates = sorted(_parse(item['transaction_date']) for item in items)
        gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
        if not gaps:
            continue
        median_gap = statistics.median(gaps)
        if not (config.SUBSCRIPTION_MIN_INTERVAL_DAYS <= median_gap
                <= config.SUBSCRIPTION_MAX_INTERVAL_DAYS):
            continue

        detected.append({
            'key': key,
            'label': items[-1]['description'],
            'monthly_amount': round(median_amount, 2),
            'occurrences': len(items),
            'median_interval_days': int(median_gap),
            'first_seen': dates[0].isoformat(),
            'last_seen': dates[-1].isoformat(),
            'transaction_ids': [item['transaction_id'] for item in items],
            'annualised': round(median_amount * 12, 2),
        })

    detected.sort(key=lambda item: item['monthly_amount'], reverse=True)
    return detected


def detect_subscriptions(conn, user_id, persist=True):
    """Detect recurring charges and flag them on Transaction_Record."""
    rows = [dict(row) for row in conn.execute(RECURRING_QUERY, (user_id,))]
    detected = find_recurring(rows)

    if persist:
        conn.execute(
            'UPDATE Transaction_Record SET is_subscription = 0 WHERE user_id = ?',
            (user_id,))
        flagged = [tid for group in detected for tid in group['transaction_ids']]
        if flagged:
            placeholders = ','.join('?' * len(flagged))
            conn.execute(
                f'UPDATE Transaction_Record SET is_subscription = 1 '
                f'WHERE transaction_id IN ({placeholders})', flagged)
        conn.commit()

    return detected


def subscription_summary(detected):
    monthly = sum(item['monthly_amount'] for item in detected)
    return {
        'count': len(detected),
        'monthly_total': round(monthly, 2),
        'annual_total': round(monthly * 12, 2),
        'entries': detected,
    }
