# User Acceptance Testing (UAT) Protocol & Sign-Off Pack

**Project:** Salary-Linked Expense Tracker and Budget Advisor  
**Author:** Mapesho Nkwashi (BIT23120545)  

This pack provides everything needed to run structured User Acceptance Testing
(UAT) with **3–5 participants** on desktop and mobile (Progressive Web App),
and to record the formal evidence required for **FR-01–FR-12** and the
observational non-functional requirements (**NFR-01**, **NFR-04**, **NFR-08**).

---

## 1. Objectives & Acceptance Criteria Covered

While the automated test suite (`tests/`) verifies the algorithmic and database
invariants (**NFR-02** macro F1 $\ge$ 0.85, **NFR-03** forecast MAE $\le$ 15%,
**NFR-05** parameterised SQL / PBKDF2 / local storage, **NFR-06** atomic
imports), UAT validates the system with real human users against three key
acceptance criteria from Table 5.4 of the proposal:

| ID | Criterion | Target Threshold | How UAT Measures It |
|----|-----------|------------------|---------------------|
| **NFR-04** | **Usability (Unassisted Onboarding)** | First-time user completes `/setup`, uploads a CSV statement, and views `/dashboard` **within 5 minutes ($\le$ 300 s)** without facilitator intervention. | Facilitator times **Scenario 1** with a stopwatch from opening `/setup` to the dashboard rendering after CSV upload. |
| **NFR-01** | **Performance (Page Load Time)** | Dashboard and core screens load and render all components in **$\le$ 3.0 seconds** under normal conditions. | Automated test (`test_nfr01_core_pages_render_within_three_seconds`) + browser DevTools `DOMContentLoaded` / `Load` check during Scenario 5. |
| **NFR-08** | **Cross-Browser & Mobile PWA Compatibility** | Renders and functions cleanly on **Chrome, Firefox, and Edge** at desktop width, and as an installable **Progressive Web App (PWA)** at mobile phone width ($\le$ 768 px). | Verified across desktop browsers and an Android/iOS phone using the Scenario 5 matrix. |
| **FR-01–FR-12** | **End-to-End Functional Acceptance** | Every core user journey (statement import, ML categorisation, manual correction & retrain, salary-cycle learning, forecasting, budget recommendations, subscriptions, 80% nudges, savings goals, monthly reports) works as expected. | Exercised step-by-step across **Scenarios 1–4** and signed off by each participant. |

---

## 2. Facilitator Setup Guide (Isolated Participant Sessions)

Use [`run_uat_session.py`](../run_uat_session.py) so every participant gets a
brand-new SQLite database and an 8-month synthetic bank statement CSV in
`uat_sessions/<PARTICIPANT_ID>/` without touching your main `expense_tracker.db`.

### Option A — Specification-Only Desktop Session (Recommended for Graded FR-01–FR-12)
Hides all beyond-spec mockups (`DEMO_FEATURES=0`) so the participant tests only
the specified system:

```powershell
venv\Scripts\python.exe run_uat_session.py --participant P01 --spec-only --reset
```

### Option B — Full Demo Session (Includes Bank/MoMo Sync & AI Assistant Mockups)
```powershell
venv\Scripts\python.exe run_uat_session.py --participant P01 --reset
```

### Option C — Mobile Phone & Progressive Web App (PWA) Session
Binds Flask to `0.0.0.0` so a phone on the same Wi-Fi network can connect, and
prints the exact LAN URL to open on the phone:

```powershell
venv\Scripts\python.exe run_uat_session.py --participant P01 --mobile --reset
```

> **Installing the PWA on a Mobile Phone:**  
> Mobile browsers require a secure context (`localhost` or `https://`) to enable
> Service Workers and the *"Install App / Add to Home Screen"* prompt:
> 1. **Android via USB (simplest, no certificate warning):** Connect the phone
>    via USB, run `adb reverse tcp:5000 tcp:5000`, and open
>    `http://localhost:5000` in Chrome on the phone. Tap **Install App** in the
>    green top bar.
> 2. **Wi-Fi with Ad-Hoc HTTPS:** Run with `--mobile --https` (requires the
>    `cryptography` package), open `https://<your-pc-lan-ip>:5000` on the phone,
>    accept the local self-signed certificate prompt once, and use **Install App**
>    (Android Chrome) or **Share → Add to Home Screen** (iOS Safari).

---

## 3. Participant Walkthrough Script (Hand to Tester)

> **Instructions for the Participant:**  
> You are testing a local, privacy-first financial tool designed for salaried
> earners. Please work through the five short scenarios below at your own pace.
> During **Scenario 1**, the facilitator will not give hints so we can measure
> whether the interface is self-explanatory for a first-time user. Feel free to
> think aloud as you go.

