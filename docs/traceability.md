# Requirements Traceability Matrix

Salary-Linked Expense Tracker and Budget Advisor — Mapesho Nkwashi (BIT23120545).

This document traces every numbered requirement in the project proposal to the
code that implements it and the test that verifies it, and records the status of
the two hard acceptance criteria. It is the evidence that the specified system
(FR-01–FR-12, NFR-01–NFR-08) has been delivered.

Run the full test suite that backs this matrix with:

```bash
venv\Scripts\python.exe -m unittest discover -s tests -t .
```

> **Scope note.** The numbered requirements below are the *specified* system. The
> AI assistant, bank / mobile-money sync, shared household budgets and email
> receipt auto-fetcher are **demo extensions beyond the proposal** — see the last
> section. They are labelled *(Mock / demo)* in the UI and can be switched off
> entirely (`DEMO_FEATURES=0`) to run the specification-only build.

---

## Functional requirements (FR-01 – FR-12)

| ID | Requirement | Implementation | Verified by |
|----|-------------|----------------|-------------|
| FR-01 | Import bank statements (CSV) | `services/ingestion.py` — column aliasing, per-row validation, SHA-256 deduplication | `tests/test_ingestion.py` |
| FR-02 | Categorise transactions with ML | `ml/categorization.py` — TF-IDF + Naive Bayes / Random Forest, rule-seed cold start | `tests/test_ml.py`, `ml/evaluation.py`, `tests/test_end_to_end.py` |
| FR-03 | Manual recategorisation | `app.py:recategorise` (dropdown per row; stored as a `user` label and retrained on) | `tests/test_routes.py` (recategorisation tests) |
| FR-04 | Record salary / pay cycle | `/setup`; salary credits auto-detected on import (amount + pay-day window) | `tests/test_ingestion.py` (salary detection) |
| FR-05 | Behavioural Learning Phase | `services/behavioural.py:learning_status` (+ `cycle_profile` salary-cycle analysis) | `tests/test_services.py` (LearningPhase, CycleProfile) |
| FR-06 | Expenditure forecasting | `ml/forecasting.py` — ARIMA / Linear Regression / mean / median / Croston, chosen per category by rolling-origin CV | `tests/test_ml.py` (ForecastingTests) |
| FR-07 | Budget recommendations | `services/recommendation.py:build_budget` (needs-based hierarchy, dynamic savings re-projection) | `tests/test_services.py` (budget tests) |
| FR-08 | Subscription detection | `ml/subscriptions.py` — recurring charges by amount + monthly interval | `tests/test_ml.py` (subscription tests) |
| FR-09 | Nudge alerts at 80% of budget | `services/recommendation.py:generate_nudges` (loss-framed; + proactive savings-goal alert) | `tests/test_services.py` (NudgeTests) |
| FR-10 | Dashboard | `app.py:dashboard`, `templates/dashboard.html` | `tests/test_routes.py` (page render / dashboard) |
| FR-11 | Savings goal tracker | `services/recommendation.py:savings_position` + `goal_requirements`; `templates/savings.html` | `tests/test_services.py` (SavingsTests), `tests/test_routes.py` |
| FR-12 | Monthly report | `services/recommendation.py:monthly_report`; `templates/reports.html` | `tests/test_services.py` (MonthlyReportTests), `tests/test_routes.py` |

---

## Non-functional requirements (NFR-01 – NFR-08)

Requirement text and acceptance criteria below are taken verbatim (paraphrased
for length) from the proposal's Table 5.4.

| ID | Attribute | Requirement / acceptance criterion (proposal Table 5.4) | Implementation | Verified by / evidence |
|----|-----------|---------------------------------------------------------|----------------|------------------------|
| NFR-01 | Performance | Dashboard loads and renders all components in acceptable time — **page load ≤ 3 s** under normal conditions. | Local Flask app, parameterised/indexed queries, Chart.js with a CSS-bar fallback so a page never blocks on the CDN. | `tests/test_routes.py:UATAndPerformanceTests` (`test_nfr01_core_pages_render_within_three_seconds`) — **PASS**, plus live timing log in [`docs/uat_plan_and_sheet.md`](uat_plan_and_sheet.md). |
| NFR-02 | ML Accuracy | Categorisation model **F1 ≥ 0.85** on the held-out test set. | `ml/categorization.py` (stratified 5-fold CV), `ml/evaluation.py`; recorded to `ModelMetrics` each training run. | `tests/test_end_to_end.py`, `tests/test_ml.py`. **Measured ≈ 0.97–0.99 — PASS.** |
| NFR-03 | ML Accuracy | Forecast **MAE ≤ 15% of actual monthly _category_ spending** (per category). | `ml/forecasting.py:backtest` reports **both** the per-category MAE ratio *and* the monthly-total ratio. | `tests/test_ml.py`, `tests/test_end_to_end.py`. On the 8-month synthetic statement: **per-category ≈ 8–11% — PASS** (monthly-total ≈ 5–8%). See note on sporadic real data. |
| NFR-04 | Usability | Usable without prior technical training — a first-time user completes CSV upload and views their dashboard **within 5 minutes** unassisted. | Guided `/setup` → upload → dashboard flow; plain-language nudges and every recommendation carries an explanation; no jargon. | Structured UAT walkthrough (`Scenario 1` stopwatch protocol in [`docs/uat_plan_and_sheet.md`](uat_plan_and_sheet.md), launched via [`run_uat_session.py`](../run_uat_session.py)). |
| NFR-05 | Security | Financial data protected from unauthorised access — all data stored locally in SQLite, nothing transmitted to external servers, parameterised queries prevent SQL injection. | Single SQLite file bound to `127.0.0.1`; PBKDF2 password hash; parameterised queries throughout (`config.py`, `security.py`, `services/ingestion.py`). | `tests/test_ingestion.py` (SQL-injection stored literally), `tests/test_routes.py` (password never plain text) — **PASS.** |
| NFR-06 | Reliability | Data integrity maintained during all storage operations — **all DB writes atomic; no partial transaction records committed.** | `services/ingestion.py:import_statement` and `reset_transactions.py` wrap the work in one DB transaction — all or nothing. | `tests/test_ingestion.py` (a rejected row leaves no partial import) — **PASS.** |
| NFR-07 | Maintainability | Modules updatable without affecting other layers — layered architecture, each layer exposing a defined interface to adjacent layers only. | Five layers map to directories (`services/ingestion`, `database`, `ml/`, `services/recommendation`, `app.py`/`templates/`); every tunable threshold centralised in `config.py`. | Design — **PASS.** |
| NFR-08 | Compatibility | Dashboard accessible from standard modern browsers — **renders correctly on Chrome, Firefox and Edge, desktop and mobile widths.** | Standard HTML + responsive CSS (auto-fit grids, card-tables that reflow at phone width, mobile viewport meta) + installable Progressive Web App (`static/manifest.json`, `/sw.js`, `/offline`, `static/js/pwa.js`). | `tests/test_routes.py:PWATests` — **PASS**, plus cross-browser & mobile PWA verification matrix in [`docs/uat_plan_and_sheet.md`](uat_plan_and_sheet.md). |

