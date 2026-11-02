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

## Demo extensions (beyond the graded requirements)

Alongside the specified system (FR-01–FR-12, NFR-01–NFR-08) the app carries a set
of **demo mockups** built on top of the same data and layers: an AI spending
assistant, one-click bank and mobile-money sync, shared household budgets, and an
email receipt auto-fetcher. These are proof-of-concept extensions, not delivered
requirements, and every one is clearly labelled *(Mock)* in the interface.

They are controlled by a single switch so the graded, specification-only build can
be run cleanly:

```bash
DEMO_FEATURES=0 python app.py
```

With `DEMO_FEATURES=0` every mock UI entry point disappears and its routes return
404; the default keeps them on for demonstrations. A throwaway database can also be
used for a walkthrough without touching real data by setting `EXPENSE_TRACKER_DB`
to another path.

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

The full requirements traceability matrix — every FR and NFR mapped to its
implementation, its verifying test, and the acceptance-criteria evidence — is in
[`docs/traceability.md`](docs/traceability.md). The summary below is the
functional side of it.

| ID | Requirement | Implementation |
|---|---|---|
| FR-01 | Transaction import | `services/ingestion.py` — column aliases, per-row validation, SHA-256 dedup |
| FR-02 | ML categorisation | `ml/categorization.py` — TF-IDF + Naive Bayes / Random Forest |
| FR-03 | Manual recategorisation | `app.py:recategorise`, dropdown on every row of the Transactions screen |
| FR-04 | Salary record | `/setup`; salary credits auto-detected on import |
| FR-05 | Behavioural Learning Phase | `services/behavioural.py:learning_status` |
| FR-06 | Expenditure forecasting | `ml/forecasting.py` — ARIMA / Linear Regression / mean / median / Croston, chosen per category by CV |
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

**Naive Bayes is the text-only baseline; Random Forest weighs more.** Per
section 3.7.2, the two classifiers do not share a pipeline. Naive Bayes uses
only the TF-IDF of the merchant description. Random Forest additionally uses the
normalised amount and the day-of-week / day-of-month, which carry the payday and
bill-cycle timing that can separate a fixed recurring charge from ad-hoc
spending. A `ColumnTransformer` gives each model the features it uses from one
shared frame. On clean data where merchant names are highly separable the two
tie, and model selection keeps the simpler baseline; the extra features earn
their place on noisier, real-world descriptions.

**A curated seed dataset bootstraps the cold start; the user's data takes
over.** Training data preparation (section 3.7.1) has two sources.
`data/seed_transactions.csv` is a hand-labelled set of ~80 diverse merchant
descriptions across all spending categories, preprocessed the same way as real
transactions (lowercase, punctuation stripped, whitespace collapsed via
`normalise`; ISO date standardisation and amount coercion in the ingestion
layer). The trainer includes it **only while the user's own labelled data is
too thin to train on alone** — so a usable model exists from the first upload,
and once the user has enough of their own transactions their patterns take over.
This matters for the reported F1: a mature user is measured on their own
transactions (the thing NFR-02 is about), not on the generic seed, whose many
small, single-example categories would otherwise drag the macro average down.

**The forecaster stops at the last complete month.** Including the month in
progress tells the model spending has collapsed and drags every forecast down.
Missing months are also filled with zeros rather than skipped, because a
category with no spending in March is absent from a `GROUP BY` result, not zero,
and a series with a hole in it fits the wrong lag structure.

**ARIMA is not used blindly, and it is compared against Linear Regression.**
Each category picks between ARIMA(1,0,0), Linear Regression, a 3-month mean, a
3-month median and Croston's intermittent-demand estimator by rolling-origin
cross-validation — training always precedes testing. ARIMA remains the
designated primary model and is kept whenever it is within 2% of the best
alternative. Linear Regression is the comparator the proposal names (section
3.7.3): it wins on categories with a steady trend, but on a clean autoregressive
series ARIMA still wins and on short, noisy series the robust mean/median do —
the measured error decides, and "Linear Regression was evaluated but rarely beat
ARIMA on this data" is itself a reportable result. Croston's method addresses
sporadic categories (spent some months, none others), where it estimates the
per-month spend rate rather than being dragged to zero by the empty months.

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
- **NFR-03** (forecast MAE ≤ 15% of actual monthly spend): hold-out on the most
  recent complete month, training on everything before it. The requirement is
  stated as a share of *monthly spend*, so the headline error is measured on the
  month's **total** forecast against the month's actual total — the quantity the
  user budgets a month against, where per-category over- and under-shoots net
  out. The stricter per-category error is also reported, as a diagnostic.

