# System Test Results

**Salary-Linked Expense Tracker and Budget Advisor**
Student: Mapesho Nkwashi (BIT23120545) · Supervisor: Mr J. Mwanza
Measurements captured **2026-11-10**; defect fixes and the final regression and
functional runs **2026-11-17/18**; real-device sessions **2026-11-18/19**. All figures below are measured on the test machine
described in §1 — none are estimated.

This document records the machine-level verification of the acceptance criteria
(NFR-01 performance, NFR-02 categorisation accuracy, NFR-03 forecast accuracy),
the cross-browser / responsive checks, and the defects found and resolved. It is
the evidence base for the testing section of Chapter 8 and complements the
user-acceptance results workbook (`docs/uat_results.xlsx`, kept locally because
it contains participant responses).

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

- **Automated testing** — a suite of 239 unit, integration and end-to-end tests
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
NFR-04's "complete upload-to-dashboard within 5 minutes" was therefore measured
separately, with a stopwatch, on real phones (§5.1). A first-time user, unassisted,
took **1 min 39 s** from the first-launch setup screen to the populated dashboard on
an iPhone 13. An expert baseline on a Samsung Galaxy S25+ took 1 min 6 s. The ~2.5 s model retrain is the
one noticeable system wait (§8, item D2).

---

## 5. Responsive / narrow-width check

Measured in Chrome with a 375 × 812 mobile viewport.

| Check | Result |
|-------|--------|
| Server/compute time vs desktop | Identical — load is compute-bound, not layout-bound, so width does not affect it |
| Horizontal scroll at 375 px | **None** (`scrollWidth ≤ innerWidth`) on dashboard and transactions |
| Layout | Navigation wraps, metric cards and the "Smart Suggestions" panel stack cleanly; verified visually |

**PWA status.** The PWA is verified on the server side: the manifest is valid
and declares maskable icons, every asset is served, and `/sw.js` is served from
the root scope with the correct JavaScript MIME type and `Service-Worker-Allowed: /`
header (automated `PWATests` plus the functional run in §7). The embedded test
browser used for the automated checks refuses to register any service worker (a
control registration of an unrelated static script failed identically), so
installation was confirmed manually in **Microsoft Edge 157** on 2026-11-17.
Under DevTools → Application → Service workers, `/sw.js` showed as *activated and
running*, and Cache storage held the `expense-tracker-shell-v3` precache. It was
then confirmed on a real Android phone. On a real iPhone the app installed, but the
offline fallback did not appear (§5.1, open defect D15). See
[`docs/mobile-install.md`](mobile-install.md) for the phone install paths.

### 5.1 Real-device tests (Android and iPhone)

The checks above use browser emulation. To confirm behaviour on actual hardware,
the app was run on two physical phones, covering **two platforms** (Android/Chrome
and iOS/Safari) and **two user profiles** (expert and first-time). In each session
the generated statement was transferred to the phone and uploaded from it.

| Session | Date | Device | Browser | Connection | Tester |
|---------|------|--------|---------|------------|--------|
| A | 2026-11-18 | Samsung Galaxy S25+, Android 17 | Chrome | USB: `run_uat_session.py --usb` (`adb reverse`; server bound to `127.0.0.1`, nothing exposed to the network) | Developer — **expert baseline** (knows the app) |
| B | 2026-11-19 | Apple iPhone 13, iOS 27.2.1 | Safari | Wi-Fi over HTTPS with a locally trusted mkcert certificate (`SSL_CERT`/`SSL_KEY`); server reachable on the local network for the session only | First-time user — **non-expert**, completely **unassisted** |

| # | Check | Requirement | Session A (expert, Android) | Session B (first-time, iPhone) |
|---|-------|-------------|:------------------:|:----------------------:|
| 1 | First-use timing: stopwatch from the setup screen through statement upload to the populated dashboard | NFR-04 (≤ 5 min) | **PASS — 1 min 6 s** | **PASS — 1 min 39 s** (unassisted) |
| 2 | Installs to the home screen and launches full-screen (standalone, no address bar) | NFR-08, PWA | **PASS** | **PASS** |
| 3 | Receipt scan opens the phone camera; the photo uploads automatically and is read by OCR | D13 fix, receipts | **PASS** | **PASS, partial accuracy:** capture and upload worked, but some line items were read incorrectly (D14) |
| 4 | Menu scroll arrow (›) shows while items are off-screen and clears at the end of a real swipe | D13 fix | **PASS** | **PASS** |
| 5 | With the server stopped, the installed app shows the "You are offline" page instead of a browser error | PWA offline fallback | **PASS** | **FAIL** — open defect D15 (§8) |
| — | General use of the installed app across all screens | NFR-08 | **PASS** — everything worked | Core journey completed unassisted (check 1); no other issues reported |