> **NFR-03 — note on the per-category criterion.** The proposal states NFR-03 per
> **category** ("15% of the actual monthly category spending"). On the standard
> 8-month synthetic statement the per-category MAE is **≈ 8–11% across seeds —
> within the 15% target.** The implementation currently gates its displayed
> pass/fail on the *monthly-total* ratio (≈ 5–8%); since the per-category figure
> also passes on this data, **gating on `per_category_mae_ratio` instead would
> match the proposal's exact wording and still pass** — a one-line change worth
> making so the headline metric matches the spec verbatim. Caveat: per-period
> error on genuinely sporadic categories (school fees, a car service billed a few
> times a year) is irreducibly high — on a sporadic-heavy real month it has been
> observed around 17–20%. That is an inherent property of intermittent-demand
> forecasting; record it as an honest limitation, and prefer evaluating on a
> representative statement.

---

## Acceptance criteria

The proposal fixes two hard, measurable thresholds. Both are checked
automatically and shown on the Settings screen with a pass/fail badge.

| Criterion | Target | Measured | Status |
|-----------|--------|----------|--------|
| Categorisation macro F1 (NFR-02) | ≥ 0.85 | ≈ 0.97–0.99 (5-fold CV) | **PASS** |
| Forecast MAE, per category — *the proposal's stated criterion* (NFR-03) | ≤ 15% | ≈ 8–11% (8-month synthetic) | **PASS** |
| Forecast MAE, monthly total (NFR-03, aggregate view) | ≤ 15% | ≈ 5–8% | **PASS** |

Both are reproducible: the categorisation bake-off runs end to end in
`notebooks/categorisation_evaluation.ipynb` (logic in `ml/evaluation.py`), and
the forecast hold-out in `ml/forecasting.py:backtest`, which reports the
per-category and monthly-total figures side by side. NFR-03 passes at both levels
on the synthetic evaluation data; the per-category figure can rise above 15% on a
sporadic-heavy real month (see the NFR-03 note above).

---

## Beyond-spec demo extensions (not graded requirements)

These were built on top of the delivered system as proof-of-concept extensions.
They are **not** part of FR-01–FR-12 and are all gated by `config.DEMO_FEATURES`
(default on; `DEMO_FEATURES=0` hides every one and 404s their routes, leaving the
specification-only build).

| Extension | Implementation | Verified by |
|-----------|----------------|-------------|
| AI spending assistant (keyword logic-tree over the user's own data) | `app.py:ask_assistant` | `tests/test_routes.py` (NewFeatureTests) |
| Bank sync (synthetic, idempotent, retrains on new data) | `app.py:sync_bank` | `tests/test_routes.py` |
| Mobile-money sync | `app.py:sync_momo` | `tests/test_routes.py` |
| Shared household budgets (invite / settle / leave) | `app.py:household`, `leave_household` | `tests/test_routes.py` |
| Email receipt auto-fetcher (real IMAP) | `app.py:auto_fetch_receipts`, `services/imap_fetcher.py` | `tests/test_imap.py`, `tests/test_routes.py` |

**Receipt scanning** (`services/ocr.py`) is a real feature rather than a demo
mock: an uploaded receipt is read with Tesseract OCR (poppler for PDFs); no items
are ever invented — a failed scan reports an actionable error and the user can
enter the line items by hand. It is not one of the numbered requirements.

**Caveat for the defence.** The email auto-fetcher reaches out to an IMAP mailbox,
which is outbound network access and therefore sits outside the NFR-05
"everything stays local" guarantee. It is opt-in (needs `IMAP_EMAIL` /
`IMAP_PASSWORD`), off in the default/graded build, and should be presented as an
optional integration rather than part of the specified, local-only system.