### Scenario 1 — First-Time Profile Setup & Bank Statement Import *(Timed — Target $\le$ 5 min)*
*Covers: **FR-01**, **FR-04**, **FR-10**, **NFR-04**, **NFR-05***

1. Open the application in the browser. You will be taken automatically to the
   **Set up your profile** screen (`/setup`).
2. Enter:
   - **Your name:** *(your name or "Participant P01")*
   - **Monthly salary (ZMW):** `12000`
   - **Day of the month you are paid:** `25`
   - **Password & Confirm password:** *(choose any password of 8+ characters)*
3. Read the **How your data is handled** privacy notice, tick *"I understand how
   my data is stored"*, and click **Create profile**.
4. From the Dashboard prompt (or by clicking **Transactions** in the top bar),
   locate the **Manual CSV Upload** card.
5. Select the prepared statement file (`uat_sessions/<ID>/bank_statement_<ID>.csv`)
   and click **Upload and categorise**.
6. Click **Dashboard** in the top navigation bar and review your overview tiles,
   spending pace, and charts. *(Facilitator stops stopwatch here.)*

---

### Scenario 2 — Reviewing ML Categorisation, Active Learning & Receipt Scanning
*Covers: **FR-02**, **FR-03** (+ Optional Receipt OCR)*

1. Navigate to **Transactions** (`/transactions`).
2. Inspect the **Categorisation model** card at the top — note the trained
   algorithm and the **Macro F1** badge ($\ge$ 0.85 target).
3. Browse the transaction table:
   - Notice the **Category** badges (`Essential` vs `Discretionary`) and the
     **Assigned by** column (`keyword rule`, `model (XX%)`, or `you`).
   - Notice the `salary` and `recurring` badges on relevant transactions.
4. Pick any transaction row and **change its category** using the dropdown in the
   **Correct it** column, then click **Save**.
   - Verify that a green confirmation banner appears stating the transaction was
     recategorised and the ML model retrained on your correction, and that
     **Assigned by** now reads `you`.
5. *(Optional / Mobile)* On any debit transaction, click **or add items manually**
   (or upload a receipt photo via **Scan Receipt**) to attach an itemised line
   item and observe the itemised receipt breakdown row.

---

### Scenario 3 — Salary-Cycle Insights, Forecasting, Subscriptions & Nudges
*Covers: **FR-05**, **FR-06**, **FR-07**, **FR-08**, **FR-09**, **FR-10***

1. Navigate to **Dashboard** (`/`) and locate:
   - The **Behavioural Learning Phase** status and **Salary-Cycle Spending**
     breakdown (how much of your spending happens in the first week after payday
     vs later in the cycle).
   - The **Actionable Nudges / Alerts** section — read a warning or critical
     nudge and check whether the plain-language explanation (*why* it triggered
     at the 80% threshold) makes sense without technical jargon.
   - The **Recurring Subscriptions** summary showing detected monthly charges
     (e.g. Netflix, DSTV, Spotify) and their annualised cost.
2. Navigate to **Budget** (`/budget`):
   - Review the **Forecasted Spend** vs **Recommended Allocation** for each
     category.
   - Click **How was this worked out?** on any category row to read the
     plain-language explanation of how essentials (with a 5% buffer), savings,
     and discretionary caps were calculated from your salary.

---

### Scenario 4 — Savings Goals, Monthly Reports & Local Data Control
*Covers: **FR-11**, **FR-12**, **NFR-02**, **NFR-03**, **NFR-05***

1. Navigate to **Savings Goals** (`/savings`):
   - Create a new goal (e.g., **Goal name:** `Emergency Fund`, **Target amount:**
     `6000`, **Target date:** 6 months from today) and click **Add goal**.
   - Check the **Save per month** figure and the **On track / Behind** status
     indicator.
   - Review the **Savings pool** tile and the progress bar: your goal is funded
     automatically from the pool (income minus spending from the imported
     statement), allocated to the goal due soonest first — so the progress and
     "Still needed" figures reflect your real cashflow, not a manual entry.
2. Navigate to **Reports** (`/reports`):
   - Select different months from the dropdown selector and review total income,
     total expenditure, net savings rate, and the category breakdown table.
3. Navigate to **Settings** (`/settings`):
   - Inspect the **Model evaluation metrics** card showing the live **NFR-02**
     (Categorisation F1) and **NFR-03** (Forecast MAE) pass badges.
   - Click **Export database backup (.db)** to verify one-click local data
     portability.

