"""Currency conversion for emailed receipts priced in another currency.

Only the email-receipt scan uses this, and that scan is already online (it
signs in to the mailbox), so the exchange rates are fetched in the same
session: once per day at most, and only when a foreign-currency receipt
actually turns up. The request carries nothing but the base currency code.
The last rates fetched are kept in AppSetting, so a later scan that cannot
reach the rate service still converts, using rates of a stated age. With no
rates at all, a foreign receipt is skipped rather than stored wrongly.

A converted amount is an estimate: the bank converts at its own rate, often
with a fee. FX_TOLERANCE is the one rule both the scan and the statement
import use to recognise the same purchase despite that difference.
"""
import json
import re
import urllib.request
from datetime import date

import config
import database

RATES_URL = 'https://open.er-api.com/v6/latest/{base}'
RATES_SETTING = 'fx_rates'
EXACT_TOLERANCE = 0.01
# A bank's card rate plus fees typically lands within a few percent of the
# market rate. Wider risks joining two different purchases; narrower lets the
# statement add the purchase a second time.
FX_TOLERANCE = 0.05

# Most specific first: 'US$' before '$'. A receipt showing kwacha anywhere is
# treated as kwacha, so a "K 50.00 (approx $2)" receipt is not converted.
_LOCAL = re.compile(r'(\bZMW\b|\bZK\b|\bK\s?\d)')
_CURRENCIES = (
    ('USD', re.compile(r'(US\$|\bUSD\b|\$)')),
    ('EUR', re.compile(r'(€|\bEUR\b)')),
    ('GBP', re.compile(r'(£|\bGBP\b)')),
    ('ZAR', re.compile(r'(\bZAR\b|\bR\s?\d)')),
)


def detect_currency(text):
    """The receipt's currency code, or None when it is kwacha (or unmarked)."""
    if not text or _LOCAL.search(text):
        return None
    for code, pattern in _CURRENCIES:
        if pattern.search(text):
            return code
    return None


def tolerance(amount, converted):
    """How far apart two amounts may be and still be the same purchase."""
    return round(abs(amount) * FX_TOLERANCE, 2) if converted else EXACT_TOLERANCE


def _fetch_rates(base, timeout=10):
    with urllib.request.urlopen(RATES_URL.format(base=base), timeout=timeout) as response:
        payload = json.load(response)
    if payload.get('result') != 'success' or not payload.get('rates'):
        raise ValueError('The rate service returned no rates.')
    return payload['rates']


class Converter:
    """Converts foreign amounts to kwacha, fetching rates lazily (one per scan)."""

    def __init__(self, conn, fetch=_fetch_rates, today=None):
        self.conn = conn
        self.fetch = fetch
        self.today = today or date.today()
        self._rates = None
        self.as_of = None
        self.stale = False

    def _load(self):
        cached = database.get_setting(self.conn, RATES_SETTING)
        cached = json.loads(cached) if cached else None
        if cached and cached.get('as_of') == self.today.isoformat():
            return cached
        try:
            fresh = {'as_of': self.today.isoformat(),
                     'rates': self.fetch(config.CURRENCY)}
            database.set_setting(self.conn, RATES_SETTING, json.dumps(fresh))
            return fresh
        except Exception:  # offline, timeout, bad reply: fall back to the cache
            if cached:
                self.stale = True
            return cached

    def to_local(self, amount, code):
        """(kwacha amount, kwacha per unit) for `amount` in `code`, or None."""
        if self._rates is None:
            loaded = self._load() or {}
            self._rates = loaded.get('rates', {})
            self.as_of = loaded.get('as_of')
        per_kwacha = self._rates.get(code)  # units of `code` per 1 kwacha
        if not per_kwacha:
            return None
        return round(amount / per_kwacha, 2), round(1 / per_kwacha, 4)
