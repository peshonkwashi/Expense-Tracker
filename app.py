"""Presentation Layer (section 3.6.5).

Flask routes for the Salary-Linked Expense Tracker and Budget Advisor. Every
route is thin: it reads request state, calls into services/ or ml/, and renders
a template. Business logic lives in those modules so that each layer can be
tested and replaced independently (NFR-07).
"""
import os
import threading
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

import config
import database
import security
from database import get_db_connection, init_db
from ml import categorization, forecasting, subscriptions
from services import behavioural, ingestion, recommendation

from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)
app.secret_key = security.load_or_create_secret_key()
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_BYTES

csrf = CSRFProtect(app)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(
    minutes=config.SESSION_TIMEOUT_MINUTES)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


@app.context_processor
def inject_globals():
    return {'currency': config.CURRENCY, 'now': datetime.now(),
            'session_timeout': config.SESSION_TIMEOUT_MINUTES,
            'nudge_threshold': int(config.NUDGE_THRESHOLD * 100),
            'demo_features': config.DEMO_FEATURES}


def _demo_only():
    """Abort with 404 when demo features are disabled, so the mock routes behave
    as if they do not exist in a spec-only (DEMO_FEATURES=0) build. Read at call
    time so the flag can be toggled in tests."""
    if not config.DEMO_FEATURES:
        abort(404)


@app.template_filter('money')
def money(value):
    try:
        return f'{float(value):,.2f}'
    except (TypeError, ValueError):
        return '0.00'


# --- Authentication and first-run -----------------------------------------

@app.route('/login', methods=('GET', 'POST'))
def login():
    conn = get_db_connection()
    try:
        if not security.password_is_set(conn):
            return redirect(url_for('setup'))

        if request.method == 'POST':
            client = request.remote_addr or 'local'
            # Checked before the password so a locked-out client cannot keep
            # guessing (and does not even pay for a hash comparison).
            wait = security.login_lockout_remaining(client)
            if wait:
                flash(f'Too many incorrect attempts. Try again in {wait} '
                      'seconds.', 'error')
                return render_template('login.html'), 429
            if security.verify_password(conn, request.form.get('password')):
                security.reset_login_attempts(client)
                security.start_session(conn)
                # Only same-site relative paths: '//host' is rejected too.
                destination = security.safe_next(request.args.get('next'))
                return redirect(destination or url_for('dashboard'))
            security.record_failed_login(client)
            flash('Incorrect password.', 'error')
        return render_template('login.html')
    finally:
        conn.close()


@app.route('/logout', methods=('POST',))
def logout():
    # POST-only with a CSRF token: as a GET link, any web page could sign the
    # user out with something as simple as <img src="/logout">.
    session.clear()
    flash('You have been signed out.', 'info')
    return redirect(url_for('login'))


@app.route('/reset-password', methods=('GET', 'POST'))
def reset_password():
    """Offline password reset by verifying the local user's profile details."""
    conn = get_db_connection()
    try:
        if not database.get_user(conn) or not security.password_is_set(conn):
            return redirect(url_for('setup'))

        if request.method == 'POST':
            client = request.remote_addr or 'local'
            wait = security.login_lockout_remaining(client)
            if wait:
                flash(f'Too many incorrect attempts. Try again in {wait} '
                      'seconds.', 'error')
                return render_template('reset_password.html', form=request.form), 429

            if not security.verify_profile_for_reset(
                    conn,
                    request.form.get('full_name'),
                    request.form.get('salary_amount'),
                    request.form.get('salary_day')):
                security.record_failed_login(client)
                flash('Those profile details do not match the account on this '
                      'device.', 'error')
                return render_template('reset_password.html', form=request.form)

            password = request.form.get('password') or ''
            if password != (request.form.get('password_confirm') or ''):
                flash('The two passwords do not match.', 'error')
                return render_template('reset_password.html', form=request.form)

            ok, err = security.set_password(conn, password)
            if not ok:
                flash(err, 'error')
                return render_template('reset_password.html', form=request.form)

            security.reset_login_attempts(client)
            session.clear()
            flash('Your password has been reset. Please sign in with your new '
                  'password.', 'success')
            return redirect(url_for('login'))

        return render_template('reset_password.html', form={})
    finally:
        conn.close()


