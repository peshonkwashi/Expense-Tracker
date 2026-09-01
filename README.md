# Salary-Linked Expense Tracker and Budget Advisor

Implementation of the system specified in *Project Proposal (Mapesho Nkwashi,
BIT23120545)* — a locally hosted Flask application that imports bank statements,
categorises transactions with machine learning, learns a user's spending across
the salary cycle, and generates explainable budget recommendations.

All data stays on the machine it runs on. Nothing is transmitted anywhere.

---

## Running it

```bash
venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. On first launch you set a name, salary,
pay day and a password; the database and all tables are created automatically.

To try it with data straight away, generate a synthetic statement and upload it
from the Transactions screen:

```bash
python generate_sample_data.py --months 8 --salary 12000 --salary-day 25
```

Pass your own `--salary` and `--salary-day`. Spending is scaled to the salary,
so the generated persona always lives at roughly 93% of income rather than
spending a fixed amount regardless of what they earn.

Run the tests with:

```bash
python -m unittest discover -s tests -t .
```

---

## How the layers fit together

The five architectural layers from section 3.6 map onto directories:

| Layer | Location | Responsibility |
|---|---|---|
| Data Acquisition | `services/ingestion.py` | Validate, parse, deduplicate and store CSV statements |
| Data Storage | `database.py`, `schema.sql` | SQLite connection, schema, forward-only migrations |
| ML Processing | `ml/` | Categorisation, forecasting, subscription detection |
| Recommendation & Insights | `services/recommendation.py`, `services/behavioural.py` | Budgets, nudges, savings, salary-cycle analysis |
| Presentation | `app.py`, `templates/`, `static/` | Flask routes, Jinja2 templates, Chart.js |

Each layer only calls the one below it, so any of them can be replaced without
touching the others (NFR-07). Every tunable threshold — the 80% nudge trigger,
the 0.85 F1 target, the 15% MAE ceiling — is defined once in `config.py`.

---

## Where the requirements are implemented

| ID | Requirement | Implementation |
|---|---|---|
| FR-01 | Transaction import | `services/ingestion.py` — column aliases, per-row validation, SHA-256 dedup |
| FR-02 | ML categorisation | `ml/categorization.py` — TF-IDF + Naive Bayes / Random Forest |
| FR-03 | Manual recategorisation | `app.py:recategorise`, dropdown on every row of the Transactions screen |
| FR-04 | Salary record | `/setup`; salary credits auto-detected on import |
| FR-05 | Behavioural Learning Phase | `services/behavioural.py:learning_status` |
| FR-06 | Expenditure forecasting | `ml/forecasting.py` — ARIMA with method selection |
| FR-07 | Budget recommendations | `services/recommendation.py:build_budget` |
| FR-08 | Subscription detection | `ml/subscriptions.py` |
| FR-09 | Nudge alerts at 80% | `services/recommendation.py:generate_nudges` |
| FR-10 | Dashboard | `templates/dashboard.html` |
| FR-11 | Savings goal tracker | `templates/savings.html`, `recommendation.savings_position` |
| FR-12 | Monthly report | `templates/reports.html`, `recommendation.monthly_report` |

Security measures from section 5.9 live in `security.py`: PBKDF2 password hash,
10-minute idle timeout, a generated secret key kept out of source, `secure_filename`
on uploads, a 5MB upload cap, and parameterised queries throughout.

---

## Design decisions worth knowing about

**The rule seed exists to break a deadlock.** A supervised classifier cannot
train without labelled data, and no data is labelled until something classifies
it. `RULE_SEED` in `ml/categorization.py` assigns the first labels by keyword.
From then on the model trains on those labels plus every user correction, and
`category_source` on each transaction records which of the three assigned it.

**The forecaster stops at the last complete month.** Including the month in
progress tells the model spending has collapsed and drags every forecast down.
Missing months are also filled with zeros rather than skipped, because a
category with no spending in March is absent from a `GROUP BY` result, not zero,
and a series with a hole in it fits the wrong lag structure.

**ARIMA is not used blindly.** Each category picks between ARIMA(1,0,0), a
3-month mean and a 3-month median by rolling-origin cross-validation — training
always precedes testing. ARIMA remains the designated primary model and is kept
whenever it is within 2% of the best alternative. On short, noisy series a
7-point autoregression frequently overfits, and the measured error says so.

**Essentials are never budgeted away.** If forecast essential spending exceeds
the salary, the system reports the deficit rather than scaling rent down to fit.
Only discretionary categories are trimmed, and proportionally.

**The salary-cycle chart averages, it does not accumulate.** Per-week figures
are the average across completed pay cycles, so they can be read straight
against the salary. Summing the whole history — which the first version did —
put a K51,000 bar next to a K8,000 salary and meant nothing. The cycle in
progress is excluded for the same reason the forecaster stops at the last
complete month, and days 28+ fold into week 4 rather than forming a two-day
fifth bucket that reads as a spending collapse.

**Savings are allocated by waterfall.** The pool is cumulative income minus
expenditure, assigned to goals in due-date order. Showing the whole pool against
every goal — as the first version did — makes two goals look complete when only
one can be.

---

## Model evaluation

Both acceptance criteria are checked automatically, and the results are shown on
the Settings screen with a pass/fail badge against the target.

- **NFR-02** (categorisation F1 ≥ 0.85): stratified 5-fold cross-validation,
  macro F1, recorded to `ModelMetrics` on every training run.
- **NFR-03** (forecast MAE ≤ 15% of actual): hold-out on the most recent
  complete month, training on everything before it.

On an 8-month synthetic dataset (K8,000 salary, pay day 30) the current
implementation reports macro F1 of 0.99 and a forecast MAE of 11.4% of mean
actual spend — both inside their targets.

Macro F1 is sensitive to small classes: it averages per-category scores
regardless of support, so a category with two examples counts as much as one
with fifty. An earlier synthetic dataset scored 0.83 macro F1 at 98% accuracy
purely because three categories had two examples each. If the score looks low,
check the per-category support before concluding the model is weak.

One caveat worth stating in the report: while training labels come from the rule
seed, a high F1 partly measures the model's agreement with those keyword rules
rather than with ground truth. The score becomes a genuine measure of accuracy
as user corrections accumulate, since corrections are recorded as `user`-sourced
labels and weighted more heavily during training.

---

## Known limitations

- **Chart.js loads from a CDN.** The dashboard degrades to accessible CSS bar
  charts when it cannot load, so the app is usable offline, but for a fully
  offline install download `chart.umd.js` into `static/js/` and point the script
  tag in `templates/base.html` at it.
- **Single user.** The schema carries `user_id` throughout and every query
  filters on it, but the UI assumes one profile per database file.
- **Password recovery does not exist.** By design — there is no server to
  recover from. The Settings screen offers a database backup instead.

---

## Project layout

```
app.py                    Flask routes (Presentation Layer)
config.py                 All tunable constants and paths
database.py               Connection, schema init, migrations
schema.sql                Table definitions and seed categories
security.py               Password, session timeout, secret key
generate_sample_data.py   Synthetic bank statement generator
ml/
  categorization.py       TF-IDF + Naive Bayes / Random Forest (FR-02)
  forecasting.py          ARIMA forecasting and evaluation (FR-06)
  subscriptions.py        Recurring charge detection (FR-08)
services/
  ingestion.py            CSV validation, parsing, dedup (FR-01)
  behavioural.py          Learning phase, salary-cycle analysis (FR-05)
  recommendation.py       Budgets, nudges, savings, reports (FR-07/09/11/12)
templates/                Jinja2 templates, one per screen
static/                   Stylesheet and chart rendering
tests/                    111 unit, route and integration tests
```
