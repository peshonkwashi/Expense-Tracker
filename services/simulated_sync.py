"""Simulated bank and mobile-money connections (demo features).

Zambia has no open-banking API an individual project can join, so, as agreed
with the supervisor, bank and mobile-money sync are simulated: the user picks a
real Zambian provider, sees a read-only consent screen, and "connects"; each
sync then generates realistic, provider-specific ZMW transactions. Nothing is
sent anywhere (NFR-05).

Connections live in AppSetting as small JSON records: one for the bank and
one per wallet, so a user with both MTN MoMo and Airtel Money can link both.
Every synced row's import_hash names its source ('sync-bank-...',
'sync-momo-mtn-...', 'sync-momo-airtel-...'), so Disconnect removes exactly
the rows that one connection imported, leaving uploaded, manual,
email-receipt and other-wallet transactions untouched.
"""
import json
import random
import re
import secrets
from datetime import date, datetime, timedelta

import database

CONSENT_DAYS = 90

BANKS = {
    'zanaco': {'name': 'Zanaco', 'prefix': 'ZANACO POS'},
    'fnb': {'name': 'FNB Zambia', 'prefix': 'FNB CARD PURCHASE'},
    'absa': {'name': 'Absa', 'prefix': 'ABSA POS'},
    'stanbic': {'name': 'Stanbic', 'prefix': 'STANBIC VISA'},
}

WALLETS = {
    'mtn': {'name': 'MTN MoMo', 'prefixes': ('096', '076')},
    'airtel': {'name': 'Airtel Money', 'prefixes': ('097', '077')},
}

# Card spending seen on a bank account: (merchant, category, low, high).
BANK_MERCHANTS = [
    ('SHOPRITE CAIRO RD', 'Groceries', 80, 850),
    ('PICK N PAY ARCADES', 'Groceries', 60, 700),
    ('HUNGRY LION MANDA HILL', 'Dining Out', 40, 220),
    ('DEBONAIRS PIZZA', 'Dining Out', 90, 260),
    ('PUMA ENERGY KABULONGA', 'Transport', 150, 600),
    ('NETFLIX.COM', 'Subscriptions', 159, 159),
    ('LINK PHARMACY', 'Healthcare', 30, 400),
    ('GAME STORES', 'Shopping', 100, 900),
]

# Wallet outflows, worded the way each network's statement shows them.
WALLET_MERCHANTS = {
    'mtn': [
        ('MTN MoMo Airtime Purchase', 'Airtime & Data', 10, 100),
        ('MTN MoMo Data Bundle', 'Airtime & Data', 50, 250),
        ('MoMo Pay ZESCO Prepaid Token', 'Utilities', 100, 500),
        ('MoMo Pay DSTV Subscription', 'Subscriptions', 250, 500),
        ('MoMo Pay Shoprite', 'Groceries', 50, 600),
        ('MTN MoMo Send Money', 'Transfers', 100, 1500),
        ('MTN MoMo Agent Cash Out', 'Cash Withdrawal', 200, 2000),
    ],
    'airtel': [
        ('Airtel Money Airtime Top-up', 'Airtime & Data', 10, 100),
        ('Airtel Money Data Bundle', 'Airtime & Data', 50, 250),
        ('Airtel Money ZESCO Token', 'Utilities', 100, 500),
        ('Airtel Money GOtv Payment', 'Subscriptions', 100, 300),
        ('Airtel Money Pay Pick n Pay', 'Groceries', 50, 600),
        ('Airtel Money Send Money', 'Transfers', 100, 1500),
        ('Airtel Money Agent Withdrawal', 'Cash Withdrawal', 200, 2000),
    ],
}

BANK_SETTING, BANK_LAST_SYNC = 'sync_bank_link', 'last_bank_sync'
# The single-wallet format used before multi-wallet support; migrated on read.
_LEGACY_MOMO, _LEGACY_MOMO_SYNC = 'sync_momo_link', 'last_momo_sync'


def _keys(kind, provider=None):
    """(connection key, last-sync key) for the bank or one wallet."""
    if kind == 'bank':
        return BANK_SETTING, BANK_LAST_SYNC
    return f'sync_momo_link_{provider}', f'last_momo_sync_{provider}'