@app.route('/setup', methods=('GET', 'POST'))
def setup():
    """First launch: profile, password and the transparent data notice."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        if user and security.password_is_set(conn):
            return redirect(url_for('login'))

        if request.method == 'POST':
            errors = []
            full_name = (request.form.get('full_name') or '').strip()
            if not full_name:
                errors.append('Full name is required.')

            try:
                salary_amount = float(request.form.get('salary_amount') or 0)
                if salary_amount <= 0:
                    errors.append('Salary amount must be greater than zero.')
            except ValueError:
                salary_amount = 0
                errors.append('Salary amount must be a number.')

            try:
                salary_day = int(request.form.get('salary_day') or 0)
                if not 1 <= salary_day <= 31:
                    errors.append('Salary day must be between 1 and 31.')
            except ValueError:
                salary_day = 0
                errors.append('Salary day must be a whole number.')

            password = request.form.get('password') or ''
            if password != (request.form.get('password_confirm') or ''):
                errors.append('The two passwords do not match.')
            elif len(password) < security.MIN_PASSWORD_LENGTH:
                errors.append(f'Password must be at least '
                              f'{security.MIN_PASSWORD_LENGTH} characters long.')

            if not request.form.get('accept_notice'):
                errors.append('Please acknowledge the data handling notice.')

            if errors:
                for message in errors:
                    flash(message, 'error')
                return render_template('setup.html', form=request.form)

            if user:
                conn.execute(
                    'UPDATE User SET full_name = ?, salary_amount = ?, '
                    'salary_day = ? WHERE user_id = ?',
                    (full_name, salary_amount, salary_day, user['user_id']))
            else:
                conn.execute(
                    'INSERT INTO User (full_name, salary_amount, salary_day, '
                    'learning_started_on) VALUES (?, ?, ?, ?)',
                    (full_name, salary_amount, salary_day, date.today().isoformat()))
            security.set_password(conn, password)
            security.acknowledge_notice(conn)
            conn.commit()

            security.start_session()
            flash('Profile created. Upload a bank statement to begin the '
                  'behavioural learning phase.', 'success')
            return redirect(url_for('transactions'))

        return render_template('setup.html', form={})
    finally:
        conn.close()


@app.before_request
def require_setup():
    """Send a fresh installation to setup before anything else."""
    # The PWA service worker and offline page are public infrastructure: a
    # browser fetches /sw.js with no session, and /offline must render even when
    # unauthenticated, so neither is redirected to setup.
    if request.endpoint in {'setup', 'login', 'reset_password', 'static',
                            'service_worker', 'offline', None}:
        return None
    conn = get_db_connection()
    try:
        if not database.get_user(conn) or not security.password_is_set(conn):
            return redirect(url_for('setup'))
    finally:
        conn.close()
    return None


# --- Dashboard -------------------------------------------------------------

def _dashboard_context(conn, user):
    """Shared assembly for the dashboard and budget screens."""
    learning = behavioural.learning_status(conn, user)
    cycle = behavioural.cycle_profile(conn, user)
    detected = subscriptions.detect_subscriptions(conn, user['user_id'], persist=False)
    subs = subscriptions.subscription_summary(detected)

    budget = None
    nudges = []
    suggestions = []
    if learning['complete']:
        budget = recommendation.build_budget(conn, user)
        recommendation.persist_recommendations(conn, user['user_id'], budget)
        nudges = recommendation.generate_nudges(budget, cycle, subs)
        
        from services.insights import generate_suggestions
        from services.recommendation import _month_key
        suggestions = generate_suggestions(conn, user['user_id'], _month_key())

    return {'learning': learning, 'cycle': cycle, 'subscriptions': subs,
            'budget': budget, 'nudges': nudges, 'suggestions': suggestions}


# --- On-device assistant -----------------------------------------------------

@app.route('/ask-assistant', methods=['POST'])
@security.login_required
def ask_assistant():
    """On-device assistant (services/assistant.py): parses the question and
    answers from the user's own data, with no network access (NFR-05).

    The answer is an HTML fragment (for <strong> emphasis); every
    user-controlled string in it (category, goal, merchant names) is escaped
    by the assistant. The previous question's slots are kept in the session so
    follow-ups like "and last month?" work.
    """
    from flask import jsonify
    from services import assistant
    _demo_only()
    payload = request.get_json(silent=True) or {}
    question = str(payload.get('question') or '')[:300]
    conn = database.get_db_connection()
    try:
        user = database.get_user(conn)
        reply = assistant.answer(conn, user, question,
                                 context=session.get('assistant_ctx'))
    finally:
        conn.close()
    if reply.context:
        session['assistant_ctx'] = reply.context
    return jsonify({'answer': reply.answer, 'suggestions': reply.suggestions})


@app.route('/')
@security.login_required
def dashboard():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        context = _dashboard_context(conn, user)
        savings = recommendation.savings_position(conn, user, context['budget'])

        chart = {'labels': [], 'recommended': [], 'spent': []}
        if context['budget']:
            for row in context['budget']['rows'][:8]:
                chart['labels'].append(row['category'])
                chart['recommended'].append(row['recommended'])
                chart['spent'].append(row['spent'])

        cycle_chart = {
            'labels': list(context['cycle']['weeks'].keys()),
            'values': list(context['cycle']['weeks'].values()),
        }

        return render_template('dashboard.html', user=user, savings=savings,
                               chart=chart, cycle_chart=cycle_chart, **context)
    finally:
        conn.close()


# --- Transactions ----------------------------------------------------------

@app.route('/transactions/export', methods=['GET'])
@security.login_required
def export_transactions():
    """Export transactions to CSV (filtered by current selections)."""
    import csv
    from io import StringIO
    from flask import Response
    
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        
        selected_category = request.args.get('category', '')
        selected_month = request.args.get('month', '')
        search_query = (request.args.get('q') or '').strip()
        
        query = (
            "SELECT t.transaction_date, t.description, t.amount, t.transaction_type, "
            "c.category_name, t.is_subscription "
            "FROM Transaction_Record t "
            "LEFT JOIN Category c ON t.category_id = c.category_id "
            "WHERE t.user_id = ?"
        )
        params = [user['user_id']]
        
        if selected_category:
            query += ' AND c.category_name = ?'
            params.append(selected_category)
        if selected_month:
            query += " AND strftime('%Y-%m', t.transaction_date) = ?"
            params.append(selected_month)
        if search_query:
            query += " AND LOWER(t.description) LIKE ? ESCAPE '!'"
            params.append(f'%{_like_escape(search_query.lower())}%')
            
        query += ' ORDER BY t.transaction_date DESC, t.transaction_id DESC'
        
        rows = conn.execute(query, params).fetchall()
        
        si = StringIO()
        cw = csv.writer(si)
        cw.writerow(['Date', 'Description', 'Amount', 'Type', 'Category', 'Is Subscription'])
        for r in rows:
            cw.writerow([r['transaction_date'], r['description'], f"{r['amount']:.2f}", 
                         r['transaction_type'], r['category_name'], 
                         'Yes' if r['is_subscription'] else 'No'])
                         
        output = si.getvalue()
        
        filename = "transactions"
        if selected_month:
            filename += f"_{selected_month}"
        if selected_category:
            filename += f"_{selected_category}"
        filename += ".csv"
        
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )
    finally:
        conn.close()

# --- Bank Sync Mock ----------------------------------------------------------

@app.route('/sync-bank', methods=['POST'])
@security.login_required
def sync_bank():
    _demo_only()
    import random
    from datetime import date, timedelta
    
    conn = database.get_db_connection()
    try:
        user = database.get_user(conn)
        today = date.today()

        # Real bank sync is idempotent: re-fetching the same window finds nothing
        # new. Mirror that so repeated clicks don't pile up duplicate synthetic
        # rows and inflate every chart.
        if database.get_setting(conn, 'last_bank_sync') == today.isoformat():
            flash("Your bank account is already up to date — no new transactions "
                  "since your last sync today.", "info")
            return redirect(url_for('transactions'))

        # Realistic everyday merchants for a Zambian account (ZMW), each mapped
        # to its true category so a synced row is indistinguishable from one read
        # off an uploaded statement. All debits: a salary is monthly, not
        # something a few-day sync pulls, and injecting a fake one every sync
        # would distort the salary-cycle analysis and the savings pool.
        merchants = [
            ("Shoprite",      'Groceries',       80, 850),
            ("Pick n Pay",    'Groceries',       60, 700),
            ("Hungry Lion",   'Dining Out',      40, 220),
            ("Ubuntu Cab",    'Transport',       25, 160),
            ("Netflix",       'Subscriptions',  159, 159),
            ("MTN Airtime",   'Airtime & Data',  20, 200),
            ("Link Pharmacy", 'Healthcare',      30, 400),
            ("Game Stores",   'Shopping',       100, 900),
        ]
        category_ids = {name: database.category_id_for(conn, cat)
                        for name, cat, _, _ in merchants}

        # 5-10 synthetic new transactions dated over the last few days.
        num_txns = random.randint(5, 10)
        added = 0
        for _ in range(num_txns):
            name, _cat, low, high = random.choice(merchants)
            days_ago = random.randint(0, 3)
            txn_date = (today - timedelta(days=days_ago)).strftime('%Y-%m-%d')
            amt = round(random.uniform(low, high), 2)
            conn.execute(
                "INSERT INTO Transaction_Record (transaction_date, description, "
                "amount, transaction_type, category_id, category_source, user_id) "
                "VALUES (?, ?, ?, 'DEBIT', ?, 'rule', ?)",
                (txn_date, name, amt, category_ids[name], user['user_id'])
            )
            added += 1

        database.set_setting(conn, 'last_bank_sync', today.isoformat())
        conn.commit()

        # New data has arrived, so retrain and re-evaluate exactly as an upload or
        # a correction does — otherwise the models would silently ignore synced
        # rows while the other two ingestion paths learn from theirs.
        _retrain_and_evaluate(conn, user['user_id'])

        flash(f"Bank Sync Successful! Downloaded {added} new transactions and "
              f"refreshed your categories, budget and forecasts.", "success")
        return redirect(url_for('transactions'))
    finally:
        conn.close()


# --- Mobile Money Sync Mock --------------------------------------------------

@app.route('/sync-momo', methods=['POST'])
@security.login_required
def sync_momo():
    _demo_only()
    import random
    from datetime import date, timedelta

    conn = database.get_db_connection()
    try:
        user = database.get_user(conn)
        today = date.today()

        # Idempotent like a real wallet sync — a second pull the same day finds
        # nothing new rather than duplicating synthetic transactions.
        if database.get_setting(conn, 'last_momo_sync') == today.isoformat():
            flash("Your mobile money wallet is already up to date — no new "
                  "transactions since your last sync today.", "info")
            return redirect(url_for('transactions'))

        # Typical Zambian mobile-money outflows (MTN MoMo / Airtel Money), each
        # mapped to its real category. Debit-only, on the same reasoning as the
        # bank sync: money *received* over mobile money is not salary, and
        # counting inbound transfers as income would distort the salary-cycle and
        # savings figures. Mobile money's signature costs — airtime, bill
        # payments, merchant MoMo-Pay, sending money and agent cash-out — are all
        # outflows anyway, which is what the budget cares about.
        merchants = [
            ("MTN MoMo Airtime",         'Airtime & Data',  10,  100),
            ("Airtel Money Data Bundle", 'Airtime & Data',  50,  250),
            ("ZESCO Token via MoMo",     'Utilities',      100,  500),
            ("DSTV Payment via MoMo",    'Subscriptions',  250,  500),
            ("MoMo Pay - Shoprite",      'Groceries',       50,  600),
            ("MoMo Pay - Pharmacy",      'Healthcare',      30,  300),
            ("Send Money to Family",     'Transfers',      100, 1500),
            ("Cash Out at MoMo Agent",   'Cash Withdrawal', 200, 2000),
            ("MoMo Withdrawal Fee",      'Transfers',        5,   40),
        ]
        category_ids = {name: database.category_id_for(conn, cat)
                        for name, cat, _, _ in merchants}

        # 5-10 synthetic wallet transactions dated over the last few days.
        num_txns = random.randint(5, 10)
        added = 0
        for _ in range(num_txns):
            name, _cat, low, high = random.choice(merchants)
            days_ago = random.randint(0, 3)
            txn_date = (today - timedelta(days=days_ago)).strftime('%Y-%m-%d')
            amt = round(random.uniform(low, high), 2)
            conn.execute(
                "INSERT INTO Transaction_Record (transaction_date, description, "
                "amount, transaction_type, category_id, category_source, user_id) "
                "VALUES (?, ?, ?, 'DEBIT', ?, 'rule', ?)",
                (txn_date, name, amt, category_ids[name], user['user_id'])
            )
            added += 1

        database.set_setting(conn, 'last_momo_sync', today.isoformat())
        conn.commit()

        # Same as the bank sync: fresh data means retrain + re-evaluate.
        _retrain_and_evaluate(conn, user['user_id'])

        flash(f"Mobile Money Sync Successful! Imported {added} recent MTN MoMo / "
              f"Airtel Money transactions and refreshed your budget and forecasts.",
              "success")
        return redirect(url_for('transactions'))
    finally:
        conn.close()


@app.route('/transactions', methods=('GET', 'POST'))
@security.login_required
def transactions():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)

        if request.method == 'POST':
            _handle_upload(conn, user)
            return redirect(url_for('transactions'))

        selected_category = request.args.get('category', '')
        selected_month = request.args.get('month', '')
        search_query = (request.args.get('q') or '').strip()
        query = (
            "SELECT t.*, c.category_name, c.category_type "
            "FROM Transaction_Record t "
            "LEFT JOIN Category c ON t.category_id = c.category_id "
            "WHERE t.user_id = ?"
        )
        params = [user['user_id']]
        if selected_category:
            query += ' AND c.category_name = ?'
            params.append(selected_category)
        if selected_month:
            query += " AND strftime('%Y-%m', t.transaction_date) = ?"
            params.append(selected_month)
        if search_query:
            query += " AND LOWER(t.description) LIKE ? ESCAPE '!'"
            params.append(f'%{_like_escape(search_query.lower())}%')
            
        # Get total count for pagination
        count_query = query.replace('SELECT t.*, c.category_name, c.category_type', 'SELECT COUNT(*)')
        total_transactions = conn.execute(count_query, params).fetchone()[0]
        
        # Pagination params
        page = request.args.get('page', 1, type=int)
        per_page = 50
        offset = (page - 1) * per_page
        
        query += ' ORDER BY t.transaction_date DESC, t.transaction_id DESC LIMIT ? OFFSET ?'
        params.extend([per_page, offset])

        # Ensure subscriptions are tagged so we can hide OCR scanner on them
        from ml import subscriptions
        subscriptions.detect_subscriptions(conn, user['user_id'], persist=True)

        rows = conn.execute(query, params).fetchall()
        
        total_pages = (total_transactions + per_page - 1) // per_page
        
        # Load items for transactions
        items_by_txn = {}
        txn_ids = [str(r['transaction_id']) for r in rows]
        if txn_ids:
            placeholders = ','.join('?' * len(txn_ids))
            items = conn.execute(f'SELECT * FROM Receipt_Item WHERE transaction_id IN ({placeholders})', txn_ids).fetchall()
            for it in items:
                items_by_txn.setdefault(it['transaction_id'], []).append(it)
        
        categories = conn.execute(
            'SELECT * FROM Category ORDER BY category_type, category_name').fetchall()
        months = recommendation.available_months(conn, user['user_id'])
        metric = database.latest_metric(conn, 'CATEGORISATION')

        return render_template('transactions.html', transactions=rows,
                               categories=categories, months=months,
                               selected_category=selected_category,
                               selected_month=selected_month,
                               search_query=search_query,
                               metric=metric, target_f1=config.TARGET_F1,
                               items_by_txn=items_by_txn,
                               page=page, total_pages=total_pages)
    finally:
        conn.close()


def _handle_upload(conn, user):
    """Validate and import an uploaded statement (FR-01)."""
    uploaded = request.files.get('file')
    if uploaded is None or not uploaded.filename:
        flash('No file was selected.', 'error')
        return

    filename = secure_filename(uploaded.filename)
    extension = os.path.splitext(filename)[1].lower()
    if extension not in config.ALLOWED_UPLOAD_EXTENSIONS:
        flash('Only .csv bank statements can be uploaded.', 'error')
        return

    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    stamped = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{filename}"
    filepath = os.path.join(config.UPLOAD_FOLDER, stamped)
    uploaded.save(filepath)

    try:
        report = ingestion.import_statement(conn, user['user_id'], filepath)
    except ingestion.CsvValidationError as exc:
        flash(f'Upload rejected: {exc}', 'error')
        os.remove(filepath)
        return
    except Exception as exc:
        flash(f'Import failed and no data was saved: {exc}', 'error')
        return

    pipeline = ingestion.post_import_pipeline(conn, user['user_id'])

    message = (f"Imported {report['imported']} transaction(s) from "
               f"{report['rows']} row(s).")
    if report['duplicates']:
        message += f" {report['duplicates']} duplicate(s) skipped."
    if report.get('merged_email'):
        message += (f" {report['merged_email']} matched purchase(s) already added "
                    "from your email receipts; the bank's details were applied "
                    "and the items kept.")
    if report['salary_rows']:
        message += f" {report['salary_rows']} salary credit(s) identified."
    if pipeline['relabelled']:
        message += (f" {pipeline['relabelled']} previously unlabelled "
                    f"transaction(s) categorised by the retrained model.")
    flash(message, 'success')

    if report['rejected']:
        preview = '; '.join(f'row {line}: {reason}'
                            for line, reason in report['rejected'][:5])
        more = (f' (+{len(report["rejected"]) - 5} more)'
                if len(report['rejected']) > 5 else '')
        flash(f"{len(report['rejected'])} row(s) could not be read — {preview}{more}",
              'warning')

    training = pipeline['training']
    if training['trained'] and training['f1'] is not None:
        verdict = 'meets' if training['f1'] >= config.TARGET_F1 else 'below'
        flash(f"Categorisation model retrained ({training['algorithm']}, "
              f"macro F1 {training['f1']:.3f}, {verdict} the {config.TARGET_F1} "
              f"target).", 'info')
    elif not training['trained']:
        flash(f"Categorisation model not retrained: {training['reason']} "
              f"Keyword rules are labelling transactions in the meantime.", 'info')


def _like_escape(text):
    """Make % and _ in a search term match literally (LIKE ... ESCAPE '!')."""
    return text.replace('!', '!!').replace('%', '!%').replace('_', '!_')


@app.route('/transactions/add', methods=('POST',))
@security.login_required
def add_transaction():
    """Log a single cash or manual transaction directly from the UI / mobile PWA."""
    import hashlib
    description = ' '.join((request.form.get('description') or '').split())[:120]
    raw_date = (request.form.get('transaction_date') or date.today().isoformat()).strip()
    parsed_date = ingestion.parse_date(raw_date)
    amount = ingestion._parse_amount(request.form.get('amount'))
    txn_type = (request.form.get('transaction_type') or 'DEBIT').strip().upper()
    chosen_category = (request.form.get('category_name') or '').strip()

    errors = []
    if not description:
        errors.append('Enter a description or merchant name.')
    if parsed_date is None:
        errors.append('Enter a valid transaction date.')
    else:
        date_err = ingestion.date_out_of_range(parsed_date)
        if date_err:
            errors.append(f'Date {date_err}.')
    if amount is None or amount <= 0:
        errors.append('Amount must be greater than zero.')
    elif amount > config.MAX_TRANSACTION_AMOUNT:
        errors.append(f'Amount exceeds the maximum of '
                      f'{config.MAX_TRANSACTION_AMOUNT:,.0f} {config.CURRENCY}.')
    if txn_type not in ('DEBIT', 'CREDIT'):
        errors.append('Transaction type must be Debit or Credit.')

    if errors:
        for msg in errors:
            flash(msg, 'error')
        return redirect(url_for('transactions'))

    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        date_iso = parsed_date.isoformat()
        amount = round(float(amount), 2)

        cat_row = None
        if chosen_category:
            cat_row = conn.execute(
                'SELECT category_id, category_name FROM Category WHERE category_name = ?',
                (chosen_category,)).fetchone()

        if cat_row:
            category_id = cat_row['category_id']
            assigned_name = cat_row['category_name']
            source = 'user'
            confidence = 1.0
        elif txn_type == 'DEBIT':
            assigned_name, confidence, source = categorization.classify(
                description, amount=amount, transaction_date=date_iso)
            category_id = database.category_id_for(conn, assigned_name)
        else:
            # Same default as a statement import: money in is Income.
            assigned_name = 'Income'
            category_id = database.category_id_for(conn, assigned_name)
            source = 'default'
            confidence = None

        # Every manual entry is unique (two identical bus fares on one day are
        # two fares), and the 'manual-' prefix marks where the row came from.
        nonce = f"{datetime.now().isoformat()}|{description}|{amount}|{os.urandom(4).hex()}"
        import_hash = 'manual-' + hashlib.sha256(nonce.encode('utf-8')).hexdigest()[:40]

        conn.execute(
            'INSERT INTO Transaction_Record (transaction_date, description, amount, '
            'transaction_type, is_salary, is_subscription, category_id, '
            'category_source, category_confidence, import_hash, user_id) '
            'VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)',
            (date_iso, description, amount, txn_type, category_id,
             source, confidence, import_hash, user['user_id']))

        if txn_type == 'CREDIT':
            ingestion.detect_salary(conn, user['user_id'])
        conn.commit()

        # No retrain here: it takes a few seconds, too slow when logging several
        # cash purchases on a phone. A category chosen by hand is stored as a
        # user label, so the next retrain (on import, correction or from
        # Settings) learns from it.
        forecasting.reset_forecast_cache()

        flash(f'Added {txn_type.lower()} "{description}" '
              f'({amount:,.2f} {config.CURRENCY}) under {assigned_name}.', 'success')
        return redirect(url_for('transactions'))
    finally:
        conn.close()


@app.route('/transactions/<int:transaction_id>/category', methods=('POST',))
@security.login_required
def recategorise(transaction_id):
    """Manual category override (FR-03).

    The correction is stored with source 'user', which both protects it from
    being overwritten by the model and gives it extra weight the next time the
    model trains.
    """
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        owned = conn.execute(
            'SELECT transaction_id FROM Transaction_Record '
            'WHERE transaction_id = ? AND user_id = ?',
            (transaction_id, user['user_id'])).fetchone()
        if not owned:
            abort(404)

        category_name = request.form.get('category_name')
        category = conn.execute(
            'SELECT category_id FROM Category WHERE category_name = ?',
            (category_name,)).fetchone()
        if not category:
            flash('Unknown category.', 'error')
            return redirect(request.referrer or url_for('transactions'))

        conn.execute(
            'UPDATE Transaction_Record SET category_id = ?, category_source = ?, '
            'category_confidence = 1.0 WHERE transaction_id = ?',
            (category['category_id'], 'user', transaction_id))
        conn.commit()
        # A correction is a new 'user'-sourced label, so retrain immediately: the
        # model and the forecast reflect it without waiting for a manual retrain.
        result, relabelled, _ = _retrain_and_evaluate(conn, user['user_id'])
        if result['trained'] and relabelled:
            flash(f'Recategorised as {category_name}. The model retrained on your '
                  f'correction and relabelled {relabelled} similar transaction(s).',
                  'success')
        else:
            flash(f'Recategorised as {category_name}. The model has learned from '
                  f'your correction.', 'success')
        return redirect(request.referrer or url_for('transactions'))
    finally:
        conn.close()


# --- Budget ----------------------------------------------------------------

@app.route('/transactions/<int:transaction_id>/receipt/clear', methods=('POST',))
@security.login_required
def clear_receipt(transaction_id):
    conn = database.get_db_connection()
    user = database.get_user(conn)
    
    # Verify ownership
    txn = conn.execute('SELECT transaction_id FROM Transaction_Record WHERE transaction_id = ? AND user_id = ?', 
                       (transaction_id, user['user_id'])).fetchone()
    
    if txn:
        conn.execute('DELETE FROM Receipt_Item WHERE transaction_id = ?', (transaction_id,))
        conn.commit()
        flash('Receipt cleared successfully.')
        
    conn.close()
    return redirect(url_for('transactions'))

@app.route('/transactions/<int:transaction_id>/receipt', methods=('POST',))
@security.login_required
def upload_receipt(transaction_id):
    """Scan an uploaded receipt into itemised lines (scan-receipt feature)."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        txn = conn.execute(
            'SELECT t.*, c.category_name FROM Transaction_Record t '
            'LEFT JOIN Category c ON t.category_id = c.category_id '
            'WHERE t.transaction_id = ? AND t.user_id = ?',
            (transaction_id, user['user_id'])).fetchone()
        if not txn:
            abort(404)

        file = request.files.get('receipt')
        if file is None or not file.filename:
            flash('No receipt file was selected.', 'error')
            return redirect(url_for('transactions'))

        filename = secure_filename(file.filename)
        extension = os.path.splitext(filename)[1].lower()
        if extension not in config.ALLOWED_RECEIPT_EXTENSIONS:
            flash('Receipts must be an image (JPG, PNG, HEIC) or a PDF.', 'error')
            return redirect(url_for('transactions'))

        os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
        stamped = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{filename}"
        upload_path = os.path.join(config.UPLOAD_FOLDER, stamped)
        file.save(upload_path)

        from services.ocr import ReceiptScanError, scan_receipt
        try:
            scan = scan_receipt(upload_path)
        except ReceiptScanError as exc:
            flash(f'Receipt could not be scanned: {exc} You can add the '
                  'items manually instead.', 'error')
            return redirect(url_for('transactions'))
        finally:
            try:
                os.remove(upload_path)
            except OSError:
                pass

        if not scan.items:
            flash('No priced items could be read from that receipt. Try a '
                  'sharper, well-lit photo taken straight on, or add the items '
                  'manually.', 'warning')
            return redirect(url_for('transactions'))

        _insert_receipt_items(conn, transaction_id, scan.items)
        conn.commit()
        flash(f'Receipt scanned: {len(scan.items)} item(s) extracted.', 'success')
        mismatch = _receipt_mismatch(scan, float(txn['amount']))
        if mismatch:
            flash(mismatch, 'warning')
        return redirect(url_for('transactions'))
    finally:
        conn.close()


