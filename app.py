"""Presentation Layer (section 3.6.5).

Flask routes for the Salary-Linked Expense Tracker and Budget Advisor. Every
route is thin: it reads request state, calls into services/ or ml/, and renders
a template. Business logic lives in those modules so that each layer can be
tested and replaced independently (NFR-07).
"""
import os
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

app = Flask(__name__)
app.secret_key = security.load_or_create_secret_key()
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_BYTES
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(
    minutes=config.SESSION_TIMEOUT_MINUTES)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


@app.context_processor
def inject_globals():
    return {'currency': config.CURRENCY, 'now': datetime.now(),
            'session_timeout': config.SESSION_TIMEOUT_MINUTES,
            'nudge_threshold': int(config.NUDGE_THRESHOLD * 100)}


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
            if security.verify_password(conn, request.form.get('password')):
                security.start_session()
                destination = request.args.get('next')
                # Only allow same-site relative redirects.
                if destination and destination.startswith('/'):
                    return redirect(destination)
                return redirect(url_for('dashboard'))
            flash('Incorrect password.', 'error')
        return render_template('login.html')
    finally:
        conn.close()


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been signed out.', 'info')
    return redirect(url_for('login'))


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
    if request.endpoint in {'setup', 'login', 'static', None}:
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
    if learning['complete']:
        budget = recommendation.build_budget(conn, user)
        recommendation.persist_recommendations(conn, user['user_id'], budget)
        nudges = recommendation.generate_nudges(budget, cycle, subs)

    return {'learning': learning, 'cycle': cycle, 'subscriptions': subs,
            'budget': budget, 'nudges': nudges}


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
        query += ' ORDER BY t.transaction_date DESC, t.transaction_id DESC LIMIT 500'

        rows = conn.execute(query, params).fetchall()
        categories = conn.execute(
            'SELECT * FROM Category ORDER BY category_type, category_name').fetchall()
        months = recommendation.available_months(conn, user['user_id'])
        metric = database.latest_metric(conn, 'CATEGORISATION')

        return render_template('transactions.html', transactions=rows,
                               categories=categories, months=months,
                               selected_category=selected_category,
                               selected_month=selected_month, metric=metric,
                               target_f1=config.TARGET_F1)
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
        report = ingestion.import_statement(
            conn, user['user_id'], filepath,
            salary_amount=float(user['salary_amount']))
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
        flash(f'Recategorised as {category_name}. The model will learn from '
              f'this correction on the next retrain.', 'success')
        return redirect(request.referrer or url_for('transactions'))
    finally:
        conn.close()


# --- Budget ----------------------------------------------------------------

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

@app.route('/savings', methods=('GET', 'POST'))
@security.login_required
def savings():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)

        if request.method == 'POST':
            errors = []
            goal_name = (request.form.get('goal_name') or '').strip()
            if not goal_name:
                errors.append('Goal name is required.')
            try:
                target_amount = float(request.form.get('target_amount') or 0)
                if target_amount <= 0:
                    errors.append('Target amount must be greater than zero.')
            except ValueError:
                target_amount = 0
                errors.append('Target amount must be a number.')

            target_date = request.form.get('target_date') or ''
            try:
                parsed = datetime.strptime(target_date, '%Y-%m-%d').date()
                if parsed <= date.today():
                    errors.append('Target date must be in the future.')
            except ValueError:
                errors.append('Target date is invalid.')

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


# --- Settings --------------------------------------------------------------

@app.route('/settings')
@security.login_required
def settings():
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        return render_template(
            'settings.html', user=user,
            categorisation=database.latest_metric(conn, 'CATEGORISATION'),
            forecasting_metric=database.latest_metric(conn, 'FORECASTING'),
            target_f1=config.TARGET_F1, max_mae_ratio=config.MAX_MAE_RATIO,
            db_path=config.DATABASE_PATH)
    finally:
        conn.close()


@app.route('/settings/retrain', methods=('POST',))
@security.login_required
def retrain():
    """Retrain the categoriser and re-run the forecast hold-out evaluation."""
    conn = get_db_connection()
    try:
        user = database.get_user(conn)
        result = categorization.train_categorization_model(conn, user['user_id'])
        if result['trained']:
            relabelled = categorization.recategorise_uncategorised(conn, user['user_id'])
            f1_text = (f"{result['f1']:.3f}" if result['f1'] is not None
                       else 'not cross-validated')
            flash(f"Retrained {result['algorithm']} on {result['samples']} "
                  f"labelled transactions (macro F1 {f1_text}). "
                  f"{relabelled} transaction(s) relabelled.", 'success')
        else:
            flash(f"Not retrained: {result['reason']}", 'warning')

        evaluation = forecasting.backtest(conn, user['user_id'])
        if evaluation['evaluated']:
            verdict = ('within' if evaluation['meets_target'] else 'outside')
            flash(f"Forecast hold-out on {evaluation['test_month']}: MAE "
                  f"{evaluation['mae']:.2f} {config.CURRENCY} "
                  f"({evaluation['mae_ratio'] * 100:.1f}% of mean actual, "
                  f"{verdict} the {config.MAX_MAE_RATIO * 100:.0f}% target).",
                  'info')
        else:
            flash(f"Forecast not evaluated: {evaluation['reason']}", 'info')

        subscriptions.detect_subscriptions(conn, user['user_id'])
        return redirect(url_for('settings'))
    finally:
        conn.close()


@app.route('/settings/export')
@security.login_required
def export_database():
    """One-click local backup of the SQLite file (section 5.11)."""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return send_file(config.DATABASE_PATH, as_attachment=True,
                     download_name=f'expense_tracker-backup-{stamp}.db')


@app.errorhandler(413)
def too_large(_error):
    flash(f'That file is larger than the '
          f'{config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit.', 'error')
    return redirect(url_for('transactions')), 413


if __name__ == '__main__':
    init_db()
    # Local-only by design (section 5.9): bound to the loopback interface so the
    # dashboard is not reachable from other machines on the network.
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5000)),
            debug=os.environ.get('FLASK_DEBUG') == '1')
