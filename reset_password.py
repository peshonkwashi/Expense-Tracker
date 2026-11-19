"""Reset the local unlock password from the host terminal.

If a user forgets both their password and the profile details needed by the
in-app /reset-password screen, running this script on the host machine sets a
new PBKDF2 password hash directly in the local SQLite database without touching
any transactions, goals, or profile data.

    python reset_password.py                        # interactive prompt
    python reset_password.py --password newpass123  # non-interactive

Prefer the interactive prompt: a password given with --password is saved in
the terminal's command history. Any device still signed in is signed out on
its next request.
"""
import argparse
import getpass
import os
import sys

import config
import database
import security


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--password',
                        help='new password (at least 8 characters); omit to prompt hidden')
    args = parser.parse_args()

    if not os.path.exists(config.DATABASE_PATH):
        print(f'No database found at {config.DATABASE_PATH}. Run python app.py first.')
        return 1

    conn = database.get_db_connection()
    try:
        user = database.get_user(conn)
        if not user:
            print('No user profile exists yet. Open /setup in the browser to create one.')
            return 1

        password = args.password
        if not password:
            password = getpass.getpass(f"New password for {user['full_name']} (8+ chars): ")
            confirm = getpass.getpass('Confirm new password: ')
            if password != confirm:
                print('Error: The two passwords do not match.')
                return 1

        ok, err = security.set_password(conn, password)
        if not ok:
            print(f'Error: {err}')
            return 1

        print(f"Password updated for '{user['full_name']}' in {config.DATABASE_PATH}.\n"
              'Devices that were signed in will be asked to sign in again.')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