def _insert_receipt_items(conn, transaction_id, items):
    for item in items:
        conn.execute(
            'INSERT INTO Receipt_Item (transaction_id, item_name, amount, '
            'quantity) VALUES (?, ?, ?, ?)',
            (transaction_id, item['name'], item['price'], item['quantity']))


def _receipt_mismatch(scan, transaction_amount):
    """A warning when the scan doesn't add up, or None when it does.

    OCR can drop or misread lines, so the extracted items are checked against
    the receipt's printed total and the transaction amount, telling the user
    when to review the items.
    """
    tolerance = 0.01
    if scan.total is not None and abs(scan.total - transaction_amount) > tolerance:
        return (f'The receipt total ({scan.total:,.2f}) does not match this '
                f'transaction ({transaction_amount:,.2f}). Check that this is '
                'the right receipt.')
    expected = scan.total if scan.total is not None else transaction_amount
    if abs(scan.items_total - expected) > tolerance:
        return (f'The extracted items add up to {scan.items_total:,.2f} but the '
                f'receipt is for {expected:,.2f}. Some lines may have been '
                'missed or misread; review the items below.')
    return None


@app.route('/transactions/<int:transaction_id>/receipt/items', methods=('POST',))
@security.login_required
def add_receipt_item(transaction_id):
    """Add one receipt line by hand (when OCR fails or misses a line)."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        txn = conn.execute(
            'SELECT transaction_id FROM Transaction_Record '
            'WHERE transaction_id = ? AND user_id = ?',
            (transaction_id, user['user_id'])).fetchone()
        if not txn:
            abort(404)

        name = (request.form.get('item_name') or '').strip()[:60]
        try:
            amount = round(float(request.form.get('amount', '')), 2)
            quantity = int(request.form.get('quantity') or 1)
        except ValueError:
            amount, quantity = None, None
        if (not name or amount is None or not 0 < amount <= 100000
                or not 1 <= quantity <= 999):
            flash('Enter an item name, an amount between 0 and 100,000, and a '
                  'quantity of at least 1.', 'error')
            return redirect(url_for('transactions'))

        _insert_receipt_items(conn, transaction_id, [
            {'name': name, 'price': amount, 'quantity': quantity}])
        conn.commit()
        flash(f'Added "{name}" to the receipt.', 'success')
        return redirect(url_for('transactions'))
    finally:
        conn.close()


@app.route('/transactions/<int:transaction_id>/receipt/items/<int:item_id>/delete',
           methods=('POST',))
@security.login_required
def delete_receipt_item(transaction_id, item_id):
    """Remove one misread receipt line."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        deleted = conn.execute(
            'DELETE FROM Receipt_Item WHERE item_id = ? AND transaction_id IN '
            '(SELECT transaction_id FROM Transaction_Record '
            ' WHERE transaction_id = ? AND user_id = ?)',
            (item_id, transaction_id, user['user_id'])).rowcount
        if not deleted:
            abort(404)
        conn.commit()
        return redirect(url_for('transactions'))
    finally:
        conn.close()

