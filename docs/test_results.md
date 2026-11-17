# System Test Results

**Salary-Linked Expense Tracker and Budget Advisor**
Student: Mapesho Nkwashi (BIT23120545) · Supervisor: Mr J. Mwanza
Measurements captured **2026-11-10**. All figures below are measured on the test
machine described in §1 — none are estimated.

This document records the machine-level verification of the acceptance criteria
(NFR-01 performance, NFR-02 categorisation accuracy, NFR-03 forecast accuracy)
and the cross-browser / responsive checks. It is the evidence base for the
testing section of Chapter 8 and complements the user-acceptance results in
[`uat_results.xlsx`](uat_results.xlsx).

---

## 8.1 Introduction

This chapter describes how the Smart Salary-Linked Expense Tracking and Predictive
Budgeting System was tested and evaluated. It sets out the testing approach, the
types of test carried out, the test cases together with their expected and actual
outcomes, the errors (defects) identified and how they were resolved, and a
critical evaluation of the system against the objectives defined in Chapter 1.

A layered testing strategy was adopted, matching the system's five-layer
architecture so that each layer could be verified independently. Four
complementary types of testing were applied:

- **Automated testing** — a suite of 110 unit, integration and end-to-end tests
  exercising the data, machine-learning, service and presentation layers,
  providing repeatable regression evidence.
- **Machine-learning verification** — quantitative measurement of the two learning
  components against their acceptance criteria: transaction-categorisation accuracy
  (NFR-02, macro-F1 ≥ 0.85) and expenditure-forecast error (NFR-03, MAE ≤ 15%).
- **Non-functional / performance testing** — measurement of page-load and
  task-processing times against the three-second responsiveness target (NFR-01),
  together with cross-browser and responsive (mobile) checks.
- **User Acceptance Testing (UAT)** — a structured session with seven participants,
  using the System Usability Scale (SUS) and a feature/value questionnaire to
  evaluate usability and real-world fit.

The test cases, their expected results and the actual measured results are
presented in the sections that follow; the defects uncovered and their resolutions
are tabulated in §8; and the chapter closes with a critical evaluation of the
system against the project objectives, drawing the automated, performance and
user-acceptance evidence together.

---

## 1. Test environment

| Item | Detail |
|------|--------|
| Machine | Intel® Core™ i7-8650U @ 1.90 GHz — 4 physical / 8 logical cores |
| Memory | 31.9 GB RAM |
| Operating system | Windows 11 Pro, build 10.0.26300 (64-bit) |
| Runtime | Python virtual-env; Flask on the Werkzeug development server |
| Network binding | `127.0.0.1` only — nothing transmitted off-device (NFR-05) |
| Test dataset | `uat_demo.db` — 227 transactions across 7 complete months (Apr–Oct 2026) plus a partial November; declared salary ZMW 12,000, payday day 25 |
| Model artefact | `uat_sessions/P02/categoriser.joblib` |

### Browsers tested

| Browser | Version | Engine |
|---------|---------|--------|
| Google Chrome / Chromium | 152.0.7977.167 | Blink (measurement engine) |
| Microsoft Edge | 157.0.4341.42 | Blink (Chromium) |

Firefox was excluded by scope. Edge shares Chrome's Blink engine and hits the
same local server, so its load times are expected to match Chrome within
measurement noise; a one-paste console snippet to capture exact Edge figures is
in §6.

---

## 2. NFR-02 — Transaction categorisation accuracy

**Target:** macro-averaged F1 ≥ 0.85.
**Method:** 5-fold stratified cross-validation (`ml/evaluation.py`) over the
user's own labelled transactions — 266 labelled rows across 12 categories.

| Model | Macro-F1 | Accuracy |
|-------|:--------:|:--------:|
| **MultinomialNB** (selected) | **0.994** | 0.996 |
| RandomForest | 0.994 | 0.996 |

**Per-category F1 (selected model):**

| Category | F1 | Category | F1 |
|----------|:--:|----------|:--:|
| Airtime & Data | 1.000 | Loan & Debt | 1.000 |
| Dining Out | 1.000 | Personal Care | 1.000 |
| Entertainment | 1.000 | **Shopping** | **0.933** |
| Groceries | 1.000 | Subscriptions | 1.000 |
| Healthcare | 1.000 | Transport | 0.991 |
| Housing | 1.000 | Utilities | 1.000 |