**NFR-04.** The first-time user completed upload-to-dashboard in **1 min 39 s
without help**, about 33 % of the 5-minute allowance. This is the requirement's
direct evidence, and it passes with a wide margin. The expert baseline was
1 min 6 s (22 %). The first-time user took 33 s (50 %) longer. Most of that is
human interaction rather than system waiting, since the system's processing for
the whole upload is only ~2.5 s (§4.4). The two sessions also differ in device
and platform, so the 33 s cannot be attributed to inexperience alone.

**Cross-platform.** Every feature tested worked on both platforms except the
offline page on iOS (D15). Installation, camera capture and the navigation
behaviour all held up on Safari/WebKit as well as Chrome/Blink.

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
- **Safari (WebKit, iOS)** — not part of the original scope, but covered by the
  real-device session on an iPhone 13 (§5.1). Every tested feature worked except
  the offline fallback (D15).

| Browser | Desktop | Narrow / phone | Source |
|---------|:-------:|:---------------:|--------|
| Chrome / Chromium 152 | PASS — loads < 1 s, no layout issues | PASS — no horizontal scroll at 375 px; real Galaxy S25+ (Android 17) all checks pass | Measured (this document, §5.1) |
| Edge 157 | Service worker installs and runs (confirmed manually); load times expected to match Chrome (same Blink engine + same local server) | Not tested | Manual DevTools check; snippet below for timings |
| Safari, iOS 27.2.1 | Not tested | Real iPhone 13: install, camera, navigation PASS; offline page **FAIL** (D15) | Real-device session (§5.1) |

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

Final run on 2026-11-18, after every fix in §8, using full test discovery
(`python -m unittest discover -s tests -t .`) in the project virtual-env.

| Suite | Tests | Covers | Result |
|-------|:-----:|--------|:------:|
| `tests/test_routes.py` | 67 | Pages, auth, security controls, PWA, demo features. Includes `test_nfr01_core_pages_render_within_three_seconds`, the 6 security regression tests (D6–D9), and 3 timer tests: lockout expiry, idle sign-out after 10 minutes, and the idle timer refreshing on activity | PASS |
| `tests/test_ml.py` | 43 | Categoriser training/evaluation, forecasting, subscriptions | PASS |
| `tests/test_ingestion.py` | 41 | CSV parsing, validation, deduplication, atomic import (NFR-06) | PASS |
| `tests/test_services.py` | 35 | Budget recommendations, nudges, savings, reports | PASS |
| `tests/test_ocr.py` | 23 | Receipt OCR (real Tesseract, images and PDFs) | PASS |
| `tests/test_insights.py` | 17 | Smart Suggestions rules | PASS |
| `tests/test_end_to_end.py` | 9 | Statement → dashboard integration — incl. the NFR-02 and NFR-03 target tests | PASS |
| `tests/test_imap.py` | 4 | Email receipt fetcher (network-free) | PASS |
| **Total** | **239** | | **239 / 239 PASS** (exit 0, 0 skipped, 219 s) |

### Live end-to-end functional run — PC and mobile

To confirm that the fixes and the removal of the redundant `run_uat.bat` launcher
left the whole application working, two real servers were launched through the
UAT launcher, each with its own fresh isolated database:

- **PC session:** `run_uat_session.py --participant SMOKE-PC --port 5000`
- **Mobile session:** `run_uat_session.py --participant SMOKE-MOB --port 5001 --usb`.
  The USB bridge reported no phone attached, as expected; the server stayed bound
  to `127.0.0.1` per NFR-05.

