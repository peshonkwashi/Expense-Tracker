"""Launch an isolated User Acceptance Testing (UAT) session (section 5.10.3).

Prepares a clean, participant-specific SQLite database and a matching synthetic
bank statement CSV so each UAT participant starts from the first-launch /setup
screen without touching the main expense_tracker.db.

Examples:
    # Standard desktop UAT session for Participant P01 (graded spec only):
    python run_uat_session.py --participant P01 --spec-only

    # Mobile PWA UAT session reachable from a phone on the same Wi-Fi:
    python run_uat_session.py --participant P02 --mobile

    # Android phone over USB (recommended for mobile UAT - no certificates,
    # nothing exposed to the network; the phone opens http://localhost:5000):
    python run_uat_session.py --participant P03 --usb
"""
import argparse
import csv
import glob
import os
import shutil
import socket
import subprocess
import sys

import generate_sample_data


def _find_adb():
    """Locate adb: PATH first, then a winget-installed Google.PlatformTools
    (which is not on PATH until a new terminal is opened after installing)."""
    found = shutil.which('adb')
    if found:
        return found
    pattern = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft',
                           'WinGet', 'Packages', 'Google.PlatformTools_*',
                           'platform-tools', 'adb.exe')
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _bridge_usb(port):
    """Map the phone's localhost:<port> to this PC over USB (adb reverse).

    The phone then reaches the app at http://localhost:<port>, which browsers
    treat as a secure context, so the PWA installs with no certificate and the
    server stays bound to 127.0.0.1 (NFR-05). Returns True when the bridge is up.
    """
    adb = _find_adb()
    if not adb:
        print(' USB bridge       : adb not found. Install it with '
              '`winget install Google.PlatformTools`, then open a new terminal.')
        return False
    result = subprocess.run([adb, 'reverse', f'tcp:{port}', f'tcp:{port}'],
                            capture_output=True, text=True)
    if result.returncode == 0:
        print(f' USB bridge       : active - phone localhost:{port} maps to this PC')
        return True
    print(' USB bridge       : no device detected. Enable USB debugging, connect '
          f'the phone, accept the prompt, then run: adb reverse tcp:{port} tcp:{port}')
    return False