On synthetic and realistic mixed datasets the current implementation reports
macro F1 around 0.99 and a monthly-total forecast MAE of roughly 2–4% of actual
monthly spend — both comfortably inside their targets.

**The monthly total is accurate; individual sporadic categories are not, and
that is expected.** For a category spent only some months (school fees, a car
service, an occasional clothing splurge) the per-period error is irreducibly
high: no univariate model can predict *which* month an occasional lump lands, so
its forecast is always "wrong" against that specific month. This is an inherent
property of intermittent-demand forecasting, not a fault in the model — which is
exactly why Croston's method targets the average spend *rate* and why NFR-03 is
measured on the monthly total. Aggregated to the month, those per-category misses
cancel and the total forecast stays within a few percent of actual. The
per-category MAE ratio is surfaced alongside the headline figure so this
distinction is visible rather than hidden.

**The categorisation evaluation is reproducible in a notebook.**
`notebooks/categorisation_evaluation.ipynb` runs the full Naive Bayes vs Random
Forest bake-off end to end — generating synthetic data, cross-validating both
models, and showing per-category precision/recall/F1 and a confusion matrix
against the 0.85 target. The measurement logic lives in `ml/evaluation.py`
(imported by the notebook and unit-tested), so the numbers are not hand-typed.
The notebook needs the development tools:

```bash
pip install -r requirements-dev.txt
```

```bash
jupyter notebook notebooks/categorisation_evaluation.ipynb
```

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
- **Receipt scanning uses real OCR, with a manual fallback.** Scanning a receipt
  reads it with Tesseract (`requirements-ocr.txt`, plus the Tesseract binary and,
  for PDF receipts, poppler). No items are ever invented: if the OCR stack is
  missing or the image cannot be read, the scan reports a clear, actionable
  error; if it runs but finds no priced lines, it says so. Either way the user
  can type the line items in by hand (and can delete any line OCR misread). The
  extracted items are reconciled against the receipt's printed total and the
  transaction amount, and a mismatch is flagged. On Windows the binaries install
  with `winget install UB-Mannheim.TesseractOCR` and
  `winget install oschwartz10612.Poppler`, and are found on PATH or in their
  standard install locations; otherwise set `TESSERACT_CMD` / `POPPLER_PATH`.

---

## Project layout

```
app.py                    Flask routes (Presentation Layer)
config.py                 All tunable constants and paths
database.py               Connection, schema init, migrations
schema.sql                Table definitions and seed categories
data/                     Curated labelled seed dataset (training data prep)
security.py               Password, session timeout, secret key
generate_sample_data.py   Synthetic bank statement generator
ml/
  categorization.py       TF-IDF + Naive Bayes / Random Forest (FR-02)
  evaluation.py           Cross-validated NB vs RF comparison (section 3.7.4)
  forecasting.py          ARIMA/LinReg/mean/median/Croston forecasting (FR-06)
  subscriptions.py        Recurring charge detection (FR-08)
services/
  ingestion.py            CSV validation, parsing, dedup (FR-01)
  behavioural.py          Learning phase, salary-cycle analysis (FR-05)
  recommendation.py       Budgets, nudges, savings, reports (FR-07/09/11/12)
  insights.py             Smart Suggestions engine (dashboard)
  ocr.py                  Tesseract OCR for the scan-receipt feature
templates/                Jinja2 templates, one per screen
static/                   Stylesheet and chart rendering
notebooks/                Reproducible model-evaluation notebook
tests/                    unit, route and integration tests
```
