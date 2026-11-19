"""Match emailed receipts to transactions, and add the ones the bank hasn't
shown yet.

Two passes share one mailbox session:

1. Itemise existing transactions (find_receipt_for). For each un-itemised
   transaction from a merchant that emails receipts, the inbox is searched for
   emails from (or mentioning) that merchant within a few days of the
   transaction date. The receipt whose total equals the transaction amount
   wins. If nothing matches exactly and the window held only one receipt, that
   one is used and flagged for review, because a tip or a rounding difference
   can make an honest receipt differ slightly from the bank amount. With
   several receipts and no exact match, nothing is guessed.

2. Add new transactions (read_new_receipts + add_receipts). Recent receipt
   emails not used in pass 1 are read. A receipt that matches an existing
   debit (same amount within 3 days) only itemises it; anything else becomes
   a new transaction. Each new row is keyed by the email's Message-ID, so a
   rescan never adds it twice, and when the bank statement arrives later the
   import merges the bank row into it (services/ingestion.py) instead of
   adding a duplicate.
"""
import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import database
from ml import categorization
from services import fx, imap_fetcher
from services.ocr import (EMAIL_RECEIPT_MERCHANTS, ReceiptScan, ReceiptScanError,
                          parse_receipt_text, parse_receipt_total)

MATCH_WINDOW_DAYS = 3
EMAIL_HASH_PREFIX = 'email-'
# Converted from another currency: matched within fx.FX_TOLERANCE, not to the cent.
EMAIL_FX_HASH_PREFIX = 'email-fx-'

MATCHED = 'matched'          # receipt total equals the transaction amount
SOLE_CANDIDATE = 'sole'      # the only receipt nearby, but the total differs
AMBIGUOUS = 'ambiguous'      # several receipts nearby, none matches the amount
UNREADABLE = 'unreadable'    # emails found, but no priced lines could be read
NOT_FOUND = 'not_found'      # no email from this merchant near that date


@dataclass
class EmailReceiptResult:
    status: str
    scan: ReceiptScan = None
    message_key: str = None

    @property
    def usable(self):
        return self.status in (MATCHED, SOLE_CANDIDATE)


def _scan_text(text):
    items = parse_receipt_text(text)
    return ReceiptScan(items=items, total=parse_receipt_total(text)) if items else None


def _read_message(msg, scan_file):
    """Read one email as a receipt: (ReceiptScan or None, the text it was read from)."""
    for text in imap_fetcher.body_texts(msg):
        scan = _scan_text(text)
        if scan:
            return scan, text
    path = imap_fetcher.save_attachment(msg)
    if not path:
        return None, ''
    try:
        scan = scan_file(path)
    except ReceiptScanError:
        return None, ''
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return (scan, '') if scan and scan.items else (None, '')


def scan_message(msg, scan_file):
    """Read one email as a receipt; returns a ReceiptScan with items, or None."""
    return _read_message(msg, scan_file)[0]


def receipt_amount(scan):
    return scan.total if scan.total is not None else scan.items_total


@dataclass
class Priced:
    """A receipt priced in kwacha. For a converted one, `currency`,
    `original` and `rate` record what it was and the rate used."""
    scan: ReceiptScan
    amount: float
    currency: str = None
    original: float = None
    rate: float = None

    @property
    def converted(self):
        return self.currency is not None


def price_in_local(scan, text, converter):
    """The receipt in kwacha, or None when its currency can't be converted.

    Item prices use the same rate as the total, so the items still add up to
    the transaction.
    """
    amount = round(receipt_amount(scan), 2)
    code = fx.detect_currency(text)
    if code is None:
        return Priced(scan, amount)
    converted = converter.to_local(amount, code) if converter else None
    if converted is None:
        return None
    local, rate = converted
    items = [dict(item, price=round(item['price'] * rate, 2)) for item in scan.items]
    total = round(scan.total * rate, 2) if scan.total is not None else None
    return Priced(ReceiptScan(items=items, total=total), local, code, amount, rate)


# --- Pass 1: itemise existing transactions -----------------------------------

def find_receipt_for(mail, keyword, transaction_date, amount, scan_file,
                     days_either_side=MATCH_WINDOW_DAYS, converter=None):
    """Find and read the emailed receipt for one transaction.

    A receipt in another currency is converted first and matches within
    fx.FX_TOLERANCE, since the bank converted at its own rate.
    """
    messages = imap_fetcher.find_receipt_messages(
        mail, keyword, transaction_date, days_either_side=days_either_side)
    if not messages:
        return EmailReceiptResult(NOT_FOUND)

    read = []
    for msg in messages:
        scan, text = _read_message(msg, scan_file)
        priced = price_in_local(scan, text, converter) if scan else None
        if priced:
            read.append((priced, msg))
    if not read:
        return EmailReceiptResult(UNREADABLE)

    for priced, msg in read:
        if abs(priced.amount - amount) <= fx.tolerance(amount, priced.converted):
            return EmailReceiptResult(MATCHED, priced.scan, imap_fetcher.message_key(msg))
    if len(read) == 1:
        priced, msg = read[0]
        return EmailReceiptResult(SOLE_CANDIDATE, priced.scan,
                                  imap_fetcher.message_key(msg))
    return EmailReceiptResult(AMBIGUOUS)


# --- Pass 2: receipts the bank hasn't shown yet ------------------------------

