"""Recommendation and Insights Layer (section 3.6.4).

Turns forecasts into budget allocations (FR-07), nudge alerts (FR-09) and
savings projections (FR-11).

The allocation follows the needs-based hierarchy from section 2.2 rather than a
fixed 50/30/20 split, which the literature review criticises PocketGuard for:

  1. Essential categories are funded first, at forecast + 5% headroom.
  2. Savings commitments implied by the user's goals are reserved next.
  3. Whatever remains funds discretionary spending. If the discretionary
     forecast exceeds it, every discretionary category is scaled down
     proportionally and flagged, because telling the user to spend more than
     they earn is the failure mode this system exists to prevent.

Every allocation carries an `explanation` string, satisfying the transparency
requirement in section 5.9.
"""
from datetime import date

import config
import database
from ml import forecasting
from services import behavioural

ACTUAL_SPEND_QUERY = (
    "SELECT c.category_name, SUM(t.amount) AS spent "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.user_id = ? AND t.transaction_type = 'DEBIT' AND t.is_salary = 0 "
    "AND strftime('%Y-%m', t.transaction_date) = ? "
    "GROUP BY c.category_name"
)

MONTH_TOTALS_QUERY = (
    "SELECT transaction_type, SUM(amount) AS total "
    "FROM Transaction_Record WHERE user_id = ? "
    "AND strftime('%Y-%m', transaction_date) = ? GROUP BY transaction_type"
)


def _month_key(today=None):
    today = today or date.today()
    return today.strftime('%Y-%m')


def actual_spend(conn, user_id, month_key):
    return {row['category_name']: float(row['spent'])
            for row in conn.execute(ACTUAL_SPEND_QUERY, (user_id, month_key))}


def months_until(target_date, today=None):
    """Whole months remaining until a goal date, never less than 1."""
    today = today or date.today()
    target = behavioural._as_date(target_date)
    months = (target.year - today.year) * 12 + (target.month - today.month)
    return max(1, months)


def savings_pool(conn, user_id):
    """Cumulative income minus cumulative expenditure, floored at zero."""
    income = expenses = 0.0
    for row in conn.execute(
            "SELECT transaction_type, SUM(amount) AS total FROM Transaction_Record "
            "WHERE user_id = ? GROUP BY transaction_type", (user_id,)):
        if row['transaction_type'] == 'CREDIT':
            income = float(row['total'] or 0)
        else:
            expenses = float(row['total'] or 0)
    return income, expenses, max(0.0, income - expenses)


def goal_requirements(conn, user_id, today=None):
    """Per-goal funding status and the monthly contribution still needed (FR-11).

    The savings pool is allocated across goals by a waterfall in target-date
    order: the goal due soonest is funded first. Showing the whole pool against
    every goal — as the first version did — makes two goals look complete when
    only one can be.

    The monthly figure is based on what is *still* missing, not the full target,
    so a goal that is already half funded does not keep reserving budget it no
    longer needs.
    """
    today = today or date.today()
    goals = conn.execute(
        'SELECT * FROM SavingsGoal WHERE user_id = ? ORDER BY target_date, goal_id',
        (user_id,)).fetchall()

    _, _, remaining_pool = savings_pool(conn, user_id)

    requirements = []
    for goal in goals:
        target = float(goal['target_amount'])
        remaining_months = months_until(goal['target_date'], today)
        allocated = min(remaining_pool, target)
        remaining_pool -= allocated
        shortfall = max(0.0, target - allocated)

        requirements.append({
            'goal_id': goal['goal_id'],
            'goal_name': goal['goal_name'],
            'target_amount': target,
            'target_date': goal['target_date'],
            'months_remaining': remaining_months,
            'saved': round(allocated, 2),
            'shortfall': round(shortfall, 2),
            'progress': round(min(100.0, allocated / target * 100), 1) if target else 0.0,
            'monthly_required': round(shortfall / remaining_months, 2),
            'overdue': behavioural._as_date(goal['target_date']) < today,
        })
    return requirements