A scripted user journey of 38 steps was then driven over real HTTP, using CSRF
tokens, session cookies and a genuine multipart CSV upload, with a desktop and a
mobile (Android Chrome) user-agent respectively. The steps covered first-launch
setup, statement import, every core page, category correction with retrain,
manual receipt items, savings goals, custom categories, model retrain, database
backup, CSV export, the monthly report, the assistant and both sync mocks, all PWA
assets and the service-worker headers, POST-only sign-out, the session gate, the
offline page, and the open-redirect guard.

| Session | Port | User-agent | Result |
|---------|:----:|------------|:------:|
| PC | 5000 | Desktop Chrome | **38 / 38 PASS** (22.1 s) |
| Mobile | 5001 | Android Chrome | **38 / 38 PASS** (22.2 s) |

A follow-up browser pass rendered all 7 pages with no horizontal overflow at
desktop width (port 5000) and at 375 px (port 5001), with every touch target at
least 44 px on mobile.

### Specification-only build (graded build)

The same journey was run against the build an examiner grades against FR-01 to
FR-12: `run_uat_session.py --participant SMOKE-SPEC --port 5002 --spec-only`
(`DEMO_FEATURES=0`), on 2026-11-18. In place of the demo steps, the run asserted
that every demo extension is completely absent:

| Check | Result |
|-------|:------:|
| Core journey (setup → import → all core pages → correction → receipt items → goals → categories → retrain → backup → export → report → PWA assets → security behaviour) | PASS |
| Assistant card, Household nav, bank/MoMo sync cards and email auto-fetcher card hidden | PASS |
| Demo routes return **404**: `/household`, `/ask-assistant`, `/sync-bank`, `/sync-momo`, `/auto-fetch-receipts` | PASS (5/5) |
| **Total** | **42 / 42 PASS** (17.4 s) |

### NFR-05 — nothing transmitted off the device

NFR-05 requires that no data leaves the user's machine. It was verified on
2026-11-18 at three independent layers while the full-demo and spec-only servers
ran their functional journeys:

| Layer | Method | Result |
|-------|--------|--------|
| **Server process** | A monitor sampled every TCP connection owned by both server processes throughout both runs (136 samples over 75 s, about one every 0.55 s) and flagged any remote address that was not loopback | **0 external connections** among 24 observed (all loopback test traffic) |
| **Browser** | All 7 pages loaded and every resource request recorded (Resource Timing API) | **35 / 35 requests to `127.0.0.1`**; no third-party hosts |
| **Source** | Scan of every template, the application scripts, the stylesheet, the manifest and the service worker for external URLs; scan of all Python modules for network libraries | **No external URLs.** The only network client is `services/imap_fetcher.py` |

**Documented exception.** The email receipt auto-fetcher (`imap_fetcher.py`)
contacts the user's own mail server over IMAP/TLS. It is a demo extension, is
removed entirely from the spec-only build (route returns 404 above), and does
nothing unless the user supplies `IMAP_EMAIL` and `IMAP_PASSWORD` themselves, so
it is an explicit, opt-in departure from NFR-05 rather than a silent one.

*Limitations of the method.* Connection sampling cannot rule out a connection
shorter than the sampling interval, and DNS lookups made by the operating system
resolver are not attributed to the process. The source scan covers both gaps by
showing that no code path besides the opt-in fetcher opens a network connection.

---

## 8. Defects and observations

Defects came from three sources: performance measurement (§4), a structured code
and mobile review carried out after the UAT, and the UAT itself. Every defect in
the first table was **found, fixed and re-verified**, each with a regression test
or a recorded browser/HTTP check. The second table holds the UAT usability
observations, which were investigated and found *not* to be functional defects.

Severity scale: **Major** — breaks a requirement or a security property;
**Minor** — degrades behaviour with a workaround available; **Cosmetic** —
presentation only.

### Defects found and resolved