def _hash_prefix(kind, provider=None):
    return 'sync-bank-' if kind == 'bank' else f'sync-momo-{provider}-'


def _load(conn, kind, provider=None):
    link_key, sync_key = _keys(kind, provider)
    raw = database.get_setting(conn, link_key)
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    record['last_sync'] = database.get_setting(conn, sync_key)
    # Display forms ("22 Feb 2027") for the Transactions page.
    for key in ('last_sync', 'consent_expires'):
        if record.get(key):
            record[f'{key}_label'] = date.fromisoformat(record[key]).strftime('%d %b %Y')
    return record


def _migrate_legacy_wallet(conn):
    """Move a pre-multi-wallet record (one 'sync_momo_link' key, rows tagged
    'sync-momo-<hex>') to the per-wallet format, so every other function only
    knows one format. Runs once; afterwards the legacy keys are gone."""
    raw = database.get_setting(conn, _LEGACY_MOMO)
    if raw is None:
        return
    try:
        provider = json.loads(raw).get('provider')
    except ValueError:
        provider = None
    if provider in WALLETS:
        link_key, sync_key = _keys('momo', provider)
        if database.get_setting(conn, link_key) is None:
            database.set_setting(conn, link_key, raw)
            last = database.get_setting(conn, _LEGACY_MOMO_SYNC)
            if last:
                database.set_setting(conn, sync_key, last)
        tagged = tuple(_hash_prefix('momo', key) + '%' for key in WALLETS)
        conn.execute(
            "UPDATE Transaction_Record SET import_hash = ? || substr(import_hash, 11) "
            "WHERE import_hash LIKE 'sync-momo-%' AND import_hash NOT LIKE ? "
            "AND import_hash NOT LIKE ?",
            (_hash_prefix('momo', provider),) + tagged)
    conn.execute('DELETE FROM AppSetting WHERE key IN (?, ?)',
                 (_LEGACY_MOMO, _LEGACY_MOMO_SYNC))
    conn.commit()


# --- Connections -------------------------------------------------------------

def wallet_connections(conn):
    """Every connected wallet, in WALLETS order (MTN MoMo, then Airtel Money)."""
    _migrate_legacy_wallet(conn)
    return [record for record in (_load(conn, 'momo', key) for key in WALLETS)
            if record]


def connection(conn, kind, provider=None):
    """The bank connection, or one wallet's (`provider`), or None."""
    if kind == 'momo':
        _migrate_legacy_wallet(conn)
        return _load(conn, 'momo', provider) if provider in WALLETS else None
    return _load(conn, 'bank')


def _save(conn, kind, record, provider=None):
    link_key, sync_key = _keys(kind, provider)
    # A fresh connection starts a fresh daily-sync window.
    conn.execute('DELETE FROM AppSetting WHERE key = ?', (sync_key,))
    database.set_setting(conn, link_key, json.dumps(record))


def connect_bank(conn, bank_key):
    bank = BANKS[bank_key]
    now = datetime.now()
    _save(conn, 'bank', {
        'provider': bank_key, 'name': bank['name'],
        'account_mask': f'•••• {secrets.randbelow(9000) + 1000}',
        'connected_at': now.isoformat(timespec='minutes'),
        'consent_expires': (now + timedelta(days=CONSENT_DAYS)).date().isoformat(),
    })


def normalise_zambian_number(raw):
    """'0961 234 567', '+260961234567' or '260961234567' -> '0961234567',
    or None when it isn't a 10-digit Zambian mobile number."""
    digits = re.sub(r'\D', '', raw or '')
    if digits.startswith('260') and len(digits) == 12:
        digits = '0' + digits[3:]
    return digits if re.fullmatch(r'0[79]\d{8}', digits) else None