def build_budget(conn, user, today=None, month_key=None):
    """Produce the full budget recommendation for the coming month."""
    today = today or date.today()
    month_key = month_key or _month_key(today)
    user_id = user['user_id']
    salary = float(user['salary_amount'])

    forecasts = forecasting.generate_forecasts(conn, user_id, today=today)
    categories = database.category_map(conn)
    spent = actual_spend(conn, user_id, month_key)

    goals = goal_requirements(conn, user_id, today)
    goal_total = sum(goal['monthly_required'] for goal in goals)

    essentials, discretionary = [], []
    for name, forecast in forecasts.items():
        category = categories.get(name)
        if category is None:
            continue
        row = {
            'category': name,
            'type': category['category_type'],
            'forecast': forecast['amount'],
            'method': forecast['method'],
            'basis': forecast['basis'],
            'history': forecast['history'],
            'months': forecast['months'],
            'spent': round(spent.get(name, 0.0), 2),
        }
        (essentials if category['category_type'] == 'ESSENTIAL'
         else discretionary).append(row)

    # Step 1: essentials get forecast plus headroom.
    for row in essentials:
        row['recommended'] = round(row['forecast'] * (1 + config.ESSENTIAL_BUFFER), 2)
        row['scaled'] = False
        row['explanation'] = (
            f"{row['basis']} A {int(config.ESSENTIAL_BUFFER * 100)}% buffer is "
            'added because this is an essential category where a shortfall '
            'cannot simply be absorbed.')

    essential_total = sum(row['recommended'] for row in essentials)

    # Step 2: reserve the savings commitment implied by the user's own goals.
    savings_reserved = goal_total
    available = salary - essential_total - savings_reserved

    # Step 3: discretionary spending gets what is left.
    discretionary_forecast = sum(row['forecast'] for row in discretionary)
    scale = 1.0
    overcommitted = False

    if discretionary_forecast > 0 and available < discretionary_forecast:
        overcommitted = True
        scale = max(0.0, available / discretionary_forecast)

    for row in discretionary:
        row['recommended'] = round(row['forecast'] * scale, 2)
        row['scaled'] = overcommitted
        if overcommitted:
            row['explanation'] = (
                f"{row['basis']} Your forecast discretionary spending exceeds "
                f'what is left after essentials and savings, so every '
                f'discretionary category is trimmed to '
                f'{scale * 100:.0f}% of forecast. Cutting here protects the '
                'categories you cannot cut.')
        else:
            row['explanation'] = (
                f"{row['basis']} Your income covers this in full after "
                'essentials and savings commitments.')

    discretionary_total = sum(row['recommended'] for row in discretionary)
    allocated = essential_total + discretionary_total
    surplus = salary - allocated - savings_reserved

    # Essentials above income cannot be budgeted away — telling someone their
    # rent is lower than it is would be a fiction. The system reports the
    # deficit instead and raises it as a critical alert.
    essentials_exceed_income = essential_total > salary
    recommended_savings = savings_reserved + max(0.0, surplus)

    # With no goals set, still steer a floor of income into savings so the
    # surplus is not silently treated as spendable.
    savings_floor = salary * config.DEFAULT_SAVINGS_RATE
    if not goals and recommended_savings < savings_floor:
        recommended_savings = min(savings_floor, max(0.0, salary - allocated))

    rows = sorted(essentials + discretionary,
                  key=lambda item: item['recommended'], reverse=True)
    for row in rows:
        allocation = row['recommended']
        row['pct_of_salary'] = round(allocation / salary * 100, 1) if salary else 0.0
        row['used_pct'] = round(row['spent'] / allocation * 100, 1) if allocation else 0.0
        row['remaining'] = round(allocation - row['spent'], 2)
        if allocation and row['spent'] >= allocation:
            row['status'] = 'over'
        elif allocation and row['used_pct'] >= config.NUDGE_THRESHOLD * 100:
            row['status'] = 'warning'
        else:
            row['status'] = 'ok'

    return {
        'month': month_key,
        'salary': round(salary, 2),
        'rows': rows,
        'essential_total': round(essential_total, 2),
        'discretionary_total': round(discretionary_total, 2),
        'allocated_total': round(allocated, 2),
        'surplus': round(surplus, 2),
        'recommended_savings': round(recommended_savings, 2),
        'savings_rate': round(recommended_savings / salary * 100, 1) if salary else 0.0,
        'goals': goals,
        'goal_total': round(goal_total, 2),
        'overcommitted': overcommitted,
        'essentials_exceed_income': essentials_exceed_income,
        'deficit': round(max(0.0, allocated + savings_reserved - salary), 2),
        'scale': round(scale, 3),
        'spent_total': round(sum(row['spent'] for row in rows), 2),
    }