| # | Found by | Defect | Cause | Severity | Fix | Verified by |
|---|----------|--------|-------|:--------:|-----|-------------|
| D5 | Performance measurement (§4.1) | Dashboard, Budget and Savings took ~3.7–3.9 s, **breaching NFR-01** (≤ 3 s) | The full ARIMA/Croston forecast pipeline was recomputed for 12 categories on every request | Major (requirement) | Forecasts memoised per user against a signature of the forecastable data + current month; cache warmed at startup (§4.2) | Re-measured: 19–73 ms server, < 1 s in browser (§4.3); NFR-01 test passes |
| D6 | Code review | **Open redirect** on sign-in: `/login?next=//evil.example` sent the signed-in user to an external site | The check `next.startswith('/')` also accepts `//host`, a protocol-relative URL that browsers resolve off-site. The original test only covered `https://…` | Major (security) | `security.safe_next()` accepts same-site relative paths only (rejects `//`, `\`, any scheme or host) | Confirmed exploit before the fix (302 to `//evil.example`); 2 regression tests |
| D7 | Code review | **Stored XSS** in the AI assistant: a category or savings-goal name such as `<img src=x onerror=…>` executed script on the dashboard | Assistant answers interpolated user-entered names into HTML unescaped, and the chat inserted the typed question with `innerHTML` | Major (security)¹ | Names HTML-escaped server-side; the question rendered with `textContent` | Regression test; live browser check (payload displayed as text, handler never fired) |
| D8 | Code review | **No sign-in throttling**: unlimited password guesses | No attempt counter existed. Harmless on `127.0.0.1`, but exposed to the local network when bound to `0.0.0.0` for phone testing | Minor (security) | 5 consecutive failures → 60 s lockout per client (HTTP 429); counter resets on success | 2 regression tests |
| D9 | Code review | **Sign-out was a GET link**, so any web page could sign the user out (`<img src="/logout">`) | State-changing action exposed over GET without CSRF protection | Minor (security) | Logout is POST-only with a CSRF token; the nav shows it as a button | Regression test (GET → 405); live sign-out in browser |
| D10 | Mobile review | The documented **iPhone / Wi-Fi PWA install path could not work** | `docs/mobile-install.md` instructed setting `SSL_CERT`/`SSL_KEY`, but no code read them; ad-hoc HTTPS is untrusted by phones, so the service worker never registered | Major (functional, NFR-08) | `app.py` and `run_uat_session.py` now load a trusted certificate pair from `SSL_CERT`/`SSL_KEY` | Server started on `https://`, served HTTP 200 over TLS |
| D11 | Mobile review | **Settings page did not fit a phone** (rendered ~750 px wide at a 375 px viewport) | The global `td.num { white-space: nowrap }` rule applied to prose values, and the add-category form was a non-wrapping row | Minor (layout, NFR-08) | Wrapping label/value table class; wrapping add-category form | Layout probe: 375 px, no horizontal scroll on all 7 pages |
| D12 | Mobile review | Every page's first paint **waited on a CDN** (Chart.js), and charts needed internet access | Chart.js was loaded render-blocking from jsDelivr in `<head>` | Minor (performance / offline, NFR-05) | Chart.js self-hosted, deferred and pre-cached by the service worker | 0 CDN requests; library loads locally in ~23 ms; charts draw |
| D13 | Mobile review | Mobile usability gaps: table totals misaligned, half the menu hidden off-screen with no cue, touch targets below 44 px, receipt scan could not open the camera | Responsive CSS omitted `tfoot`; the nav hid its scrollbar; `.small` controls sized for desktop; no `capture` attribute | Cosmetic | `tfoot` stacked as cards; edge-fade + chevron on the nav; 44 px targets; `capture="environment"` with auto-submit | Measured at 375 px: all targets 44 px; chevron clears at scroll end |

¹ The assistant is a demo extension in a single-user, locally hosted app, so in
practice this is self-XSS. It is graded Major because it would be exploitable as
soon as names could come from another person, for example via the household
feature.

### Open defects

| # | Found by | Defect | Analysis | Severity | Status |
|---|----------|--------|----------|:--------:|--------|
| D15 | Real-device session B (iPhone 13, iOS 27.2.1, Safari, mkcert HTTPS over Wi-Fi) | With the server stopped, the home-screen app did **not** show the "You are offline" page | The same fallback **passes** on Android/Chrome (session A), and the service worker was confirmed active in Edge 157, so the implementation works on Chromium. On iOS the fallback needs a service worker registered *inside the home-screen app*. Everything else that passed on the iPhone (install, camera, navigation) works without a service worker, so the symptom fits a worker that never registered there. Two candidate causes: **(1)** the mkcert root certificate was installed but not granted *full trust* (Settings → General → About → Certificate Trust Settings). Safari can still load the page after a certificate warning, but refuses service-worker registration on an untrusted certificate. **(2)** iOS runs home-screen apps in a container separate from Safari, so a worker registered in a Safari tab does not carry over; it must register inside the installed app while the server is running. | Minor (offline convenience only; every core feature works on iOS) | **Open.** Cause not yet confirmed. Next step: check the certificate trust setting, then re-test opening the installed app once while the server runs before stopping it |

### UAT usability observations (not defects)

| # | Source | Observation | Investigated cause | Severity | Resolution |
|---|--------|-------------|--------------------|:--------:|------------|
| D1 | P04 | "80% budget-nudge alerts" marked *did not work as expected* | **Not a functional defect.** The alert was verified to fire on the demo data — "Housing at 95% of budget" (within the ≥80% warning band) — and the automated test for it passes. The alert is data-conditional and self-labels with the live percentage under a generic "Alerts" panel below the budget charts, so it was most likely not recognised as *the 80% nudge*, or sat below the fold on a laptop. | Minor (discoverability) | Recommend: label the alert against the 80% threshold and raise the Alerts panel above the charts. *(recommendation — Ch 9)* |
| D2 | P04 | Category correction "learns instantly" marked *Partly* | The correction **does** retrain immediately, but the retrain takes ~2.5 s (§4.4) with no on-screen progress indicator, so the "instant" learning is not visibly confirmed. | Minor (feedback) | Recommend: show a "learning…" indicator and an explicit confirmation on submit. *(recommendation — Ch 9)* |
| D3 | P04 | Savings goals marked *Partly* | Functionally correct — target, monthly-required amount and on-track status all compute and render; the prominence of the on-track status is the likely cause of partial confidence. | Cosmetic | Recommend: make the on-track status more prominent. *(recommendation — Ch 9)* |
| D4 | UAT data | P06 and P07 selected 5 for every SUS item, including the negatively-worded ones | Acquiescence / straight-lining (careless responding) | N/A (data) | Reported in the UAT results workbook (`docs/uat_results.xlsx`, kept locally and excluded from the public repository because it holds participant responses) with a sensitivity analysis: mean SUS 75.4 (all) vs 85.5 (excluding the two). |
| D14 | Real-device session B (iPhone 13) | Receipt scan captured and uploaded the photo, but **some line items were read incorrectly** | OCR accuracy limitation, not a code fault. Tesseract's accuracy on phone photos depends on lighting, focus, angle and thermal-print quality. The design limits the impact: the scanner never invents items; a wrongly-read line can be deleted and the correct one added by hand; and the itemised total is checked against the transaction amount, so a mis-read shows as a visible mismatch | Minor (accuracy) | Recommend for Ch 9: image pre-processing (deskew, contrast/threshold) and a review-and-edit step before scanned items are saved. *(recommendation — Ch 9)* |

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
| NFR-04 Usability | First-time upload → dashboard ≤ 5 min | First-time user, unassisted: **1 min 39 s** on an iPhone 13 (33 % of allowance); expert baseline 1 min 6 s on a Galaxy S25+ (§5.1) | **PASS** |
| NFR-05 Security / local-only | Nothing transmitted off-device | 0 external connections (process monitor); 35/35 browser requests local; no external URLs in source; opt-in IMAP fetcher is the documented exception | **PASS** |
| NFR-08 Compatibility | Chrome/Edge, desktop + mobile | Chrome 152 desktop + 375 px; Edge 157 (service worker confirmed); Samsung Galaxy S25+ (Android 17, Chrome), installed as a PWA; iPhone 13 (iOS 27.2.1, Safari) — all tested features except the offline page (D15, open) (§5.1); Firefox out of scope by decision (§6) | **PASS** (D15 open on iOS) |

Nine defects (D5–D13, §8) were found and resolved: one requirement breach
(NFR-01), four security issues, one broken documented install path, and three
mobile layout/usability issues. Each was re-verified after its fix. One defect
remains **open**: the offline fallback on iOS (D15, Minor). Its two likely causes
are documented with a next step in §8.

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