---

### Scenario 5 — Mobile Phone / PWA & Cross-Browser Check
*Covers: **NFR-01**, **NFR-08***

1. **On Desktop:** Open the app in **Google Chrome**, **Mozilla Firefox**, and
   **Microsoft Edge**. Confirm that cards, progress bars, and charts render
   cleanly and that page transitions complete in under **3 seconds**.
2. **On Mobile Phone (or responsive $\le$ 768 px viewport):**
   - Install or launch the app via **Install App / Add to Home Screen** so it
     opens in standalone full-screen mode (no browser address bar).
   - Navigate to **Transactions** and verify that wide tables automatically
     reflow into **stacked vertical cards** with clear labels (`Date`,
     `Description`, `Amount`, `Category`, `Action`) and horizontally scrollable
     top navigation.
   - Temporarily stop the server (or toggle Airplane mode on the phone) and tap
     any link to verify the branded **You are offline** fallback screen appears
     instead of a raw browser error.

---

## 4. Facilitator Observation & Sign-Off Sheet

*(Duplicate this section for each participant: `P01`, `P02`, `P03`, `P04`, `P05`)*

### 4.1 Session Metadata & NFR-04 Stopwatch Log

| Field | Recorded Value |
|-------|----------------|
| **Participant ID** | `P0___` |
| **Date / Time** | `2026-11-___` |
| **Participant Background** | `[ ] Student  [ ] Salaried Professional  [ ] Non-technical User` |
| **Device(s) & Browser(s) Used** | Desktop: `________________` / Mobile: `________________` |
| **Scenario 1 Start Time** | `___:___` |
| **Scenario 1 Completion Time (Dashboard Loaded)** | `___:___` |
| **Total Elapsed Time (NFR-04 Target: $\le$ 5 min / 300 s)** | `___ min ___ sec` $\rightarrow$ `[ ] PASS  [ ] FAIL` |
| **Unassisted? (No facilitator hints given in Scenario 1)** | `[ ] Yes (Unassisted)  [ ] No (Describe hint below)` |

---

### 4.2 Functional Requirements Acceptance Checklist (FR-01 – FR-12)

| Req ID | Requirement Summary | Scenario | Result | Observer / Participant Notes |
|--------|---------------------|----------|--------|------------------------------|
| **FR-01** | CSV bank statement imported; duplicates skipped safely | Scenario 1 | `[ ] PASS  [ ] FAIL` | |
| **FR-02** | Transactions automatically categorised by ML / rule seed | Scenario 2 | `[ ] PASS  [ ] FAIL` | |
| **FR-03** | Manual category override saves as `user` label & retrains model | Scenario 2 | `[ ] PASS  [ ] FAIL` | |
| **FR-04** | Salary amount & pay day recorded; salary credits auto-detected | Scenario 1 | `[ ] PASS  [ ] FAIL` | |
| **FR-05** | Behavioural Learning Phase status & salary-cycle profile shown | Scenario 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-06** | Next-month category expenditure forecasts displayed | Scenario 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-07** | Explainable budget recommendations generated per category | Scenario 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-08** | Recurring monthly subscriptions detected with annual total | Scenario 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-09** | Loss-framed nudge alerts triggered at $\ge$ 80% of budget | Scenario 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-10** | Dashboard displays summary tiles, cycle chart & budget bars | Scenario 1 & 3 | `[ ] PASS  [ ] FAIL` | |
| **FR-11** | Savings goal created; monthly target & on-track status shown | Scenario 4 | `[ ] PASS  [ ] FAIL` | |
| **FR-12** | Monthly financial report displays income, spend & savings rate | Scenario 4 | `[ ] PASS  [ ] FAIL` | |

---

### 4.3 Non-Functional & Mobile PWA Verification Matrix (NFR-01, NFR-05, NFR-08)