def wallet_number_error(wallet_key, raw):
    """An error message for an unusable number, or None when it is fine."""
    number = normalise_zambian_number(raw)
    wallet = WALLETS[wallet_key]
    if not number:
        return ('Enter a Zambian mobile number, e.g. 0961 234 567 or '
                '+260 961 234 567.')
    if not number.startswith(wallet['prefixes']):
        return (f"{number[:3]} is not a {wallet['name']} number. "
                f"{wallet['name']} numbers start with "
                f"{' or '.join(wallet['prefixes'])}.")
    return None


def connect_wallet(conn, wallet_key, raw_number):
    number = normalise_zambian_number(raw_number)
    _save(conn, 'momo', {
        'provider': wallet_key, 'name': WALLETS[wallet_key]['name'],
        'number_mask': f'{number[:3]} ••• {number[-4:]}',
        'connected_at': datetime.now().isoformat(timespec='minutes'),
    }, provider=wallet_key)


def disconnect(conn, kind, user_id, remove_transactions=False, provider=None):
    """Forget the bank or one wallet; optionally delete only the rows that
    connection imported. Returns how many transactions were removed."""
    removed = 0
    if remove_transactions:
        pattern = _hash_prefix(kind, provider) + '%'
        conn.execute(
            'DELETE FROM Receipt_Item WHERE transaction_id IN (SELECT transaction_id '
            'FROM Transaction_Record WHERE user_id = ? AND import_hash LIKE ?)',
            (user_id, pattern))
        removed = conn.execute(
            'DELETE FROM Transaction_Record WHERE user_id = ? AND import_hash LIKE ?',
            (user_id, pattern)).rowcount
    conn.execute('DELETE FROM AppSetting WHERE key IN (?, ?)', _keys(kind, provider))
    return removed


# --- Syncing -----------------------------------------------------------------

def _catalogue(kind, record):
    if kind == 'bank':
        prefix = BANKS[record['provider']]['prefix']
        return [(f'{prefix} {name}', cat, low, high)
                for name, cat, low, high in BANK_MERCHANTS]
    return WALLET_MERCHANTS[record['provider']]


def _sync_one(conn, kind, record, user_id, today, first, rng):
    if record.get('consent_expires') and today > date.fromisoformat(record['consent_expires']):
        return 'expired'
    if record.get('last_sync') == today.isoformat():
        return None

    window, count = (14, rng.randint(12, 18)) if first else (3, rng.randint(5, 10))
    catalogue = _catalogue(kind, record)
    category_ids = {cat: database.category_id_for(conn, cat)
                    for _, cat, _, _ in catalogue}
    provider = record['provider']
    hash_prefix = _hash_prefix(kind, provider)
    added = 0
    for _ in range(count):
        name, category, low, high = rng.choice(catalogue)
        on = today - timedelta(days=rng.randint(0, window - 1))
        conn.execute(
            'INSERT INTO Transaction_Record (transaction_date, description, amount, '
            ' transaction_type, category_id, category_source, import_hash, user_id) '
            "VALUES (?, ?, ?, 'DEBIT', ?, 'rule', ?, ?)",
            (on.isoformat(), name, round(rng.uniform(low, high), 2),
             category_ids[category], f'{hash_prefix}{secrets.token_hex(12)}', user_id))
        added += 1
    database.set_setting(conn, _keys(kind, provider)[1], today.isoformat())
    return added


def sync(conn, kind, user_id, today=None, first=False, rng=None, provider=None):
    """Import simulated recent transactions for the connected account(s).

    For 'momo', `provider` ('mtn' or 'airtel') syncs that wallet; without it,
    every connected wallet not yet synced today. Returns the number added,
    'expired' when the bank's consent has lapsed, or None when everything
    targeted already synced today.
    """
    today = today or date.today()
    rng = rng or random.Random()

    if kind == 'momo' and provider is None:
        wallets = wallet_connections(conn)
        if not wallets:
            raise LookupError('No momo account is connected.')
        results = [_sync_one(conn, 'momo', rec, user_id, today, first, rng)
                   for rec in wallets]
        added = [n for n in results if isinstance(n, int)]
        return sum(added) if added else None

    record = connection(conn, kind, provider=provider)
    if record is None:
        raise LookupError(f'No {kind} account is connected.')
    return _sync_one(conn, kind, record, user_id, today, first, rng)