@app.route('/budget')
@security.login_required
def budget():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        context = _dashboard_context(conn, user)
        metric = database.latest_metric(conn, 'FORECASTING')
        return render_template('budget.html', user=user, metric=metric,
                               max_mae_ratio=config.MAX_MAE_RATIO, **context)
    finally:
        conn.close()


# --- Savings ---------------------------------------------------------------

def _goal_form(form):
    """Validate a savings-goal form: (name, amount, target date ISO, errors).

    Shared by adding and editing, so both reject the same bad input; float()
    accepts 'nan' and 'inf', which a bare `<= 0` check lets through.
    """
    errors = []
    goal_name = ' '.join((form.get('goal_name') or '').split())
    if not goal_name:
        errors.append('Goal name is required.')
    elif len(goal_name) > 80:
        errors.append('Goal name must be 80 characters or fewer.')
    try:
        target_amount = round(float(form.get('target_amount') or 0), 2)
        if not target_amount > 0:
            errors.append('Target amount must be greater than zero.')
        elif target_amount > config.MAX_TRANSACTION_AMOUNT:
            errors.append(f'Target amount must be at most '
                          f'{config.MAX_TRANSACTION_AMOUNT:,.0f} {config.CURRENCY}.')
    except (ValueError, OverflowError):
        target_amount = 0.0
        errors.append('Target amount must be a number.')
    target_date = (form.get('target_date') or '').strip()
    try:
        parsed = datetime.strptime(target_date, '%Y-%m-%d').date()
        if parsed <= date.today():
            errors.append('Target date must be in the future.')
        elif parsed.year > date.today().year + 100:
            errors.append('Target date is too far in the future.')
    except ValueError:
        errors.append('Target date is invalid.')
    return goal_name, target_amount, target_date, errors


