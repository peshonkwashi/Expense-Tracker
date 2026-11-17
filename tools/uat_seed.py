"""Seed a fresh UAT database with synthetic test data.

Creates a throwaway profile, imports a generated 8-month bank statement and
trains the categoriser, so a tester opens a ready-to-explore app with no setup.
Driven by environment variables (set by run_uat.bat) so it never touches the
real expense_tracker.db or the real trained model:

    EXPENSE_TRACKER_DB   path to the throwaway UAT database (required in practice)
    CATEGORISER_PATH     path to the throwaway model file

Run via the project venv:  venv\\Scripts\\python.exe tools\\uat_seed.py
"""
import os
import subprocess
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# A known password printed to the tester. This is demo data only — never a real
# account — so a fixed, shared credential is appropriate.
UAT_PASSWORD = 'uat-demo-1234'
SALARY = 12000.0
SALARY_DAY = 25


def main():
    import config  # reads EXPENSE_TRACKER_DB / CATEGORISER_PATH from the env
    db_path = config.DATABASE_PATH
    if db_path == os.path.join(PROJECT, 'expense_tracker.db'):
        sys.exit('Refusing to seed the real database. Set EXPENSE_TRACKER_DB to a '
                 'throwaway path first (run this through run_uat.bat).')

    # Start clean so every UAT session begins from the same known state.
    for suffix in ('', '-wal', '-shm'):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass

    import database
    import security
    from services import ingestion

    database.init_db()
    conn = database.get_db_connection()
    cur = conn.execute(
        'INSERT INTO User (full_name, salary_amount, salary_day) VALUES (?, ?, ?)',
        ('UAT Tester', SALARY, SALARY_DAY))
    user_id = cur.lastrowid
    security.set_password(conn, UAT_PASSWORD)
    security.acknowledge_notice(conn)
    conn.commit()

    # Generate a synthetic statement and import it through the real pipeline.
    handle = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
    handle.close()
    try:
        subprocess.run(
            [sys.executable, os.path.join(PROJECT, 'generate_sample_data.py'),
             '--months', '8', '--salary', str(int(SALARY)),
             '--salary-day', str(SALARY_DAY), '--seed', '7',
             '--output', handle.name],
            check=True, capture_output=True, cwd=PROJECT)
        report = ingestion.import_statement(conn, user_id, handle.name)
        pipeline = ingestion.post_import_pipeline(conn, user_id)
        conn.commit()
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass
    conn.close()

    print('UAT database seeded:', db_path)
    print(f"  imported {report.get('imported')} transactions, "
          f"model trained: {pipeline.get('training', {}).get('trained')}")
    print(f'  login password: {UAT_PASSWORD}')


if __name__ == '__main__':
    main()