def persist_recommendations(conn, user_id, budget):
    """Write the month's allocations to BudgetRecommendation (section 5.6.5)."""
    for row in budget['rows']:
        category_id = database.category_id_for(conn, row['category'])
        existing = conn.execute(
            'SELECT recommendation_id FROM BudgetRecommendation '
            'WHERE user_id = ? AND month_year = ? AND category_id = ?',
            (user_id, budget['month'], category_id)).fetchone()

        if existing:
            conn.execute(
                'UPDATE BudgetRecommendation SET recommended_amount = ?, '
                'forecast_amount = ?, forecast_method = ?, explanation = ?, '
                'generated_at = CURRENT_TIMESTAMP WHERE recommendation_id = ?',
                (row['recommended'], row['forecast'], row['method'],
                 row['explanation'], existing['recommendation_id']))
        else:
            conn.execute(
                'INSERT INTO BudgetRecommendation (month_year, recommended_amount, '
                'forecast_amount, forecast_method, explanation, user_id, category_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (budget['month'], row['recommended'], row['forecast'],
                 row['method'], row['explanation'], user_id, category_id))
    conn.commit()


def generate_nudges(budget, cycle, subscriptions_summary):
    """Loss-framed alerts (FR-09, section 5.6.2).

    Framed as what the user stands to lose rather than what they have done
    wrong, per Thaler and Sunstein's nudge architecture. Ordered by severity so
    the dashboard panel leads with what matters.
    """
    nudges = []

    for row in budget['rows']:
        if row['recommended'] <= 0:
            continue
        if row['status'] == 'over':
            nudges.append({
                'severity': 'critical',
                'category': row['category'],
                'title': f"{row['category']} budget exceeded",
                'message': (
                    f"You have spent {row['spent']:.2f} {config.CURRENCY} on "
                    f"{row['category']} against a recommended "
                    f"{row['recommended']:.2f}. Every further Kwacha here comes "
                    f"out of your savings, not your spending money."),
                'why': row['explanation'],
            })
        elif row['status'] == 'warning':
            nudges.append({
                'severity': 'warning',
                'category': row['category'],
                'title': f"{row['category']} at {row['used_pct']:.0f}% of budget",
                'message': (
                    f"{row['remaining']:.2f} {config.CURRENCY} left for "
                    f"{row['category']} this month. At your current pace you "
                    f"stand to lose the {row['remaining']:.2f} that was meant "
                    f"to reach your savings."),
                'why': row['explanation'],
            })

    if budget.get('essentials_exceed_income'):
        nudges.append({
            'severity': 'critical',
            'category': 'Budget',
            'title': 'Your essential spending is larger than your salary',
            'message': (
                f"Essentials alone come to {budget['essential_total']:.2f} "
                f"{config.CURRENCY} against a salary of {budget['salary']:.2f}. "
                f"No amount of trimming discretionary spending closes a "
                f"{budget['deficit']:.2f} gap — this needs a fixed cost to change "
                f"(rent, a loan, a recurring bill) or additional income."),
            'why': 'Sum of forecast essential categories versus your declared '
                   'salary.',
        })
    elif budget['overcommitted']:
        nudges.append({
            'severity': 'critical',
            'category': 'Budget',
            'title': 'Your forecast spending exceeds your salary',
            'message': (
                f"Essentials and savings commitments account for "
                f"{budget['essential_total'] + budget['goal_total']:.2f} "
                f"{config.CURRENCY} of your {budget['salary']:.2f} salary. "
                f"Discretionary categories have been trimmed to "
                f"{budget['scale'] * 100:.0f}% of what you normally spend to "
                f"keep the month solvent."),
            'why': 'Needs-based allocation, section 2.2 of the project report.',
        })

    if subscriptions_summary and subscriptions_summary['count']:
        monthly = subscriptions_summary['monthly_total']
        if monthly > 0:
            nudges.append({
                'severity': 'info',
                'category': 'Subscriptions',
                'title': f"{subscriptions_summary['count']} recurring charges detected",
                'message': (
                    f"{monthly:.2f} {config.CURRENCY} leaves your account every "
                    f"month automatically, which is "
                    f"{subscriptions_summary['annual_total']:.2f} over a year. "
                    f"Cancelling one you no longer use is the cheapest saving "
                    f"available to you."),
                'why': 'Detected from repeated charges of a stable amount at '
                       'monthly intervals (FR-08).',
            })

    if cycle and cycle['has_surge']:
        nudges.append({
            'severity': 'info',
            'category': 'Spending pattern',
            'title': f"{cycle['surge_share']:.0f}% of your spending lands in week 1",
            'message': (
                f"You spend {cycle['surge_share']:.0f}% of your monthly total in "
                f"the first week after payday. Money moved to savings on payday "
                f"is money you will not miss by week four."),
            'why': 'Salary-cycle analysis of your own transaction history.',
        })

    order = {'critical': 0, 'warning': 1, 'info': 2}
    nudges.sort(key=lambda item: order.get(item['severity'], 3))
    return nudges