@app.route('/savings', methods=('GET', 'POST'))
@security.login_required
def savings():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)

        if request.method == 'POST':
            goal_name, target_amount, target_date, errors = _goal_form(request.form)

            if errors:
                for message in errors:
                    flash(message, 'error')
            else:
                conn.execute(
                    'INSERT INTO SavingsGoal (goal_name, target_amount, '
                    'target_date, user_id) VALUES (?, ?, ?, ?)',
                    (goal_name, target_amount, target_date, user['user_id']))
                conn.commit()
                flash(f'Savings goal "{goal_name}" added.', 'success')
            return redirect(url_for('savings'))

        learning = behavioural.learning_status(conn, user)
        budget_view = (recommendation.build_budget(conn, user)
                       if learning['complete'] else None)
        position = recommendation.savings_position(conn, user, budget_view)
        return render_template('savings.html', user=user, savings=position,
                               learning=learning, budget=budget_view)
    finally:
        conn.close()


@app.route('/savings/<int:goal_id>/edit', methods=('POST',))
@security.login_required
def edit_goal(goal_id):
    """Edit an existing savings goal's name, target amount or target date."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        goal = conn.execute(
            'SELECT * FROM SavingsGoal WHERE goal_id = ? AND user_id = ?',
            (goal_id, user['user_id'])).fetchone()
        if not goal:
            abort(404)

        goal_name, target_amount, target_date, errors = _goal_form(request.form)

        if errors:
            for message in errors:
                flash(message, 'error')
            return redirect(url_for('savings'))

        conn.execute(
            'UPDATE SavingsGoal SET goal_name = ?, target_amount = ?, target_date = ? '
            'WHERE goal_id = ? AND user_id = ?',
            (goal_name, target_amount, target_date, goal_id, user['user_id']))
        conn.commit()
        flash(f'Savings goal "{goal_name}" updated.', 'success')
        return redirect(url_for('savings'))
    finally:
        conn.close()


@app.route('/savings/<int:goal_id>/delete', methods=('POST',))
@security.login_required
def delete_goal(goal_id):
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        conn.execute('DELETE FROM SavingsGoal WHERE goal_id = ? AND user_id = ?',
                     (goal_id, user['user_id']))
        conn.commit()
        flash('Savings goal removed.', 'info')
        return redirect(url_for('savings'))
    finally:
        conn.close()


# --- Reports ---------------------------------------------------------------

@app.route('/reports')
@security.login_required
def reports():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        months = recommendation.available_months(conn, user['user_id'])
        selected = request.args.get('month') or (months[0] if months else None)

        report = (recommendation.monthly_report(conn, user, selected)
                  if selected else None)
        return render_template('reports.html', user=user, months=months,
                               selected=selected, report=report)
    finally:
        conn.close()


# --- Household Mock --------------------------------------------------------

@app.route('/household', methods=['GET', 'POST'])
@security.login_required
def household():
    _demo_only()
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        
        # Handle "Invite Partner" mock action
        if request.method == 'POST':
            partner_name = request.form.get('partner_name', 'Alex')
            partner_salary = request.form.get('partner_salary', 5000)
            
            # Check if partner exists
            partner = conn.execute('SELECT * FROM User WHERE user_id != ?', (user['user_id'],)).fetchone()
            if not partner:
                conn.execute(
                    'INSERT INTO User (full_name, salary_amount, salary_day) VALUES (?, ?, ?)',
                    (partner_name, partner_salary, user['salary_day'])
                )
                conn.commit()
                partner = conn.execute('SELECT * FROM User WHERE user_id != ?', (user['user_id'],)).fetchone()
                
                # Generate some mock transactions for the partner
                import random
                from datetime import date
                today = date.today()
                month_prefix = today.strftime('%Y-%m')
                essential_cat = conn.execute("SELECT category_id FROM Category WHERE category_type = 'ESSENTIAL'").fetchone()[0]
                discretionary_cat = conn.execute("SELECT category_id FROM Category WHERE category_type = 'DISCRETIONARY'").fetchone()[0]
                
                # 10 mock transactions for partner
                for _ in range(10):
                    day = str(random.randint(1, 28)).zfill(2)
                    amt = round(random.uniform(20.0, 300.0), 2)
                    cat = random.choice([essential_cat, discretionary_cat])
                    desc = f"Mock Partner Exp {day}"
                    conn.execute(
                        "INSERT INTO Transaction_Record (transaction_date, description, amount, transaction_type, user_id, category_id) "
                        "VALUES (?, ?, ?, 'DEBIT', ?, ?)",
                        (f"{month_prefix}-{day}", desc, amt, partner['user_id'], cat)
                    )
                conn.commit()
                flash(f"{partner_name} has accepted your household invitation!", "success")
                return redirect(url_for('household'))
        
        # Load partner data if exists
        partner = conn.execute('SELECT * FROM User WHERE user_id != ?', (user['user_id'],)).fetchone()
        stats = None
        breakdown = None
        if partner:
            # Calculate who owes who based on ESSENTIAL spending this month
            from datetime import date
            month_str = date.today().strftime('%Y-%m')
            
            def get_essential_spend(uid):
                row = conn.execute(
                    "SELECT SUM(t.amount) as total FROM Transaction_Record t "
                    "JOIN Category c ON t.category_id = c.category_id "
                    "WHERE t.user_id = ? AND t.transaction_type = 'DEBIT' AND c.category_type = 'ESSENTIAL' "
                    "AND strftime('%Y-%m', t.transaction_date) = ?",
                    (uid, month_str)
                ).fetchone()
                return row['total'] or 0.0

            user_spend = get_essential_spend(user['user_id'])
            partner_spend = get_essential_spend(partner['user_id'])
            total_spend = user_spend + partner_spend
            fair_share = total_spend / 2
            
            owes = 0
            ower = ""
            if user_spend > partner_spend:
                owes = user_spend - fair_share
                ower = partner['full_name']
                verb = "owes"
                owee = "you"
            else:
                owes = partner_spend - fair_share
                ower = "You"
                verb = "owe"
                owee = partner['full_name']

            stats = {
                'user_spend': user_spend,
                'partner_spend': partner_spend,
                'total': total_spend,
                'fair_share': fair_share,
                'ower': ower,
                'verb': verb,
                'owee': owee,
                'owes_amount': owes
            }
            
            # Fetch category breakdown for the table
            breakdown_query = """
                SELECT c.category_name, 
                       SUM(CASE WHEN t.user_id = ? THEN t.amount ELSE 0 END) as user_spent,
                       SUM(CASE WHEN t.user_id = ? THEN t.amount ELSE 0 END) as partner_spent,
                       SUM(t.amount) as total_spent
                FROM Transaction_Record t
                JOIN Category c ON t.category_id = c.category_id
                WHERE t.transaction_type = 'DEBIT' AND c.category_type = 'ESSENTIAL'
                AND strftime('%Y-%m', t.transaction_date) = ?
                GROUP BY c.category_name
                ORDER BY total_spent DESC
            """
            breakdown = conn.execute(breakdown_query, (user['user_id'], partner['user_id'], month_str)).fetchall()

        return render_template('household.html', user=user, partner=partner, stats=stats, breakdown=breakdown)
    finally:
        conn.close()


@app.route('/household/leave', methods=['POST'])
@security.login_required
def leave_household():
    """Dissolve the household: remove the mock partner and all of their data,
    returning the app to its single-user state so the demo can be reset and run
    again."""
    _demo_only()
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        partner = conn.execute('SELECT * FROM User WHERE user_id != ?',
                               (user['user_id'],)).fetchone()
        if not partner:
            flash("You are not in a household.", "info")
            return redirect(url_for('household'))

        # Remove the partner's data in FK order (receipt items -> transactions ->
        # the partner profile). Only the partner is touched; the primary user's
        # data is never affected.
        conn.execute(
            'DELETE FROM Receipt_Item WHERE transaction_id IN '
            '(SELECT transaction_id FROM Transaction_Record WHERE user_id = ?)',
            (partner['user_id'],))
        conn.execute('DELETE FROM Transaction_Record WHERE user_id = ?',
                     (partner['user_id'],))
        conn.execute('DELETE FROM SavingsGoal WHERE user_id = ?',
                     (partner['user_id'],))
        conn.execute('DELETE FROM BudgetRecommendation WHERE user_id = ?',
                     (partner['user_id'],))
        conn.execute('DELETE FROM User WHERE user_id = ?', (partner['user_id'],))
        conn.commit()
        flash(f"{partner['full_name']} has left the household — their profile and "
              f"transactions were removed.", "info")
        return redirect(url_for('household'))
    finally:
        conn.close()


# --- Auto-Fetch Receipts ---------------------------------------------------

IMAP_EMAIL_SETTING = 'imap_email'


@app.route('/auto-fetch-receipts', methods=['POST'])
@security.login_required
def auto_fetch_receipts():
    """Read emailed receipts from the user's inbox: itemise the transactions
    they match, and add the purchases the bank hasn't shown yet.

    The address and app password come from the Settings form (or, failing
    that, the IMAP_EMAIL / IMAP_PASSWORD environment variables). The password
    is used for this one sign-in and never stored or logged; only the address
    is remembered, to pre-fill the form. Receipts are matched to transactions
    by merchant, date and amount (services/email_receipts.py); only receipts
    that were actually found and read are used. A receipt with no matching
    transaction becomes a new one, which a later statement upload merges with
    the bank's row rather than duplicating (services/ingestion.py).
    """
    _demo_only()
    import os
    import imaplib
    import socket
    from services import email_receipts, fx
    from services.imap_fetcher import connect_imap, parse_transaction_date
    from services.ocr import EMAIL_RECEIPT_MERCHANTS, emails_receipt, scan_receipt
    from ml.categorization import normalise

    email_address = (request.form.get('imap_email') or
                     os.environ.get('IMAP_EMAIL') or '').strip()
    app_password = request.form.get('imap_password') or os.environ.get('IMAP_PASSWORD') or ''
    if not (email_address and app_password):
        flash('Email receipt scanning is not set up. Enter your email address '
              'and an app password under Settings → Email receipts.', 'error')
        return redirect(url_for('settings'))

    conn = database.get_db_connection()
    try:
        user = database.get_user(conn)
        database.set_setting(conn, IMAP_EMAIL_SETTING, email_address)
        conn.commit()

        unitemised = conn.execute("""
            SELECT t.transaction_id, t.description, t.amount, t.transaction_date
            FROM Transaction_Record t
            WHERE t.user_id = ? AND t.transaction_type = 'DEBIT'
              AND t.is_salary = 0
              AND NOT EXISTS (SELECT 1 FROM Receipt_Item r
                              WHERE r.transaction_id = t.transaction_id)
            ORDER BY t.transaction_date DESC
            LIMIT 40
        """, (user['user_id'],)).fetchall()
        # Only merchants that email a digital receipt are worth searching for.
        eligible = [t for t in unitemised if emails_receipt(t['description'])][:15]

        try:
            mail = connect_imap(email_address, app_password)
        except imaplib.IMAP4.error:
            flash('Your mail provider refused the sign-in. Check the address, '
                  'and use an app password rather than your normal password '
                  '(for Gmail: Google Account → Security → App passwords).', 'error')
            return redirect(url_for('settings'))
        except (OSError, socket.timeout):
            flash('Could not reach your mail server. Check your internet '
                  'connection and try again.', 'error')
            return redirect(url_for('settings'))
        finally:
            app_password = None  # used once; drop the reference

        itemised, review, unreadable, ambiguous, not_found = [], [], [], [], 0
        used_messages = set()
        sweep, stored = email_receipts.SweepResult(), {'added': [], 'itemised': []}
        # Fetches exchange rates only if a foreign-currency receipt turns up.
        converter = fx.Converter(conn)
        try:
            for txn in eligible:
                text = normalise(txn['description'])
                # Longest keyword first, so 'uber eats' wins over 'uber'.
                keyword = max((kw for kw in EMAIL_RECEIPT_MERCHANTS if kw in text),
                              key=len)
                try:
                    result = email_receipts.find_receipt_for(
                        mail, keyword, parse_transaction_date(txn['transaction_date']),
                        float(txn['amount']), scan_receipt, converter=converter)
                except Exception as error:  # one bad search must not 500 the scan
                    app.logger.warning('Email receipt search failed for %s: %s',
                                       keyword, error)
                    result = email_receipts.EmailReceiptResult(email_receipts.NOT_FOUND)

                label = f"{txn['description']} ({float(txn['amount']):,.2f})"
                if result.usable:
                    used_messages.add(result.message_key)
                    _insert_receipt_items(conn, txn['transaction_id'], result.scan.items)
                    (itemised if result.status == email_receipts.MATCHED
                     else review).append(label)
                elif result.status == email_receipts.UNREADABLE:
                    unreadable.append(label)
                elif result.status == email_receipts.AMBIGUOUS:
                    ambiguous.append(label)
                else:
                    not_found += 1

            # Pass 2: receipts for purchases the bank hasn't shown yet.
            try:
                sweep = email_receipts.read_new_receipts(
                    mail, scan_receipt, skip_keys=used_messages, converter=converter)
            except Exception as error:  # an odd mailbox must not 500 the scan
                app.logger.warning('Email receipt sweep failed: %s', error)
            stored = email_receipts.add_receipts(conn, user['user_id'], sweep.receipts)
        finally:
            try:
                mail.logout()
            except Exception:
                pass

        conn.commit()
        itemised += stored['itemised']

        if itemised:
            flash(f"Itemised {len(itemised)} emailed receipt(s) that match the "
                  f"amount: {', '.join(itemised)}.", 'success')
        if review:
            flash(f"Itemised {len(review)} receipt(s) whose total differs from "
                  f"the bank amount. Check them: {', '.join(review)}.", 'warning')
        if ambiguous:
            flash(f"Several receipts were close to {', '.join(ambiguous)} and "
                  "none matched the amount, so nothing was added. Add those "
                  "items by hand.", 'warning')
        if unreadable:
            flash(f"Found emails but could not read the items for: "
                  f"{', '.join(unreadable)}. Add those items by hand.", 'warning')
        if stored['added']:
            flash(f"Added {len(stored['added'])} purchase(s) from your email "
                  f"receipts: {', '.join(stored['added'])}. When your bank "
                  "statement includes them, uploading it will merge them rather "
                  "than add them twice.", 'success')
        if converter.as_of:  # set only when a conversion was needed
            note = (f"Receipts in another currency were converted to "
                    f"{config.CURRENCY} at the rate of {converter.as_of}")
            note += (" (the rate service couldn't be reached, so the last saved "
                     "rates were used)" if converter.stale else "")
            flash(note + ". These amounts are estimates: uploading your bank "
                  "statement replaces them with what the bank charged.", 'info')
        if sweep.free_trials:
            flash(f"Found free-trial sign-up(s), not added because nothing was "
                  f"charged yet: {', '.join(sorted(set(sweep.free_trials)))}. "
                  "The first charge will be picked up when its receipt arrives.",
                  'info')
        if sweep.foreign_currency:
            flash(f"Couldn't convert receipt(s) priced in another currency, "
                  f"because no exchange rate was available: "
                  f"{', '.join(sweep.foreign_currency)}. Scan again when you're "
                  f"online, or they will appear when your bank statement is "
                  f"uploaded.", 'warning')
        if not (itemised or review or ambiguous or unreadable or stored['added']
                or sweep.foreign_currency or sweep.free_trials):
            flash('No new receipt emails were found in the last 30 days.', 'info')
    finally:
        conn.close()

    return redirect(url_for('transactions'))

# --- Settings --------------------------------------------------------------

def _extract_per_category_mae_ratio(metric):
    """The per-category forecast error ratio (NFR-03) of a FORECASTING metric.

    Read from its own column; rows recorded before that column existed carry
    it only in the notes text, so those fall back to parsing the notes.
    """
    if not metric:
        return None
    if 'per_category_mae_ratio' in metric.keys() and             metric['per_category_mae_ratio'] is not None:
        return float(metric['per_category_mae_ratio'])
    import re
    match = re.search(r'per-category MAE ratio ([0-9.]+)', str(metric['notes'] or ''))
    try:
        return float(match.group(1)) if match else None
    except ValueError:
        return None


@app.route('/settings')
@security.login_required
def settings():
    from services.ocr import ocr_status
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        f_metric = database.latest_metric(conn, 'FORECASTING')
        return render_template(
            'settings.html', user=user,
            categorisation=database.latest_metric(conn, 'CATEGORISATION'),
            forecasting_metric=f_metric,
            per_category_mae_ratio=_extract_per_category_mae_ratio(f_metric),
            target_f1=config.TARGET_F1, max_mae_ratio=config.MAX_MAE_RATIO,
            db_path=config.DATABASE_PATH, ocr_status=ocr_status(),
            imap_email=(database.get_setting(conn, IMAP_EMAIL_SETTING)
                        or os.environ.get('IMAP_EMAIL', '')))
    finally:
        conn.close()


def _retrain_and_evaluate(conn, user_id):
    """Retrain the categoriser, relabel anything still uncategorised, re-run the
    forecast hold-out and refresh subscription detection.

    Shared by the manual Settings action and the automatic retrain a user
    correction triggers, so both run exactly the same steps against the same
    functions. Returns (training_result, relabelled_count, forecast_evaluation).
    """
    result = categorization.train_categorization_model(conn, user_id)
    relabelled = 0
    if result['trained']:
        relabelled = categorization.recategorise_uncategorised(conn, user_id)
    evaluation = forecasting.backtest(conn, user_id)
    subscriptions.detect_subscriptions(conn, user_id)
    # A correction may have relabelled transactions, which changes the forecast
    # inputs; drop the memoised forecasts so the next page recomputes.
    forecasting.reset_forecast_cache()
    return result, relabelled, evaluation


@app.route('/settings/retrain', methods=('POST',))
@security.login_required
def retrain():
    """Retrain the categoriser and re-run the forecast hold-out evaluation."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        result, relabelled, evaluation = _retrain_and_evaluate(conn, user['user_id'])
        if result['trained']:
            f1_text = (f"{result['f1']:.3f}" if result['f1'] is not None
                       else 'not cross-validated')
            flash(f"Retrained {result['algorithm']} on {result['samples']} "
                  f"labelled transactions (macro F1 {f1_text}). "
                  f"{relabelled} transaction(s) relabelled.", 'success')
        else:
            flash(f"Not retrained: {result['reason']}", 'warning')

        if evaluation['evaluated']:
            verdict = ('within' if evaluation['meets_target'] else 'outside')
            flash(f"Forecast hold-out on {evaluation['test_month']}: monthly-total "
                  f"MAE {evaluation['mae']:.2f} {config.CURRENCY} "
                  f"({evaluation['mae_ratio'] * 100:.1f}% of actual monthly spend, "
                  f"{verdict} the {config.MAX_MAE_RATIO * 100:.0f}% target).",
                  'info')
        else:
            flash(f"Forecast not evaluated: {evaluation['reason']}", 'info')

        return redirect(url_for('settings'))
    finally:
        conn.close()


