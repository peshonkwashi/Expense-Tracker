"""Data Storage Layer (section 3.6.2).

Owns the SQLite connection, schema creation and forward-only migrations.
All application code reaches the database through this module.
"""
import os
import sqlite3

import config

# Columns added after the first release. Replayed on every launch so an
# existing expense_tracker.db is upgraded in place without losing data.
_MIGRATIONS = {
    'User': [
        ('learning_started_on', 'DATE'),
    ],
    'Transaction_Record': [
        ('is_subscription', 'INTEGER NOT NULL DEFAULT 0'),
        ('category_source', "TEXT DEFAULT 'default'"),
        ('category_confidence', 'REAL'),
        ('import_hash', 'TEXT'),
        ('created_at', 'TIMESTAMP'),
    ],
    'BudgetRecommendation': [
        ('forecast_method', 'TEXT'),
        ('explanation', 'TEXT'),
        ('generated_at', 'TIMESTAMP'),
    ],
    'SavingsGoal': [
        ('created_at', 'TIMESTAMP'),
    ],
    'ModelMetrics': [
        ('algorithm', 'TEXT'),
        ('mae_ratio', 'REAL'),
        ('sample_count', 'INTEGER'),
        ('notes', 'TEXT'),
    ],
}

_INDEXES = [
    ('idx_txn_user_date', 'Transaction_Record(user_id, transaction_date)'),
    ('idx_txn_category', 'Transaction_Record(category_id)'),
    ('idx_txn_type', 'Transaction_Record(transaction_type)'),
    ('idx_goal_user', 'SavingsGoal(user_id, target_date)'),
    ('idx_metrics_type', 'ModelMetrics(model_type, trained_at)'),
]


def get_db_connection():
    """Return a Row-factory connection with foreign key enforcement on."""
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _existing_columns(conn, table):
    return {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _apply_migrations(conn):
    for table, columns in _MIGRATIONS.items():
        if not _table_exists(conn, table):
            continue
        present = _existing_columns(conn, table)
        for name, ddl in columns:
            if name not in present:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

    # Give pre-migration rows a stable dedup key so re-uploads still work.
    if _table_exists(conn, 'Transaction_Record'):
        conn.execute(
            "UPDATE Transaction_Record SET import_hash = 'legacy-' || transaction_id "
            "WHERE import_hash IS NULL"
        )


def _ensure_indexes(conn):
    for name, target in _INDEXES:
        conn.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {target}')
    # Deduplication guarantee for FR-01. Partial index keeps NULL hashes legal.
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_import_hash '
        'ON Transaction_Record(user_id, import_hash) WHERE import_hash IS NOT NULL'
    )


def init_db():
    """Create the database if absent and bring an existing one up to date."""
    os.makedirs(config.MODEL_DIR, exist_ok=True)
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)

    conn = get_db_connection()
    try:
        with open(config.SCHEMA_PATH, 'r', encoding='utf-8') as handle:
            conn.executescript(handle.read())
        _apply_migrations(conn)
        _ensure_indexes(conn)
        conn.commit()
    finally:
        conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM AppSetting WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(conn, key, value):
    conn.execute(
        'INSERT INTO AppSetting (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value)),
    )


def get_user(conn):
    """Single-user prototype: the first (and only) profile."""
    return conn.execute('SELECT * FROM User ORDER BY user_id LIMIT 1').fetchone()


def category_map(conn):
    """category_name -> Row(category_id, category_name, category_type)."""
    return {row['category_name']: row for row in conn.execute('SELECT * FROM Category')}


def category_id_for(conn, name):
    row = conn.execute(
        'SELECT category_id FROM Category WHERE category_name = ?', (name,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT category_id FROM Category WHERE category_name = 'Uncategorised'"
        ).fetchone()
    return row['category_id'] if row else None


def record_metric(conn, model_type, algorithm=None, f1_score=None, mae=None,
                  mae_ratio=None, sample_count=None, notes=None, user_id=None):
    conn.execute(
        'INSERT INTO ModelMetrics (model_type, algorithm, f1_score, mae, mae_ratio, '
        'sample_count, notes, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (model_type, algorithm, f1_score, mae, mae_ratio, sample_count, notes, user_id),
    )


def latest_metric(conn, model_type):
    return conn.execute(
        'SELECT * FROM ModelMetrics WHERE model_type = ? ORDER BY trained_at DESC, '
        'metric_id DESC LIMIT 1',
        (model_type,),
    ).fetchone()


if __name__ == '__main__':
    init_db()
    print(f'Database ready at {config.DATABASE_PATH}')