def savings_position(conn, user, budget, today=None):
    """Savings pool and goal progress (FR-11).

    The pool is cumulative income minus cumulative expenditure, allocated across
    goals by goal_requirements(). A goal is on track when the monthly amount it
    still needs fits inside what the budget frees up for saving.
    """
    today = today or date.today()
    user_id = user['user_id']

    income, expenses, pool = savings_pool(conn, user_id)
    goals = goal_requirements(conn, user_id, today)
    allocated = sum(goal['saved'] for goal in goals)
    available_monthly = budget['recommended_savings'] if budget else 0.0

    for goal in goals:
        goal['on_track'] = (goal['monthly_required'] <= available_monthly
                            if budget else False)

    return {
        'income': round(income, 2),
        'expenses': round(expenses, 2),
        'pool': round(pool, 2),
        'unallocated': round(max(0.0, pool - allocated), 2),
        'goals': goals,
        'recommended_monthly': available_monthly,
    }


def monthly_report(conn, user, month_key):
    """Income, expenditure, budget variance and savings for a month (FR-12)."""
    user_id = user['user_id']
    spent = actual_spend(conn, user_id, month_key)

    recommendations = {
        row['category_name']: row for row in conn.execute(
            'SELECT c.category_name, b.recommended_amount, b.forecast_amount, '
            'b.explanation FROM BudgetRecommendation b '
            'JOIN Category c ON b.category_id = c.category_id '
            'WHERE b.user_id = ? AND b.month_year = ?', (user_id, month_key))
    }

    income = expenditure = 0.0
    for row in conn.execute(MONTH_TOTALS_QUERY, (user_id, month_key)):
        if row['transaction_type'] == 'CREDIT':
            income = float(row['total'] or 0)
        else:
            expenditure = float(row['total'] or 0)

    categories = database.category_map(conn)
    lines = []
    for name in sorted(set(spent) | set(recommendations)):
        actual = round(spent.get(name, 0.0), 2)
        recommended = round(float(recommendations[name]['recommended_amount']), 2) \
            if name in recommendations else None
        variance = round(recommended - actual, 2) if recommended is not None else None
        lines.append({
            'category': name,
            'type': categories[name]['category_type'] if name in categories else '',
            'actual': actual,
            'recommended': recommended,
            'variance': variance,
            'status': ('over' if variance is not None and variance < 0
                       else 'under' if variance is not None else 'no-budget'),
        })

    return {
        'month': month_key,
        'income': round(income, 2),
        'expenditure': round(expenditure, 2),
        'net': round(income - expenditure, 2),
        'lines': lines,
        'has_budget': bool(recommendations),
    }


def available_months(conn, user_id):
    return [row['month'] for row in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', transaction_date) AS month "
        "FROM Transaction_Record WHERE user_id = ? ORDER BY month DESC", (user_id,))]