@app.route('/settings/profile', methods=('POST',))
@security.login_required
def update_profile():
    """Update the user's name, monthly salary or pay day (e.g. after a promotion).

    These three details are also what the offline password reset checks, so a
    change needs the current password, like Change password does. Otherwise
    anyone at an unlocked device could set them to values they know, sign out
    and reset the password through /reset-password.
    """
    full_name = ' '.join((request.form.get('full_name') or '').split())
    errors = []
    if not full_name or len(full_name) > 80:
        errors.append('Enter your name (up to 80 characters).')

    try:
        salary_amount = round(float(request.form.get('salary_amount') or 0), 2)
        if not salary_amount > 0:  # also rejects NaN
            errors.append('Salary amount must be greater than zero.')
        elif salary_amount > config.MAX_TRANSACTION_AMOUNT:
            errors.append(f'Salary amount must be at most '
                          f'{config.MAX_TRANSACTION_AMOUNT:,.0f} {config.CURRENCY}.')
    except (ValueError, OverflowError):
        salary_amount = 0.0
        errors.append('Salary amount must be a valid number.')

    try:
        salary_day = int(request.form.get('salary_day') or 0)
        if not 1 <= salary_day <= 31:
            errors.append('Pay day must be between 1 and 31.')
    except ValueError:
        salary_day = 0
        errors.append('Pay day must be a whole number between 1 and 31.')

    # No #anchor: the result is flashed at the top of the page.
    back = redirect(url_for('settings'))
    if errors:
        for message in errors:
            flash(message, 'error')
        return back

    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        old_salary = float(user['salary_amount'])
        old_day = int(user['salary_day'])
        income_changed = abs(salary_amount - old_salary) >= 0.01 or salary_day != old_day
        if not income_changed and full_name == user['full_name']:
            flash('Nothing to update: those are already your profile details.', 'info')
            return back

        # Same throttle as sign-in and Change password.
        client = request.remote_addr or 'local'
        wait = security.login_lockout_remaining(client)
        if wait:
            flash(f'Too many incorrect attempts. Try again in {wait} seconds.', 'error')
            return back
        if not security.verify_password(conn, request.form.get('current_password')):
            security.record_failed_login(client)
            flash('Enter your current password to save profile changes.', 'error')
            return back
        security.reset_login_attempts(client)

        conn.execute(
            'UPDATE User SET full_name = ?, salary_amount = ?, salary_day = ? '
            'WHERE user_id = ?',
            (full_name, salary_amount, salary_day, user['user_id']))

        recalculated = False
        if income_changed:
            # Re-run salary detection so any recently imported paycheck at the
            # new salary level is flagged, while keeping historical paychecks.
            ingestion.detect_salary(conn, user['user_id'])
            updated_user = database.get_user(conn)
            if behavioural.learning_status(conn, updated_user)['complete']:
                budget_data = recommendation.build_budget(conn, updated_user)
                recommendation.persist_recommendations(
                    conn, updated_user['user_id'], budget_data)
                recalculated = True
        conn.commit()

        if income_changed:
            message = (f'Income updated to {salary_amount:,.2f} {config.CURRENCY}, '
                       f'paid on day {salary_day}. ')
            message += ('Your budget recommendations and savings capacity have been '
                        'recalculated.' if recalculated else
                        'Your budget will use it once the learning phase is complete.')
        else:
            message = 'Profile updated.'
        flash(message + ' Use these details if you ever need to reset your password.',
              'success')
        return back
    finally:
        conn.close()


