"""Behavioural Learning Phase and salary-cycle analysis (FR-05).

This is the feature that distinguishes the system from the applications
reviewed in Chapter 2. For one full salary cycle the system observes and
withholds recommendations, because a budget generated from a fortnight of data
would simply be the planning fallacy with a computer attached.

It also anchors spending to the salary cycle rather than the calendar month, so
"week 1 after payday" means the week following the actual salary credit.
"""
import calendar
from datetime import date, datetime, timedelta

import config

FIRST_TXN_QUERY = (
    "SELECT MIN(transaction_date) AS first_date, MAX(transaction_date) AS last_date, "
    "COUNT(*) AS total FROM Transaction_Record WHERE user_id = ?"
)

CYCLE_SPEND_QUERY = (
    "SELECT t.transaction_date, t.amount, c.category_name, c.category_type "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.user_id = ? AND t.transaction_type = 'DEBIT' AND t.is_salary = 0"
)


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def last_salary_date(reference, salary_day):
    """The most recent salary credit date on or before `reference`.

    Clamped to the length of the month, so a salary_day of 31 pays on the 30th
    in April and on the 28th or 29th in February.
    """
    reference = _as_date(reference)
    day = min(salary_day, calendar.monthrange(reference.year, reference.month)[1])
    candidate = date(reference.year, reference.month, day)
    if candidate <= reference:
        return candidate

    year, month = (reference.year - 1, 12) if reference.month == 1 else \
                  (reference.year, reference.month - 1)
    day = min(salary_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def cycle_position(reference, salary_day):
    """Days elapsed since the last salary credit (0 = payday itself)."""
    reference = _as_date(reference)
    return (reference - last_salary_date(reference, salary_day)).days


def cycle_week(reference, salary_day):
    """Week of the salary cycle, 1-4, where week 1 is the week after payday.

    Days 28 onward fold into week 4. A salary cycle is only ever 28-31 days, so
    a separate fifth bucket would hold two or three days and read on a chart as
    a collapse in spending rather than as a short week.
    """
    return min(4, cycle_position(reference, salary_day) // 7 + 1)


def learning_status(conn, user, today=None):
    """Progress through the Behavioural Learning Phase.

    Complete once the transaction history spans a full salary cycle and holds
    enough transactions to be worth modelling. Both conditions matter: a single
    bulk upload of thirty days with four transactions has the span but not the
    substance.
    """
    today = _as_date(today or date.today())
    row = conn.execute(FIRST_TXN_QUERY, (user['user_id'],)).fetchone()

    status = {
        'complete': False, 'days_observed': 0,
        'days_required': config.LEARNING_PHASE_DAYS,
        'transactions': 0,
        'transactions_required': config.MIN_LEARNING_TRANSACTIONS,
        'progress': 0.0, 'started_on': None, 'completes_on': None,
        'message': 'Upload your first bank statement to begin the learning phase.',
    }

    if not row or not row['first_date'] or row['total'] == 0:
        return status

    started = _as_date(row['first_date'])
    latest = _as_date(row['last_date'])
    # Measured against the data, not the wall clock: a user who uploads six
    # months of history on day one has genuinely been observed for six months.
    observed = (max(latest, today) - started).days

    status.update({
        'started_on': started.isoformat(),
        'completes_on': (started + timedelta(days=config.LEARNING_PHASE_DAYS)).isoformat(),
        'days_observed': observed,
        'transactions': row['total'],
    })

    span_ratio = observed / config.LEARNING_PHASE_DAYS
    volume_ratio = row['total'] / config.MIN_LEARNING_TRANSACTIONS
    status['progress'] = round(min(1.0, min(span_ratio, volume_ratio)) * 100, 1)

    if span_ratio >= 1 and volume_ratio >= 1:
        status['complete'] = True
        status['message'] = (
            'Learning phase complete. Recommendations are based on '
            f'{row["total"]} transactions observed over {observed} days.')
    elif span_ratio < 1:
        remaining = config.LEARNING_PHASE_DAYS - observed
        status['message'] = (
            f'Learning phase: {observed} of {config.LEARNING_PHASE_DAYS} days '
            f'observed. {remaining} more day(s) of history needed before '
            'personalised budgets are generated.')
    else:
        needed = config.MIN_LEARNING_TRANSACTIONS - row['total']
        status['message'] = (
            f'Learning phase: {row["total"]} transactions recorded. About '
            f'{needed} more are needed before your spending pattern is clear '
            'enough to budget from.')
    return status


def cycle_profile(conn, user, today=None):
    """Spending distribution across a typical salary cycle (survey Q7).

    Figures are **averaged per pay cycle**, not summed over all history. Summing
    is what the first version did, and it made the chart unreadable: eight
    months of data against a K8,000 salary produced a week-1 bar of K51,000,
    which is not a number the user can compare to anything they recognise. An
    average cycle can be read straight against the salary.

    The cycle in progress is excluded, for the same reason the forecaster stops
    at the last complete month: a cycle three days old would drag the average
    down as though spending had stopped.

    Returns per-week averages and a surge indicator — the share of cycle
    spending landing in the first seven days after payday, which is the
    present-bias post-salary surge described in the literature. The share is a
    ratio, so it was correct before and is unchanged by the fix.
    """
    today = _as_date(today or date.today())
    salary_day = user['salary_day']
    current_cycle_start = last_salary_date(today, salary_day)

    # cycle start date -> {week: amount spent in that week of that cycle}
    per_cycle = {}
    essential_by_cycle = {}
    discretionary_by_cycle = {}

    for row in conn.execute(CYCLE_SPEND_QUERY, (user['user_id'],)):
        when = _as_date(row['transaction_date'])
        amount = float(row['amount'])
        start = last_salary_date(when, salary_day)

        buckets = per_cycle.setdefault(start, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0})
        buckets[cycle_week(when, salary_day)] += amount

        if row['category_type'] == 'ESSENTIAL':
            essential_by_cycle[start] = essential_by_cycle.get(start, 0.0) + amount
        else:
            discretionary_by_cycle[start] = \
                discretionary_by_cycle.get(start, 0.0) + amount

    complete = {start: buckets for start, buckets in per_cycle.items()
                if start != current_cycle_start}
    # Fall back to the in-progress cycle only when it is all there is, so a new
    # user still sees something rather than an empty chart.
    source = complete or per_cycle
    partial_only = not complete and bool(per_cycle)
    cycles = len(source) or 1

    weeks = {week: round(sum(buckets[week] for buckets in source.values()) / cycles, 2)
             for week in (1, 2, 3, 4)}

    total = sum(weeks.values())
    surge = (weeks[1] / total * 100) if total else 0.0

    essential = sum(essential_by_cycle.get(start, 0.0) for start in source) / cycles
    discretionary = sum(discretionary_by_cycle.get(start, 0.0)
                        for start in source) / cycles

    return {
        'weeks': {f'Week {week}': amount for week, amount in weeks.items()},
        'total': round(total, 2),
        'cycles': len(source),
        'partial_only': partial_only,
        'surge_share': round(surge, 1),
        'essential': round(essential, 2),
        'discretionary': round(discretionary, 2),
        'discretionary_share': round(discretionary / total * 100, 1) if total else 0.0,
        'has_surge': surge >= 35.0,
    }
