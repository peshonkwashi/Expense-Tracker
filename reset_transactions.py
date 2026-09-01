"""Remove imported transaction data without destroying the profile.

Re-uploading a corrected statement does not replace an earlier import: if any
amount changed, the deduplication hash changes with it, so the rows import as
new transactions and every total is counted twice. The fix is to remove the old
import first, which is what this does.

Kept: the user profile, the password, the data notice, and savings goals.
Removed: transactions, budget recommendations, model metrics and the trained
model — all of which are derived from the transactions and are rebuilt on the
next upload.

    python reset_transactions.py            # dry run, shows what would go
    python reset_transactions.py --apply    # do it, after taking a backup
    python reset_transactions.py --apply --all   # also remove pre-migration rows

By default, transactions recorded before the schema migration are kept. Those
are rows the CSV importer never touched — typically a user's earliest real data
— and they carry a 'legacy-' deduplication key rather than a content hash.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

import config

IMPORTED_PREDICATE = "import_hash NOT LIKE 'legacy-%'"


def summarise(conn, predicate):
    row = conn.execute(
        f'SELECT COUNT(*) AS n, MIN(transaction_date) AS first, '
        f'MAX(transaction_date) AS last, ROUND(SUM(amount), 2) AS total '
        f'FROM Transaction_Record WHERE {predicate}').fetchone()
    return row


def backup_database():
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = f'{config.DATABASE_PATH}.{stamp}.bak'
    shutil.copy2(config.DATABASE_PATH, target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true',
                        help='perform the deletion (otherwise this is a dry run)')
    parser.add_argument('--all', action='store_true',
                        help='also remove transactions predating the schema migration')
    args = parser.parse_args()

    if not os.path.exists(config.DATABASE_PATH):
        print(f'No database at {config.DATABASE_PATH}. Nothing to do.')
        return 0

    predicate = '1=1' if args.all else IMPORTED_PREDICATE

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        target = summarise(conn, predicate)
        kept = summarise(conn, f'NOT ({predicate})')
        recommendations = conn.execute(
            'SELECT COUNT(*) FROM BudgetRecommendation').fetchone()[0]
        metrics = conn.execute('SELECT COUNT(*) FROM ModelMetrics').fetchone()[0]
        goals = conn.execute('SELECT COUNT(*) FROM SavingsGoal').fetchone()[0]

        if not target['n']:
            print('No matching transactions. Nothing to remove.')
            return 0

        print(f"Database: {config.DATABASE_PATH}")
        print()
        print(f"  REMOVE  {target['n']:>4} transactions  "
              f"{target['first']} -> {target['last']}  totalling {target['total']:,}")
        print(f"  REMOVE  {recommendations:>4} budget recommendations "
              f"(recomputed on next upload)")
        print(f"  REMOVE  {metrics:>4} model metric records")
        print("  REMOVE       the trained categorisation model "
              "(retrained on next upload)")
        print(f"  KEEP    {kept['n'] or 0:>4} transactions"
              + (f"  {kept['first']} -> {kept['last']}" if kept['n'] else ''))
        print(f"  KEEP    {goals:>4} savings goals, plus your profile, password "
              f"and data notice")
        print()

        if not args.apply:
            print('Dry run. Nothing was changed. Re-run with --apply to proceed.')
            return 0

        backup = backup_database()
        print(f'Backup written to {backup}')

        # One transaction: either all of it happens or none of it does (NFR-06).
        try:
            conn.execute('BEGIN')
            deleted = conn.execute(
                f'DELETE FROM Transaction_Record WHERE {predicate}').rowcount
            conn.execute('DELETE FROM BudgetRecommendation')
            conn.execute('DELETE FROM ModelMetrics')
            conn.execute('UPDATE Transaction_Record SET is_subscription = 0')
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if os.path.exists(config.CATEGORISER_PATH):
            os.remove(config.CATEGORISER_PATH)

        conn.execute('VACUUM')
        print(f'Removed {deleted} transactions. Upload a statement to rebuild.')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
