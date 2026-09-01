"""Generate a synthetic bank statement for development and testing.

The report commits to using synthetic or consented data only (section 3.9), and
the models need volume: ARIMA needs several complete months, and the F1
evaluation needs enough examples per category to cross-validate. Fourteen rows
across three months is not enough for either.

This produces a statement with a realistic salary-cycle shape: recurring bills
on fixed dates, subscriptions at a stable amount, and discretionary spending
concentrated in the days right after payday.

    python generate_sample_data.py --months 8 --salary 12000 --salary-day 25
"""
import argparse
import csv
import random
from datetime import date, timedelta

# The salary the baselines below are calibrated against. Amounts are scaled
# proportionally when a different --salary is requested, so the generated
# persona always lives at roughly the same ratio to their income.
SALARY_REFERENCE = 12000.0

# Calibration factor applied on top of the scaling above. The category baselines
# were written independently and happen to sum to slightly more than income;
# this brings the generated persona to roughly 93% of salary, which leaves a
# thin but real surplus. The target user of this system runs close to the line —
# a persona who saves 40% of income would not exercise the nudge logic at all.
PERSONA_SPEND_RATIO = 0.90

# (merchant pool, transactions per month, monthly baseline range, volatility, bias)
#
# Spending is modelled as a stable monthly baseline per category with modest
# month-to-month variation, not as independent random draws. That matters: real
# people are creatures of habit, and a grocery bill that swings uniformly
# between K350 and K1,400 every month is noise no forecaster could learn from.
# Volatility is per-category — rent does not move, entertainment does.
#
# cycle bias: 'post-salary' clusters in the week after payday, 'fixed' lands on
# a set day, 'even' is spread across the month.
SPENDING_PROFILE = [
    (['Shoprite Manda Hill', 'Pick n Pay Levy', 'Shoprite Cairo Rd',
      'Spar Woodlands'], (3, 5), (2400, 3200), 0.10, 'even'),
    (['Fuel Puma Kabulonga', 'Engen Great East Rd', 'Total Filling Station'],
     (2, 3), (1300, 1700), 0.10, 'even'),
    (['Yango Trip', 'Taxi Fare Town'], (3, 5), (350, 500), 0.15, 'even'),
    (['ZESCO Prepaid Units'], (1, 2), (300, 420), 0.12, 'fixed'),
    (['LWSC Water Bill'], (1, 1), (180, 240), 0.06, 'fixed'),
    (['KFC Cairo Road', 'Hungry Lion Kabwata', 'Debonairs Pizza',
      'Cafe Mocha'], (3, 5), (600, 900), 0.20, 'post-salary'),
    (['Airtel Airtime Recharge', 'MTN Data Bundle'], (2, 4), (200, 300), 0.12, 'even'),
    (['Link Pharmacy', 'Medical Clinic Visit'], (1, 2), (150, 300), 0.25, 'even'),
    (['Mr Price Clothing', 'Game Store Electronics', 'Jumia Order'],
     (1, 2), (400, 700), 0.30, 'post-salary'),
    (['Betway Deposit', 'Cinema Ticket Arcades'], (1, 2), (200, 350), 0.30, 'post-salary'),
    (['Salon Appointment', 'Barber Shop'], (1, 2), (150, 250), 0.15, 'even'),
]

# Fixed monthly commitments: (description, amount, day of month, jitter).
# Also scaled to the requested salary — rent is the largest single cost and
# leaving it fixed would swamp a smaller income on its own.
RECURRING = [
    ('Rent Payment Landlord', 3500.00, 1, 0.0),
    ('NETFLIX.COM Subscription', 189.00, 8, 0.0),
    ('DSTV Multichoice', 400.00, 12, 0.0),
    ('Spotify Premium', 79.00, 18, 0.0),
    ('Bayport Loan Repayment', 850.00, 5, 0.0),
]


def month_span(months, today=None):
    """Yield (year, month) for the last `months` complete months plus this one."""
    today = today or date.today()
    year, month = today.year, today.month
    span = []
    for _ in range(months):
        span.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(span))


