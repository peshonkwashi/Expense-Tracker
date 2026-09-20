"""Smart Suggestions Engine (dashboard insights).

Reads the current month's spending and produces plain-language savings
suggestions. Three rules, each a small testable function; all thresholds live
in config.py:

  1. High-frequency shopping — many trips to one shop this month.
  2. Concentration — one merchant taking over half a discretionary category.
  3. Subscription audit — a heavy recurring-charge load worth reviewing.

Suggestions are advisory and framed positively (an opportunity to save), in the
same spirit as the nudge alerts but looking at merchant behaviour rather than
budget thresholds.
"""
from collections import defaultdict

import config
import database
from ml.subscriptions import _merchant_key

# Categories where repeated visits are physical shopping trips, so
# "consolidate into fewer trips" is sensible advice. Fuel or a loan repayment
# would not be.
TRIP_CATEGORIES = ('Groceries', 'Shopping', 'Personal Care')

MONTH_DEBITS_QUERY = (
    "SELECT t.description, t.amount, t.is_subscription, "
    "       c.category_name, c.category_type "
    "FROM Transaction_Record t "
    "LEFT JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.user_id = ? AND t.transaction_type = 'DEBIT' AND t.is_salary = 0 "
    "AND strftime('%Y-%m', t.transaction_date) = ?"
)

SUBSCRIPTION_QUERY = (
    "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total "
    "FROM Transaction_Record WHERE user_id = ? AND is_subscription = 1 "
    "AND transaction_type = 'DEBIT' "
    "AND strftime('%Y-%m', transaction_date) = ?"
)


def _month_debits(conn, user_id, month_key):
    return [dict(row) for row in conn.execute(MONTH_DEBITS_QUERY,
                                              (user_id, month_key))]


def high_frequency_suggestions(rows):
    """Rule 1: more than N visits to the same shop this month."""
    groups = defaultdict(lambda: {'visits': 0, 'total': 0.0, 'name': None})
    for row in rows:
        if row['category_name'] not in TRIP_CATEGORIES:
            continue
        key = _merchant_key(row['description'])
        if not key:
            continue
        group = groups[key]
        group['visits'] += 1
        group['total'] += float(row['amount'])
        group['name'] = group['name'] or row['description']

    suggestions = []
    for group in groups.values():
        if group['visits'] > config.SUGGEST_HIGH_FREQUENCY_VISITS:
            suggestions.append({
                'title': f"Frequent trips to {group['name']}",
                'message': (
                    f"You visited {group['name']} {group['visits']} times this "
                    f"month (spending {group['total']:.2f} {config.CURRENCY}). "
                    f"Consolidating into bulk trips could save on transport."),
                'type': 'info',
            })
    return suggestions


def concentration_suggestions(rows):
    """Rule 2: one merchant taking over half a discretionary category.

    When the dominant merchant is a subscription the advice changes: you cannot
    shop around a subscription, so instead of a comparison nudge it surfaces the
    charge's true annual cost and suggests reviewing, downgrading or cancelling.
    """
    category_total = defaultdict(float)
    category_txns = defaultdict(int)
    merchant_spend = defaultdict(lambda: defaultdict(float))
    merchant_name = {}
    merchant_is_subscription = defaultdict(bool)

    for row in rows:
        if row['category_type'] != 'DISCRETIONARY':
            continue
        category = row['category_name']
        if not category or category == 'Uncategorised':
            continue  # "shop around your uncategorised spending" is not advice
        key = _merchant_key(row['description']) or row['description']
        category_total[category] += float(row['amount'])
        category_txns[category] += 1
        merchant_spend[category][key] += float(row['amount'])
        merchant_name.setdefault(key, row['description'])
        if row['is_subscription']:
            merchant_is_subscription[key] = True

    suggestions = []
    for category, total in category_total.items():
        # Need a real pattern, not a single purchase, before advising.
        if total <= 0 or category_txns[category] < 2:
            continue
        key, spend = max(merchant_spend[category].items(), key=lambda kv: kv[1])
        share = spend / total
        if share <= config.SUGGEST_CONCENTRATION_SHARE:
            continue

        name = merchant_name[key]
        if merchant_is_subscription[key] or category == 'Subscriptions':
            # A subscription cannot be shopped around — reframe as a keep/cancel
            # decision, and make the yearly cost (which monthly billing hides)
            # visible.
            annual = spend * 12
            suggestions.append({
                'title': f"{name} is your biggest recurring charge",
                'message': (
                    f"{name} takes {share * 100:.0f}% of your {category} spending "
                    f"— {spend:,.2f} {config.CURRENCY} this month, about "
                    f"{annual:,.2f} {config.CURRENCY} a year. You can't shop around "
                    f"a subscription, but if you're not using it regularly, "
                    f"downgrading the plan or cancelling is the cleanest saving."),
                'type': 'warning',
            })
        else:
            suggestions.append({
                'title': f"{category} spending is concentrated",
                'message': (
                    f"Over half of your {category} spending ({share * 100:.0f}%) "
                    f"goes to {name}. Shopping around could yield savings."),
                'type': 'warning',
            })
    return suggestions


def subscription_audit_suggestions(conn, user_id, month_key, salary):
    """Rule 3: a heavy subscription load worth reviewing."""
    row = conn.execute(SUBSCRIPTION_QUERY, (user_id, month_key)).fetchone()
    if not row or row['n'] < config.SUGGEST_SUBSCRIPTION_MIN_COUNT:
        return []

    total = float(row['total'])
    threshold = (salary or 0) * config.SUGGEST_SUBSCRIPTION_SALARY_SHARE
    if total <= 0 or total < threshold:
        return []

    message = (f"You have {row['n']} recurring charges costing {total:.2f} "
               f"{config.CURRENCY} this month")
    if salary:
        message += f" — about {total / salary * 100:.0f}% of your salary"
    message += ". Review whether you still use all of them."
    return [{'title': 'Review your subscriptions', 'message': message,
             'type': 'warning'}]


def generate_suggestions(conn, user_id, month_key, salary=None):
    """Run all three rules and return the suggestions, most actionable first."""
    if salary is None:
        user = database.get_user(conn)
        salary = float(user['salary_amount']) if user else 0.0

    rows = _month_debits(conn, user_id, month_key)
    suggestions = []
    suggestions += concentration_suggestions(rows)
    suggestions += subscription_audit_suggestions(conn, user_id, month_key, salary)
    suggestions += high_frequency_suggestions(rows)

    priority = {'warning': 0, 'info': 1}
    suggestions.sort(key=lambda item: priority.get(item['type'], 2))
    return suggestions