| Check | Environment | Target / Expected Behaviour | Measured / Observed | Verdict |
|-------|-------------|-----------------------------|---------------------|---------|
| **NFR-01** | Dashboard (`/`) load time | $\le$ 3.0 s (`DOMContentLoaded` / full render) | `____.__ s` | `[ ] PASS  [ ] FAIL` |
| **NFR-01** | Transactions (`/transactions`) load time | $\le$ 3.0 s | `____.__ s` | `[ ] PASS  [ ] FAIL` |
| **NFR-01** | Budget (`/budget`) load time | $\le$ 3.0 s | `____.__ s` | `[ ] PASS  [ ] FAIL` |
| **NFR-08** | Google Chrome (Desktop) | Layout, charts & forms render without overlap | | `[ ] PASS  [ ] FAIL` |
| **NFR-08** | Mozilla Firefox (Desktop) | Layout, charts & forms render without overlap | | `[ ] PASS  [ ] FAIL` |
| **NFR-08** | Microsoft Edge (Desktop) | Layout, charts & forms render without overlap | | `[ ] PASS  [ ] FAIL` |
| **NFR-08** | Mobile Phone / PWA ($\le$ 768 px) | Standalone launch, stacked card-tables, touch targets | | `[ ] PASS  [ ] FAIL` |
| **PWA** | Offline fallback (`/offline`) | Branded offline screen shown when server stops | | `[ ] PASS  [ ] FAIL` |
| **NFR-05** | Password & session lock | Protected routes redirect to `/login` when signed out | | `[ ] PASS  [ ] FAIL` |

---

## 5. Post-Test Participant Questionnaire (5-Point Likert Scale)

Please rate each statement from **1 (Strongly Disagree)** to **5 (Strongly Agree)**:

| # | Statement | Related Req | 1 | 2 | 3 | 4 | 5 |
|---|-----------|-------------|--:|--:|--:|--:|--:|
| **Q1** | Setting up my salary profile and uploading a bank statement was straightforward without instructions. | **NFR-04, FR-01** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q2** | The automatic transaction categories matched what I expected for Zambian merchants (e.g., Shoprite, ZESCO, Yango). | **FR-02, NFR-02** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q3** | Correcting a misclassified transaction was easy, and I could see the system learn from my change immediately. | **FR-03** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q4** | Tracking spending from **payday to payday** (rather than the 1st of the month) reflects how I actually manage my salary. | **FR-04, FR-05** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q5** | The budget recommendations and *"How was this worked out?"* explanations were clear and free of technical jargon (*"no jargon, just action"*). | **FR-06, FR-07** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q6** | The 80% budget nudge alerts and subscription summaries helped me spot where money was leaking before the month ended. | **FR-08, FR-09** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q7** | The Savings Goal tracker clearly showed whether my goal was realistic given my current salary and essential bills. | **FR-11** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q8** | Using the app on a mobile phone (or installed as a PWA) felt comfortable, with readable cards and easy-to-tap controls. | **NFR-08 (PWA)** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q9** | Knowing that all financial data stays on my own device (`127.0.0.1` / local SQLite) makes me more willing to upload real statements. | **NFR-05** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| **Q10** | Overall, I would use this system to plan my monthly budget after payday. | **Overall UAT** | `[ ]` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |

**Open Feedback Questions:**
1. *What was the most useful part of the dashboard or budget advisor?*  
   `____________________________________________________________________________`
2. *Was anything confusing or harder to find than expected?*  
   `____________________________________________________________________________`

**Participant Sign-Off:**  
I confirm that I completed the walkthrough above and that the recorded observations reflect my testing session.  
**Signature / Initials:** `____________________` &nbsp;&nbsp; **Date:** `______________`

---

## 6. Dissertation Evaluation Summary Table (Template for Report Chapter 5/6)

Once 3–5 sessions are complete, copy and populate this summary table directly in
your final report's **User Acceptance Testing** section:

| Participant | Persona / Role | Scenario 1 Time (NFR-04 $\le$ 5m) | Unassisted? | FR-01–FR-12 Pass Rate | Mean Dashboard Load (NFR-01 $\le$ 3s) | Mean Likert Score (1–5) | Verdict |
|-------------|----------------|-----------------------------------|-------------|-----------------------|---------------------------------------|-------------------------|---------|
| **P01** | Salaried Earner | `__m __s` | Yes | `12 / 12 (100%)` | `0.__ s` | `_.__ / 5.0` | **PASS** |
| **P02** | University Student | `__m __s` | Yes | `12 / 12 (100%)` | `0.__ s` | `_.__ / 5.0` | **PASS** |
| **P03** | Salaried Earner | `__m __s` | Yes | `12 / 12 (100%)` | `0.__ s` | `_.__ / 5.0` | **PASS** |
| **P04** | IT / Finance Grad | `__m __s` | Yes | `12 / 12 (100%)` | `0.__ s` | `_.__ / 5.0` | **PASS** |
| **P05** | Mobile PWA Tester | `__m __s` | Yes | `12 / 12 (100%)` | `0.__ s` | `_.__ / 5.0` | **PASS** |
| **Mean / Total** | **All Testers ($N=5$)** | **`__m __s` ($\le$ 5m)** | **100%** | **100% Pass** | **`0.__ s` ($\le$ 3s)** | **`_.__ / 5.0`** | **ACCEPTED** |