**Verdict: PASS** (0.994 ≥ 0.85), with the lowest single category at 0.933.

> **Honesty note for the writeup.** The corpus is synthetic/seeded and merchant
> strings recur across months, so cross-validation often sees a similar merchant
> in both train and test folds. The 0.994 should therefore be read as an *upper
> bound*; the defensible claim is that the architecture clears the 0.85 bar with
> a wide margin, not that real-world accuracy would be 99%.

---

## 3. NFR-03 — Expenditure forecast accuracy

**Target:** forecast error (MAE) within 15% of actual monthly spend.
**Method:** rolling-origin hold-out (`ml/forecasting.py:backtest`) — train on
months 1…N-1, test on month N, preserving temporal order so no future month
leaks into training. The headline error is on the **month's total** (the level
the requirement names); the stricter per-category error is kept as a diagnostic.

| Dataset | Months | **Monthly-total MAE** | Per-category MAE (diagnostic) | Verdict |
|---------|:------:|:---------------------:|:-----------------------------:|:-------:|
| `uat_demo.db` | 7 | **1.13 %** | 10.78 % | PASS |
| P02 session | 7 | **7.04 %** | 9.71 % | PASS |

**Verdict: PASS** — both datasets are well within the 15% target on the
monthly total, and the stricter per-category figure also sits below 15%.

> The development database `expense_tracker.db` was **excluded** from this
> measurement: it is inconsistent scratch data (one month totals ZMW 6,390
> against ~15,000 elsewhere), which produces a meaningless error ratio. The UAT
> datasets above are the realistic salary-cycle data the system was demonstrated
> and evaluated on.

---

## 4. NFR-01 — Page load performance

**Target:** core pages load and render in ≤ 3 s under normal conditions.
**Method:** browser Navigation Timing API (`responseStart` for server time,
`loadEventEnd` for full load), plus a direct server-response probe over an
authenticated session; medians of 6 consecutive loads. Local server, no network
latency, so server time ≈ compute time.

### 4.1 Baseline — before optimisation

The dashboard, budget and savings pages each recomputed the full forecast
pipeline (ARIMA/Croston model selection across 12 categories) on **every**
request, via `recommendation.build_budget()`. Pages without a forecast were
already fast.

| Page | Server response (median of 6) |
|------|:-----------------------------:|
| Dashboard `/` | **3,735 ms** |
| Budget `/budget` | **3,935 ms** |
| Savings `/savings` | **3,735 ms** |
| Transactions `/transactions` | 125 ms |
| Reports `/reports` | ~125 ms |
| Settings `/settings` | ~400 ms |

The three forecast-bearing pages **breached** the 3 s target (~3.7–3.9 s).

### 4.2 Optimisation applied

1. **Forecast memoisation** (`ml/forecasting.py`). `generate_forecasts()` is now
   cached per user against an MD5 signature of exactly the inputs a forecast
   depends on — every forecastable (DEBIT, non-salary) transaction row plus the
   current month. Any add, delete, amount, date or category change — and the
   month rollover that moves the in-progress-month cut-off — changes the
   signature, so a stale forecast can never be served. The cache is also cleared
   explicitly after a retrain/correction (`reset_forecast_cache()`).
2. **Startup warming** (`app.py`). A daemon thread pre-computes the forecasts at
   server start, moving the one-off cost (model fitting plus the first import of
   the scientific stack) off the request path, so even the first page a user
   opens is served from cache.

### 4.3 Result — after optimisation

Server-response medians (authenticated probe, uncontended):

| Page | Before | **After (warm)** | Change |
|------|:------:|:----------------:|:------:|
| Dashboard `/` | 3,735 ms | **73 ms** | ~51× faster |
| Budget `/budget` | 3,935 ms | **38 ms** | ~104× faster |
| Savings `/savings` | 3,735 ms | **19 ms** | ~197× faster |
| Transactions | 125 ms | 136 ms | unchanged |

Browser end-to-end load (Chrome 152, includes rendering):

| Scenario | Dashboard | Budget | Savings |
|----------|:---------:|:------:|:-------:|
| First load after a fresh server start (post warm-thread) | **714 ms** | 345 ms | 212 ms |
| Steady-state (warm, repeated views) | 120–295 ms | 270 ms | 172 ms |

