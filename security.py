"""Security helpers (section 5.9).

Security was the dominant theme in the Q14 survey responses, and the current
prototype had none of it. This module provides the three measures the design
commits to: a password on every launch, a session that times out after ten
minutes of inactivity, and a secret key that is not a literal in the source.

The password hash lives in the AppSetting table using Werkzeug's PBKDF2
implementation. No plaintext password is ever written to disk.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import database

PASSWORD_KEY = 'password_hash'
NOTICE_KEY = 'data_notice_acknowledged'
MIN_PASSWORD_LENGTH = 8


def load_or_create_secret_key():
    """Persist a random secret key so sessions survive a restart.

    A hardcoded key in the source would let anyone with the repository forge a
    session cookie.
    """
    path = config.SECRET_KEY_FILE
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as handle:
            key = handle.read().strip()
            if key:
                return key

    key = secrets.token_hex(32)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best effort; Windows ACLs are handled by the user profile
    return key


def password_is_set(conn):
    return database.get_setting(conn, PASSWORD_KEY) is not None


def set_password(conn, password):
    """Store a new password hash. Returns (ok, error_message)."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return False, (f'Password must be at least {MIN_PASSWORD_LENGTH} '
                       'characters long.')
    database.set_setting(conn, PASSWORD_KEY, generate_password_hash(password))
    conn.commit()
    return True, None


def verify_password(conn, password):
    stored = database.get_setting(conn, PASSWORD_KEY)
    if not stored:
        return False
    return check_password_hash(stored, password or '')


def notice_acknowledged(conn):
    return database.get_setting(conn, NOTICE_KEY) == '1'


def acknowledge_notice(conn):
    database.set_setting(conn, NOTICE_KEY, '1')
    conn.commit()


def start_session():
    session.clear()
    session['authenticated'] = True
    touch_session()


def touch_session():
    session['last_seen'] = datetime.now(timezone.utc).isoformat()
    session.permanent = True


def session_expired():
    """True when the session has been idle past the configured timeout."""
    if not session.get('authenticated'):
        return False
    last_seen = session.get('last_seen')
    if not last_seen:
        return True
    try:
        seen_at = datetime.fromisoformat(last_seen)
    except ValueError:
        return True
    timeout = timedelta(minutes=config.SESSION_TIMEOUT_MINUTES)
    return datetime.now(timezone.utc) - seen_at > timeout


def login_required(view):
    """Gate a view behind the password, refreshing the idle timer on each hit."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.path))
        if session_expired():
            session.clear()
            flash(f'Signed out after {config.SESSION_TIMEOUT_MINUTES} minutes '
                  'of inactivity.', 'warning')
            return redirect(url_for('login'))
        touch_session()
        return view(*args, **kwargs)
    return wrapped