@app.route('/settings/categories/add', methods=('POST',))
@security.login_required
def add_category():
    """Add a user-defined spending category (for manual assignment)."""
    import sqlite3
    category_name = request.form.get('category_name', '').strip().title()
    category_type = request.form.get('category_type', '')

    if not category_name or len(category_name) > 40 or \
            category_type not in ('ESSENTIAL', 'DISCRETIONARY'):
        flash('Enter a category name (up to 40 characters) and choose a type.',
              'error')
        return redirect(url_for('settings'))

    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO Category (category_name, category_type) '
                     'VALUES (?, ?)', (category_name, category_type))
        conn.commit()
        flash(f"Category '{category_name}' added.", 'success')
    except sqlite3.IntegrityError:
        # The only expected failure: the UNIQUE constraint on category_name.
        flash(f"Category '{category_name}' already exists.", 'warning')
    finally:
        conn.close()
    return redirect(url_for('settings'))

@app.route('/settings/password', methods=('POST',))
@security.login_required
def change_password():
    """Change the password while signed in from the Settings screen."""
    # No #anchor: the result is flashed at the top of the page, where the user
    # lands after the redirect.
    back = redirect(url_for('settings'))
    conn = get_db_connection()
    try:
        # Shares the login throttle: an unattended, signed-in device must not
        # let someone try password after password to learn the real one.
        client = request.remote_addr or 'local'
        wait = security.login_lockout_remaining(client)
        if wait:
            flash(f'Too many incorrect attempts. Try again in {wait} seconds.',
                  'error')
            return back
        if not security.verify_password(conn, request.form.get('current_password')):
            security.record_failed_login(client)
            flash('Current password is incorrect.', 'error')
            return back
        security.reset_login_attempts(client)

        new_password = request.form.get('new_password') or ''
        if new_password != (request.form.get('password_confirm') or ''):
            flash('The new passwords do not match.', 'error')
            return back
        if security.verify_password(conn, new_password):
            flash('The new password is the same as the current one. Choose a '
                  'different password.', 'error')
            return back

        ok, err = security.set_password(conn, new_password)
        if not ok:
            flash(err, 'error')
            return back

        # set_password signed out every session; keep this one signed in.
        security.start_session(conn)
        flash('Your password has been updated. Any other device using the app '
              'will need to sign in again.', 'success')
        return back
    finally:
        conn.close()