For reference, if the forecast were forced onto the request path with a cold
cache, a single load measures ~5.8 s on the very first request of a new process
(dominated by the one-time scientific-library import) and ~1.5 s for a recompute
after a data change once those libraries are warm. Startup warming removes this
from the user-facing path entirely.

**Verdict: PASS** — every core page now loads in **under 1 second**, first view
and repeat views alike, comfortably within the 3 s target. The automated
regression test `tests/test_routes.py::UATAndPerformanceTests::`
`test_nfr01_core_pages_render_within_three_seconds` also passes.

### 4.4 Task processing times

Beyond page load, the system-side processing a user waits on during the core
tasks (service layer, a 245-transaction statement, same machine):

| Task | Processing time |
|------|:---------------:|
| Import a bank statement — parse + auto-categorise 245 transactions | **~65 ms** |
| Post-import pipeline — model training + forecast + subscription detection | **~2.5 s** |
| Total first upload (file chosen → dashboard ready) | **~2.5 s** |
| Category correction → model retrain ("learns instantly") | **~2.5 s** |
| Open a core page (steady state) | < 0.3 s (§4.3) |

These are **system processing** times, not human time-on-task.

**Per-tester session duration was not instrumented.** The questionnaire captures
only a submission timestamp, not per-participant start/stop times, so individual
durations cannot be derived from the collected data. The only wall-clock evidence
available is the submission window: the group session on **10 November 2026
spanned ~2 h 31 min** (first submission 12:28, last 14:59) across six testers,
with P01 completing the previous evening. If per-tester start/end times were noted
by the facilitator during the sessions, they can be tabulated here on request.
NFR-04's "complete upload-to-dashboard within 5 minutes" is therefore evidenced by
the guided-flow walkthrough (`run_uat_session.py`) rather than a timing log. The
~2.5 s model-retrain is the one noticeable system wait (§8, item D2).

---

## 5. Responsive / narrow-width check

Measured in Chrome with a 375 × 812 mobile viewport.

| Check | Result |
|-------|--------|
| Server/compute time vs desktop | Identical — load is compute-bound, not layout-bound, so width does not affect it |
| Horizontal scroll at 375 px | **None** (`scrollWidth ≤ innerWidth`) on dashboard and transactions |
| Layout | Navigation wraps, metric cards and the "Smart Suggestions" panel stack cleanly; verified visually |

The PWA installs and renders at phone width; see [`docs/mobile-install.md`](mobile-install.md).

---

## 6. Cross-browser matrix

### Decision

- **Chrome / Chromium 152** — the primary target; fully tested here at desktop
  and 375 px narrow width.
- **Edge 157** — **included**. It runs the identical Blink engine against the
  same local server, so page-load behaviour matches Chrome within measurement
  noise; exact figures are captured with the snippet below. Edge cannot be driven
  by the automated harness used here, so its result is engine-parity plus a
  reproducible manual check rather than a separate automated run.
- **Firefox (Gecko)** — **out of scope**, by decision. The cross-browser check is
  deliberately limited to the two Chromium browsers the UAT participants actually
  used (the "Device & browser" column of the responses lists Chrome, Brave and
  phone browsers — all Chromium). No Gecko-specific behaviour is claimed either
  way.

| Browser | Desktop | Narrow (375 px) | Source |
|---------|:-------:|:---------------:|--------|
| Chrome / Chromium 152 | PASS — loads < 1 s, no layout issues | PASS — no horizontal scroll | Measured (this document) |
| Edge 157 | Expected parity (same Blink engine + same local server) | Expected parity | Capture with the snippet below |

To record exact **Edge** figures: sign in at `http://127.0.0.1:5055/`, open
DevTools → Console (F12), and paste:

```js
(async()=>{const t=[];for(let i=0;i<5;i++){const s=performance.now();await fetch('/',{cache:'no-store'});t.push(performance.now()-s);}t.sort((a,b)=>a-b);console.log('Edge dashboard server-response ms — median:',Math.round(t[2]),'| all:',t.map(x=>Math.round(x)));})()
```

For a full desktop/narrow page-load figure (resize the window, hard-reload, then
run):

