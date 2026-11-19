"""Backup and restore of the local database (section 5.11).

A backup is a .zip holding one SQLite file. Phones are the reason for the zip:
neither Android nor iOS knows what a .db file is, so a downloaded .db may be
renamed or refused, and an iPhone's file picker greys out types it does not
recognise. Both platforms' Files apps understand .zip natively, and SQLite
data compresses about five-fold. A plain .db from an older backup is still
accepted on restore.

Copies are taken with SQLite's online backup API rather than a file copy, so
the result is a consistent database even if a write is in progress.
"""
import glob
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime

SQLITE_HEADER = b'SQLite format 3\x00'
REQUIRED_TABLES = {'User', 'Category', 'Transaction_Record', 'SavingsGoal',
                   'AppSetting'}
# A zip is small, but what it unpacks to must stay bounded too (zip bomb).
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
SAFETY_COPIES_KEPT = 3


class BackupError(Exception):
    """The file can't be restored. The message is safe to show the user."""


def _copy_database(source_path, dest_path):
    """Consistent copy of one SQLite database into another file."""
    src = sqlite3.connect(source_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_zip(db_path, stamp):
    """Return (zip bytes, download name) for a backup of `db_path`."""
    name = f'expense_tracker-backup-{stamp}'
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, f'{name}.db')
        _copy_database(db_path, snapshot)
        out = os.path.join(tmp, f'{name}.zip')
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, f'{name}.db')
        with open(out, 'rb') as handle:
            return handle.read(), f'{name}.zip'


def unpack(upload_path, work_dir):
    """Path of the SQLite file inside an uploaded backup (.zip or .db)."""
    with open(upload_path, 'rb') as handle:
        head = handle.read(16)
    if head == SQLITE_HEADER:
        return upload_path
    if not zipfile.is_zipfile(upload_path):
        raise BackupError('That file is not an Expense Tracker backup. Choose '
                          'the .zip (or .db) file made by "Download a backup".')
    with zipfile.ZipFile(upload_path) as archive:
        members = [m for m in archive.infolist()
                   if not m.is_dir() and m.filename.lower().endswith(
                       ('.db', '.sqlite', '.sqlite3'))]
        if len(members) != 1:
            raise BackupError('That .zip does not contain exactly one database '
                              'file, so it is not an Expense Tracker backup.')
        member = members[0]
        if member.file_size > MAX_UNZIPPED_BYTES:
            raise BackupError('The database inside that .zip is too large.')
        # Extract under a fixed name: the stored name is never used as a path.
        target = os.path.join(work_dir, 'restore.db')
        with archive.open(member) as src, open(target, 'wb') as dst:
            copied = 0
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_UNZIPPED_BYTES:
                    raise BackupError('The database inside that .zip is too large.')
                dst.write(chunk)
    return target


def validate(db_path):
    """Raise BackupError unless `db_path` is a healthy Expense Tracker database."""
    with open(db_path, 'rb') as handle:
        if handle.read(16) != SQLITE_HEADER:
            raise BackupError('That file is not a valid database.')
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()
        if not integrity or str(integrity[0]).lower() != 'ok':
            raise BackupError('The backup file is damaged (it failed the '
                              'integrity check).')
        objects = conn.execute(
            'SELECT type, name FROM sqlite_master').fetchall()
        tables = {name for kind, name in objects if kind == 'table'}
        if not REQUIRED_TABLES.issubset(tables):
            raise BackupError('That database is missing Expense Tracker tables, '
                              'so it is not a backup from this app.')
        # The app's schema has no triggers. One in an uploaded file would run
        # its own SQL on every later write, so such a file is refused.
        if any(kind == 'trigger' for kind, _ in objects):
            raise BackupError('That database contains triggers, which this app '
                              'never creates, so it was not restored.')
        if conn.execute('SELECT COUNT(*) FROM User').fetchone()[0] < 1:
            raise BackupError('That backup contains no profile.')
    except sqlite3.DatabaseError as error:
        raise BackupError(f'That database could not be read ({error}).')
    finally:
        conn.close()


def restore(source_db, live_db, stamp):
    """Replace the live database with `source_db`; return the safety-copy path.

    The current data is copied aside first, and only the newest
    SAFETY_COPIES_KEPT safety copies are kept.
    """
    safety = None
    if os.path.exists(live_db):
        safety = f'{live_db}.pre-restore-{stamp}.bak'
        _copy_database(live_db, safety)
        old = sorted(glob.glob(f'{glob.escape(live_db)}.pre-restore-*.bak'))
        for path in old[:-SAFETY_COPIES_KEPT]:
            try:
                os.remove(path)
            except OSError:
                pass
    _copy_database(source_db, live_db)
    return safety


def timestamp():
    return datetime.now().strftime('%Y%m%d-%H%M%S')