@dataclass
class NewReceipt:
    message_key: str
    on: date
    merchant: str
    amount: float          # in kwacha
    scan: ReceiptScan      # item prices in kwacha
    currency: str = None   # set when converted from another currency
    original: float = None
    rate: float = None

    @property
    def converted(self):
        return self.currency is not None

    @property
    def description(self):
        if not self.converted:
            return self.merchant
        return f'{self.merchant} ({self.currency} {self.original:,.2f} at {self.rate:,.2f})'


@dataclass
class SweepResult:
    receipts: list = field(default_factory=list)
    foreign_currency: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)


def merchant_of(msg):
    """(merchant keyword, display name) for a receipt email, or (None, None).

    The sender's name and address and the subject are checked against the
    merchants known to email receipts, so a newsletter from an unknown sender
    is never turned into a transaction.
    """
    name, address, subject = imap_fetcher.sender_and_subject(msg)
    haystack = categorization.normalise(f'{name} {address} {subject}')
    found = [kw for kw in EMAIL_RECEIPT_MERCHANTS if kw in haystack]
    if not found:
        return None, None
    keyword = max(found, key=len)  # 'uber eats' over 'uber'
    display = name if keyword in categorization.normalise(name) else keyword.title()
    return keyword, display[:60]


def read_new_receipts(mail, scan_file, skip_keys=(), days_back=30, converter=None):
    """Read the recent receipt emails not already used to itemise something.

    Receipts in another currency are converted to kwacha; one that can't be
    (no rate available) is listed in `foreign_currency` instead of stored.
    """
    result = SweepResult()
    for msg in imap_fetcher.find_recent_receipt_messages(mail, days_back=days_back):
        key = imap_fetcher.message_key(msg)
        if key in skip_keys:
            continue
        keyword, merchant = merchant_of(msg)
        if not keyword:
            continue
        scan, text = _read_message(msg, scan_file)
        if not scan:
            result.unreadable.append(merchant)
            continue
        priced = price_in_local(scan, text, converter)
        if priced is None:
            result.foreign_currency.append(merchant)
            continue
        if priced.amount <= 0:
            result.unreadable.append(merchant)
            continue
        result.receipts.append(NewReceipt(
            key, imap_fetcher.message_date(msg), merchant, priced.amount,
            priced.scan, priced.currency, priced.original, priced.rate))
    return result


def email_hash(user_id, message_key, converted=False):
    digest = hashlib.sha256(f'{user_id}|{message_key}'.encode('utf-8')).hexdigest()
    return (EMAIL_FX_HASH_PREFIX if converted else EMAIL_HASH_PREFIX) + digest[:40]


def _insert_items(conn, transaction_id, items):
    for item in items:
        conn.execute(
            'INSERT INTO Receipt_Item (transaction_id, item_name, amount, quantity) '
            'VALUES (?, ?, ?, ?)',
            (transaction_id, item['name'], item['price'], item['quantity']))


def matching_debit(conn, user_id, on, amount, window=MATCH_WINDOW_DAYS,
                   converted=False):
    """The existing debit for this amount closest to `on` (within the window);
    on a tie of dates, the closest amount."""
    return conn.execute(
        "SELECT t.transaction_id, "
        "  EXISTS (SELECT 1 FROM Receipt_Item r WHERE r.transaction_id = t.transaction_id) "
        "    AS has_items "
        "FROM Transaction_Record t "
        "WHERE t.user_id = ? AND t.transaction_type = 'DEBIT' "
        "  AND ABS(t.amount - ?) <= ? AND t.transaction_date BETWEEN ? AND ? "
        "ORDER BY ABS(julianday(t.transaction_date) - julianday(?)), "
        "  ABS(t.amount - ?) LIMIT 1",
        (user_id, amount, fx.tolerance(amount, converted),
         (on - timedelta(days=window)).isoformat(),
         (on + timedelta(days=window)).isoformat(), on.isoformat(), amount)).fetchone()


def add_receipts(conn, user_id, receipts):
    """Store swept receipts. Returns {'added': [...], 'itemised': [...]}.

    Does not commit; the caller commits once for the whole scan (NFR-06).
    """
    added, itemised = [], []
    for receipt in receipts:
        digest = email_hash(user_id, receipt.message_key, receipt.converted)
        if conn.execute('SELECT 1 FROM Transaction_Record WHERE user_id = ? '
                        'AND import_hash = ?', (user_id, digest)).fetchone():
            continue  # added by an earlier scan
        label = f'{receipt.description}: {receipt.amount:,.2f}'

        existing = matching_debit(conn, user_id, receipt.on, receipt.amount,
                                  converted=receipt.converted)
        if existing:
            if not existing['has_items']:
                _insert_items(conn, existing['transaction_id'], receipt.scan.items)
                itemised.append(label)
            continue  # the bank already has it

        on = receipt.on.isoformat()
        category_name, confidence, source = categorization.classify(
            receipt.merchant, receipt.amount, on)
        cursor = conn.execute(
            'INSERT INTO Transaction_Record (transaction_date, description, amount, '
            ' transaction_type, is_salary, category_source, category_confidence, '
            ' import_hash, user_id, category_id) '
            "VALUES (?, ?, ?, 'DEBIT', 0, ?, ?, ?, ?, ?)",
            (on, receipt.description, receipt.amount, source, confidence, digest,
             user_id, database.category_id_for(conn, category_name)))
        _insert_items(conn, cursor.lastrowid, receipt.scan.items)
        added.append(label)
    return {'added': added, 'itemised': itemised}