def _detect_lan_ip():
    """Best-effort detection of this machine's local Wi-Fi / LAN IPv4 address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('10.255.255.255', 1))
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


def prepare_session(participant, salary=12000.0, salary_day=25, months=8,
                    seed=42, reset=False, base_dir=None):
    """Create the participant folder, CSV statement, and isolated DB path."""
    root = base_dir or os.path.abspath(os.path.dirname(__file__))
    safe_id = ''.join(ch for ch in participant if ch.isalnum() or ch in ('-', '_')) or 'P01'
    session_dir = os.path.join(root, 'uat_sessions', safe_id)
    os.makedirs(session_dir, exist_ok=True)

    db_path = os.path.join(session_dir, f'uat_{safe_id}.db')
    if reset and os.path.exists(db_path):
        os.remove(db_path)

    csv_path = os.path.join(session_dir, f'bank_statement_{safe_id}.csv')
    rows = generate_sample_data.build_rows(months, salary, salary_day, seed=seed)
    with open(csv_path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Date', 'Description', 'Amount', 'Type'])
        for when, description, amount, kind in rows:
            writer.writerow([when.isoformat(), description, f'{amount:.2f}', kind])

    return {
        'participant': safe_id,
        'session_dir': session_dir,
        'db_path': db_path,
        'csv_path': csv_path,
        'row_count': len(rows),
        'salary': salary,
        'salary_day': salary_day,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--participant', default='P01',
                        help='Participant identifier, e.g. P01, P02 (default: P01)')
    parser.add_argument('--salary', type=float, default=12000.0,
                        help='Suggested monthly salary in ZMW for the test persona (default: 12000)')
    parser.add_argument('--salary-day', type=int, default=25,
                        help='Suggested pay day of the month (default: 25)')
    parser.add_argument('--months', type=int, default=8,
                        help='Months of synthetic statement history to generate (default: 8)')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed for reproducible synthetic statements (default: 42)')
    parser.add_argument('--port', type=int, default=5000,
                        help='Port to run the server on (default: 5000)')
    parser.add_argument('--mobile', action='store_true',
                        help='Bind to 0.0.0.0 so a mobile phone on the same Wi-Fi can connect')
    parser.add_argument('--https', action='store_true',
                        help='Enable ad-hoc HTTPS for mobile PWA installation over LAN')
    parser.add_argument('--usb', action='store_true',
                        help='Bridge an Android phone over USB (adb reverse) so it opens '
                             'http://localhost:<port> - the recommended mobile UAT path')
    parser.add_argument('--spec-only', action='store_true',
                        help='Set DEMO_FEATURES=0 so only graded requirements (FR-01..FR-12) appear')
    parser.add_argument('--reset', action='store_true',
                        help='Delete any existing database for this participant before starting')
    parser.add_argument('--prepare-only', action='store_true',
                        help='Prepare the session files and exit without starting Flask')
    args = parser.parse_args()

    info = prepare_session(
        args.participant, salary=args.salary, salary_day=args.salary_day,
        months=args.months, seed=args.seed, reset=args.reset)

    # Isolate both the database AND the trained model per participant, so a
    # session never trains over or shares the real ml/models/categoriser.joblib.
    model_path = os.path.join(info['session_dir'], 'categoriser.joblib')
    os.environ['EXPENSE_TRACKER_DB'] = info['db_path']
    os.environ['CATEGORISER_PATH'] = model_path
    if args.spec_only:
        os.environ['DEMO_FEATURES'] = '0'

    host = '0.0.0.0' if args.mobile else os.environ.get('FLASK_HOST', '127.0.0.1')
    # SSL_CERT/SSL_KEY (mkcert, docs/mobile-install.md) beats --https (ad-hoc):
    # phones only register the PWA service worker over a certificate they trust.
    uses_cert = bool(os.environ.get('SSL_CERT') and os.environ.get('SSL_KEY'))
    scheme = 'https' if (args.https or uses_cert) else 'http'
    lan_ip = _detect_lan_ip()

    print('=' * 72)
    print(f" UAT SESSION READY - Participant: {info['participant']}")
    print('=' * 72)
    print(f" Isolated Database : {info['db_path']}")
    print(f" Statement CSV     : {info['csv_path']} ({info['row_count']} rows)")
    print(f" Persona Salary    : {info['salary']:,.2f} ZMW on day {info['salary_day']}")
    print(f" Build Mode        : {'Specification-only (DEMO_FEATURES=0)' if args.spec_only else 'Full Demo (DEMO_FEATURES=1)'}")
    print(f" Desktop URL       : {scheme}://127.0.0.1:{args.port}")
    if host == '0.0.0.0':
        print(f" Mobile Wi-Fi URL  : {scheme}://{lan_ip}:{args.port}")
    if args.usb:
        print(f" Phone (USB) URL   : http://localhost:{args.port}")
    print(' UAT Protocol Doc  : docs/uat_plan_and_sheet.md')
    print('=' * 72)

    if args.prepare_only:
        return 0

    if args.usb:
        _bridge_usb(args.port)
        print('=' * 72)

    import config
    config.DATABASE_PATH = info['db_path']
    config.CATEGORISER_PATH = model_path
    if args.spec_only:
        config.DEMO_FEATURES = False

    import app as app_module
    import database
    database.init_db()

    ssl_ctx = 'adhoc' if args.https else None
    if uses_cert:
        ssl_ctx = (os.environ['SSL_CERT'], os.environ['SSL_KEY'])
    app_module.app.run(host=host, port=args.port, debug=False, ssl_context=ssl_ctx)
    return 0


if __name__ == '__main__':
    sys.exit(main())