```js
(()=>{const n=performance.getEntriesByType('navigation')[0];return {width:innerWidth,ttfb_ms:Math.round(n.responseStart),load_ms:Math.round(n.loadEventEnd)};})()
```

---

## 7. Automated regression suite

Run with `python -m unittest` in the project virtual-env.

| Suite | Tests | Result |
|-------|:-----:|:------:|
| `tests/test_ml.py` | 43 | PASS |
| `tests/test_routes.py` | 58 | PASS — incl. `test_nfr01_core_pages_render_within_three_seconds` |
| `tests/test_end_to_end.py` | 9 | PASS — incl. `test_categorisation_meets_the_nfr02_target` and `test_forecast_error_meets_the_nfr03_target` |

All 110 tests pass after the caching change (`python -m unittest`, exit 0).

---

## 8. Defects and observations

No functional defects were found — all 110 automated tests pass and the three
acceptance criteria are met. The items below are usability observations from the
UAT (chiefly from participant **P04**, the most critical tester) plus one
data-quality note, recorded for the Chapter 8 discussion and the Chapter 9
recommendations. None are blocking.

| # | Source | Observation | Investigated cause | Severity | Resolution |
|---|--------|-------------|--------------------|:--------:|------------|
| D1 | P04 | "80% budget-nudge alerts" marked *did not work as expected* | **Not a functional defect.** The alert was verified to fire on the demo data — "Housing at 95% of budget" (within the ≥80% warning band) — and the automated test for it passes. The alert is data-conditional and self-labels with the live percentage under a generic "Alerts" panel below the budget charts, so it was most likely not recognised as *the 80% nudge*, or sat below the fold on a laptop. | Minor (discoverability) | Recommend: label the alert against the 80% threshold and raise the Alerts panel above the charts. *(recommendation — Ch 9)* |
| D2 | P04 | Category correction "learns instantly" marked *Partly* | The correction **does** retrain immediately, but the retrain takes ~2.5 s (§4.4) with no on-screen progress indicator, so the "instant" learning is not visibly confirmed. | Minor (feedback) | Recommend: show a "learning…" indicator and an explicit confirmation on submit. *(recommendation — Ch 9)* |
| D3 | P04 | Savings goals marked *Partly* | Functionally correct — target, monthly-required amount and on-track status all compute and render; the prominence of the on-track status is the likely cause of partial confidence. | Cosmetic | Recommend: make the on-track status more prominent. *(recommendation — Ch 9)* |
| D4 | UAT data | P06 and P07 selected 5 for every SUS item, including the negatively-worded ones | Acquiescence / straight-lining (careless responding) | N/A (data) | Reported in [`uat_results.xlsx`](uat_results.xlsx) with a sensitivity analysis: mean SUS 75.4 (all) vs 85.5 (excluding the two). |

By decision, D1–D3 are recorded as Chapter 9 recommendations and were not actioned
in code for this evaluation; the underlying features are verified working (§2, §4
and the automated suite), so these are usability refinements, not fixes to broken
behaviour.

---

## 9. Summary

| Requirement | Target | Measured | Verdict |
|-------------|--------|----------|:-------:|
| NFR-01 Performance | Core pages ≤ 3 s | < 1 s (all pages, first and repeat views) | **PASS** |
| NFR-02 Categorisation | Macro-F1 ≥ 0.85 | 0.994 (lowest category 0.933) | **PASS** |
| NFR-03 Forecasting | Monthly MAE ≤ 15 % | 1.13 % and 7.04 % on two datasets | **PASS** |

### Code changes made for NFR-01

- `ml/forecasting.py` — added `_FORECAST_CACHE`, `_forecast_signature()` and
  `reset_forecast_cache()`; `generate_forecasts()` is now memoised and
  signature-invalidated.
- `app.py` — `reset_forecast_cache()` is called after every retrain/correction;
  a daemon thread warms the forecast cache at startup.

### Known limitation / recommendation

Measurements were taken on the Werkzeug **development** server. For a production
deployment a WSGI server (e.g. waitress or gunicorn) is recommended; combined
with the caching above, this would preserve sub-second loads under concurrent
use. This is noted as future work rather than a defect, since the system is
specified (NFR-05) as a single-user, locally-hosted application.