@app.route('/settings/export')
@security.login_required
def export_database():
    """One-click backup of the database as a .zip (section 5.11).

    A .zip rather than a bare .db, because phones don't recognise .db files
    (see services/backup.py).
    """
    from io import BytesIO
    from services import backup
    data, name = backup.backup_zip(config.DATABASE_PATH, backup.timestamp())
    return send_file(BytesIO(data), mimetype='application/zip',
                     as_attachment=True, download_name=name)


@app.route('/settings/restore', methods=('POST',))
@security.login_required
def restore_database():
    """Replace all data with an uploaded backup (.zip, or a .db from older
    backups), after keeping a safety copy of the current data.

    Needs the current password: a restore replaces everything, including the
    password itself.
    """
    import tempfile
    from services import backup

    uploaded = request.files.get('backup_file')
    if uploaded is None or not uploaded.filename:
        flash('Choose the backup file to restore.', 'error')
        return redirect(url_for('settings'))

    conn = get_db_connection()
    try:
        client = request.remote_addr or 'local'
        wait = security.login_lockout_remaining(client)
        if wait:
            flash(f'Too many incorrect attempts. Try again in {wait} seconds.', 'error')
            return redirect(url_for('settings'))
        if not security.verify_password(conn, request.form.get('current_password')):
            security.record_failed_login(client)
            flash('Enter your current password to restore a backup.', 'error')
            return redirect(url_for('settings'))
        security.reset_login_attempts(client)
    finally:
        conn.close()

    stamp = backup.timestamp()
    with tempfile.TemporaryDirectory() as work:
        upload_path = os.path.join(work, 'upload')
        uploaded.save(upload_path)
        try:
            db_file = backup.unpack(upload_path, work)
            backup.validate(db_file)
            safety = backup.restore(db_file, config.DATABASE_PATH, stamp)
        except backup.BackupError as error:
            flash(str(error), 'error')
            return redirect(url_for('settings'))

    # Bring an older backup up to the current schema, then rebuild everything
    # derived from the old data.
    init_db()
    categorization.reset_model_cache()
    forecasting.reset_forecast_cache()
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        _retrain_and_evaluate(conn, user['user_id'])
        conn.commit()
        security.start_session(conn)
    finally:
        conn.close()

    message = ('Backup restored. From now on, sign in with the password that was '
               'in use when this backup was made.')
    if safety:
        message += (' Your previous data was kept as '
                    f'{os.path.basename(safety)} next to the database.')
    flash(message, 'success')
    return redirect(url_for('settings'))


@app.route('/sw.js')
def service_worker():
    """Serve the PWA Service Worker from the root path so its scope covers '/'."""
    response = send_file(os.path.join(app.static_folder, 'sw.js'),
                         mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/offline')
def offline():
    """Offline fallback screen pre-cached by the PWA Service Worker."""
    return render_template('offline.html')


@app.errorhandler(413)
def too_large(_error):
    flash(f'That file is larger than the '
          f'{config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit.', 'error')
    return redirect(url_for('transactions')), 413


def _warm_forecast_cache():
    """Pre-compute the forecasts once at startup so the first page a user opens
    is served from cache (NFR-01).

    The first forecast of a fresh process also pays a one-off cost importing the
    scientific stack (statsmodels/scikit-learn) and fitting every category's
    model. Doing it here, off the request path, keeps every user-facing page
    load well under the three-second target. It runs in a daemon thread so the
    server still starts accepting connections immediately, and it is best-effort:
    any failure (e.g. no user set up yet) is swallowed and the forecast simply
    computes lazily on first use instead.
    """
    try:
        conn = database.get_db_connection()
        try:
            user = database.get_user(conn)
            if user is not None:
                forecasting.generate_forecasts(conn, user['user_id'])
        finally:
            conn.close()
    except Exception:
        pass


if __name__ == '__main__':
    init_db()
    threading.Thread(target=_warm_forecast_cache, daemon=True).start()
    # Local-only by default (section 5.9): bound to 127.0.0.1 unless FLASK_HOST
    # is set (e.g. FLASK_HOST=0.0.0.0 to reach the PWA from a mobile phone on
    # the same Wi-Fi network). Set FLASK_SSL=adhoc for local HTTPS when testing
    # PWA installation over LAN.
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    ssl_mode = os.environ.get('FLASK_SSL', '').strip().lower()
    ssl_ctx = 'adhoc' if ssl_mode in ('1', 'true', 'adhoc') else None
    # SSL_CERT/SSL_KEY: a certificate pair the phone actually trusts (the mkcert
    # path in docs/mobile-install.md). Browsers only register the PWA service
    # worker over HTTPS with a trusted certificate — an ad-hoc self-signed one
    # is not trusted, so this is the route that makes Wi-Fi install work.
    ssl_cert, ssl_key = os.environ.get('SSL_CERT'), os.environ.get('SSL_KEY')
    if ssl_cert and ssl_key:
        ssl_ctx = (ssl_cert, ssl_key)
    app.run(host=host, port=int(os.environ.get('PORT', 5000)),
            debug=os.environ.get('FLASK_DEBUG') == '1',
            ssl_context=ssl_ctx)