def days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def split_total(total, parts):
    """Split a monthly total into `parts` transaction amounts.

    Uses random weights rather than equal shares so individual transactions
    still look natural while the monthly total stays where it was put.
    """
    weights = [random.uniform(0.6, 1.4) for _ in range(parts)]
    scale = total / sum(weights)
    return [weight * scale for weight in weights]


def build_rows(months, salary, salary_day, seed=42):
    random.seed(seed)
    rows = []
    today = date.today()

    # The baselines above describe someone earning SALARY_REFERENCE. Scale them
    # to the requested salary, otherwise --salary only changes the credit rows
    # and a K8,000 earner is generated spending a K12,000 earner's money — which
    # produces a persona 45% beyond their income and a nonsensical budget.
    scale = (salary / SALARY_REFERENCE) * PERSONA_SPEND_RATIO

    # One baseline per category for the whole run: this is the user's habit.
    baselines = [random.uniform(low, high) * scale
                 for _, _, (low, high), _, _ in SPENDING_PROFILE]

    # Generate one extra month at the front and discard it afterwards. Its
    # post-payday spending lands inside the requested window, so the first real
    # month is not short of the discretionary spending it should have inherited
    # from the previous payday.
    span = month_span(months + 1)
    window_start = date(span[1][0], span[1][1], 1)

    for year, month in span:
        last_day = days_in_month(year, month)

        pay_day = min(salary_day, last_day)
        pay_date = date(year, month, pay_day)
        if pay_date <= today:
            rows.append((pay_date, 'SALARY CREDIT EMPLOYER',
                         round(salary + random.uniform(-40, 40), 2), 'Credit'))

        for description, amount, day, jitter in RECURRING:
            when = date(year, month, min(day, last_day))
            if when > today:
                continue
            value = amount * scale * (1 + random.uniform(-jitter, jitter))
            rows.append((when, description, round(value, 2), 'Debit'))

        for index, (merchants, (low, high), _, volatility, bias) in \
                enumerate(SPENDING_PROFILE):
            # This month's total for the category: the habitual baseline, moved
            # by a bounded amount. Categories keep their character month to month.
            monthly_total = baselines[index] * (1 + random.gauss(0, volatility))
            monthly_total = max(0.0, monthly_total)
            count = random.randint(low, high)
            if count == 0 or monthly_total <= 0:
                continue

            for amount in split_total(monthly_total, count):
                if bias == 'post-salary':
                    # Deliberately allowed to fall into the next calendar month.
                    # Someone paid on the 30th does their post-payday spending in
                    # the first week of the following month. Clamping it back
                    # inside the pay month silently deleted almost all
                    # discretionary spending for late-paid users, leaving
                    # categories with two examples instead of thirty.
                    when = pay_date + timedelta(days=random.randint(0, 9))
                elif bias == 'fixed':
                    when = date(year, month, min(random.randint(3, 14), last_day))
                else:
                    when = date(year, month, random.randint(1, last_day))

                if when > today:
                    continue
                rows.append((when, random.choice(merchants),
                             round(amount, 2), 'Debit'))

    # Drop the lead-in month now that it has contributed its post-payday spill.
    rows = [row for row in rows if row[0] >= window_start]
    rows.sort(key=lambda row: row[0])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--months', type=int, default=8,
                        help='number of months of history to generate')
    parser.add_argument('--salary', type=float, default=12000.0)
    parser.add_argument('--salary-day', type=int, default=25)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', default='sample_data/sample_bank_statement.csv')
    args = parser.parse_args()

    rows = build_rows(args.months, args.salary, args.salary_day, args.seed)

    import os
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Date', 'Description', 'Amount', 'Type'])
        for when, description, amount, kind in rows:
            writer.writerow([when.isoformat(), description, f'{amount:.2f}', kind])

    debits = sum(1 for row in rows if row[3] == 'Debit')
    print(f'Wrote {len(rows)} rows ({debits} debits, {len(rows) - debits} credits) '
          f'across {args.months} months to {args.output}')


if __name__ == '__main__':
    main()
