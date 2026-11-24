"""On-device financial assistant.

Answers plain-English questions about the user's own data with no network
access (NFR-05): no language model and no external service. A question is
parsed into an intent plus slots (category, merchant, period, amount), then
answered from the same services the screens use, so the assistant always
agrees with the dashboard and budget pages.

    parse()   question text            -> Query
    answer()  Query + database         -> Reply (HTML answer, suggestions, context)

Follow-up questions ("and last month?", "what about transport?") reuse the
previous question's slots, carried between requests in the session as a small
dict (Reply.context).

Every user-controlled string (category, goal, merchant names) is escaped
before it is placed in the HTML answer.
"""
import calendar
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode
from datetime import date, timedelta

from markupsafe import escape

import database
from ml import categorization, forecasting, subscriptions
from services import behavioural, recommendation

# --- Vocabulary --------------------------------------------------------------

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS.pop('may', None)  # 'may' is also a verb ("may I...");
_MAY = re.compile(r'\b(in|for|during|of|since)\s+may\b')

# Everyday words for the built-in categories. Only categories that exist in
# the database are ever used, so a renamed or missing category is ignored.
CATEGORY_SYNONYMS = {
    'food': ['Groceries', 'Dining Out'],
    'eating out': ['Dining Out'], 'restaurant': ['Dining Out'],
    'restaurants': ['Dining Out'], 'takeaway': ['Dining Out'],
    'takeaways': ['Dining Out'], 'lunch': ['Dining Out'],
    'grocery': ['Groceries'], 'supermarket': ['Groceries'],
    'rent': ['Housing'], 'house': ['Housing'],
    'electricity': ['Utilities'], 'water': ['Utilities'], 'bills': ['Utilities'],
    'taxi': ['Transport'], 'taxis': ['Transport'], 'bus': ['Transport'],
    'fuel': ['Transport'], 'petrol': ['Transport'], 'travel': ['Transport'],
    'airtime': ['Airtime & Data'], 'data': ['Airtime & Data'],
    'bundles': ['Airtime & Data'],
    'medical': ['Healthcare'], 'doctor': ['Healthcare'], 'pharmacy': ['Healthcare'],
    'health': ['Healthcare'], 'school': ['Education'], 'fees': ['Education'],
    'loan': ['Loan & Debt'], 'loans': ['Loan & Debt'], 'debt': ['Loan & Debt'],
    'clothes': ['Shopping'], 'clothing': ['Shopping'],
    'fun': ['Entertainment'], 'movies': ['Entertainment'],
    'salon': ['Personal Care'], 'haircut': ['Personal Care'],
}

# Words that never name a merchant, so "how much did I spend" can't match a
# shop that happens to contain "much".
STOPWORDS = set('''
a about above after again all also am an and any are as at be been before
being below between both but by can cost costs could did do does doing done
down during each ever every few for from had has have having how i if in
into is it its just last left like made make many me money month months more
most my no not now of on once only or other our out over own paid pay pays
per please same she should show so some spend spending spent still such tell
than that the their them then there these they this those through times to
too total under until up us very was we week weeks were what when where
which while who why will with would year years you your today yesterday
much compare compared versus vs biggest largest top highest average daily
budget remaining afford buy cycle payday since salary so far recent latest
transactions transaction purchases purchase payments payment forecast predict
next expect goal goals saving savings subscription subscriptions recurring
help income earn earned times often day days again ago go goes went use used
k zmw kwacha
'''.split())


# --- Query -------------------------------------------------------------------

@dataclass
class Period:
    start: date
    end: date      # inclusive
    label: str
    kind: str      # 'month', 'cycle', 'days', 'year', 'all', 'week', 'day'

    def previous(self):
        """The equivalent period just before this one (for comparisons)."""
        if self.kind == 'month':
            prev_end = self.start - timedelta(days=1)
            label = ('last month' if self.label == 'this month'
                     else prev_end.strftime('in %B %Y'))
            return Period(prev_end.replace(day=1), prev_end, label, 'month')
        length = (self.end - self.start).days + 1
        end = self.start - timedelta(days=1)
        label = {'cycle': 'the previous pay cycle', 'week': 'the week before',
                 'day': 'the day before'}.get(self.kind, f'in the {length} days before')
        return Period(end - timedelta(days=length - 1), end, label, self.kind)


@dataclass
class Query:
    text: str
    intent: str = None
    categories: list = field(default_factory=list)   # category names
    category_label: str = None
    merchant: str = None
    receipt_item: str = None
    channel: str = None                              # 'mtn', 'airtel', 'bank', 'cash', 'all'
    period: Period = None
    period_explicit: bool = False
    amount: float = None
    followup: bool = False


@dataclass
class Reply:
    answer: str
    suggestions: list = field(default_factory=list)
    context: dict = field(default_factory=dict)


def _month_period(year, month, label):
    last = calendar.monthrange(year, month)[1]
    return Period(date(year, month, 1), date(year, month, last), label, 'month')


def parse_period(text, today, salary_day):
    """(Period, explicit) for the time words in `text`; this month by default."""
    this_month = _month_period(today.year, today.month, 'this month')

    if re.search(r'\btoday\b', text):
        return Period(today, today, 'today', 'day'), True
    if re.search(r'\byesterday\b', text):
        day = today - timedelta(days=1)
        return Period(day, day, 'yesterday', 'day'), True
    match = re.search(r'\b(?:last|past|previous)\s+(\d{1,3})\s+(day|week|month)s?\b', text)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        days = n * {'day': 1, 'week': 7, 'month': 30}[unit]
        return Period(today - timedelta(days=days - 1), today,
                      f'in the last {n} {unit}{"s" if n > 1 else ""}', 'days'), True
    if re.search(r'\b(last|previous)\s+(pay\s*)?cycle\b', text):
        start = behavioural.last_salary_date(today, salary_day)
        prev_start = behavioural.last_salary_date(start - timedelta(days=1), salary_day)
        return Period(prev_start, start - timedelta(days=1),
                      'the previous pay cycle', 'cycle'), True
    if re.search(r'\b(this|current)\s+(pay\s*)?cycle\b|\bsince\s+pay\s*day\b'
                 r'|\bpay\s*cycle\b', text):
        start = behavioural.last_salary_date(today, salary_day)
        return Period(start, today, f'this pay cycle (since {start:%d %b})',
                      'cycle'), True
    if re.search(r'\bthis\s+week\b', text):
        start = today - timedelta(days=today.weekday())
        return Period(start, today, 'this week', 'week'), True
    if re.search(r'\blast\s+week\b', text):
        end = today - timedelta(days=today.weekday() + 1)
        return Period(end - timedelta(days=6), end, 'last week', 'week'), True
    if re.search(r'\b(last|previous)\s+month\b', text):
        return this_month.previous(), True
    if re.search(r'\bthis\s+year\b', text):
        return Period(date(today.year, 1, 1), today, 'this year', 'year'), True
    if re.search(r'\b(all\s+time|ever|altogether|since\s+i\s+started)\b', text):
        return Period(date(1900, 1, 1), today, 'in total', 'all'), True

    names = '|'.join(sorted(MONTHS, key=len, reverse=True))
    match = re.search(rf'\b({names})\b(?:\s+(\d{{4}}))?', text)
    month, year = None, None
    if match:
        month, year = MONTHS[match.group(1)], match.group(2)
    elif _MAY.search(text):
        month, year = 5, (re.search(r'\bmay\s+(\d{4})', text) or [None, None])[1]
    if month:
        year = int(year) if year else (today.year if month <= today.month
                                       else today.year - 1)
        return _month_period(year, month, date(year, month, 1).strftime('in %B %Y')), True
    if re.search(r'\bthis\s+month\b|\bso\s+far\b', text):
        return this_month, True
    return this_month, False


def parse_amount(text):
    """The money amount in a question ("k800", "1,200", "2.5k"), or None."""
    pattern = (r'(?:\b(?:k|zmw)\s?)?(\d[\d,]*(?:\.\d+)?)\s*'
               r'(k\b|thousand\b)?(?!\s*(?:days?|weeks?|months?|years?|%|x\b|times))')
    for match in re.finditer(pattern, text):
        try:
            value = float(match.group(1).replace(',', ''))
        except ValueError:
            continue
        if match.group(2):
            value *= 1000
        if value >= 1 and not re.fullmatch(r'(19|20)\d\d', match.group(1)):
            return value
    return None


def _singular(word):
    return word[:-1] if word.endswith('s') and len(word) > 3 else word


def match_categories(text, category_names):
    """(category names, label) mentioned in `text`, or ([], None).

    Real category names win over synonyms, and the longest phrase wins, so
    "dining out" beats "out" and "airtime & data" beats "data".
    """
    lookup = {}
    for name in category_names:
        key = name.lower()
        lookup[key] = [name]
        lookup[key.replace('&', 'and')] = [name]
        lookup[_singular(key)] = [name]
    available = set(category_names)
    for word, targets in CATEGORY_SYNONYMS.items():
        kept = [t for t in targets if t in available]
        if kept and word not in lookup:
            lookup[word] = kept
    for phrase in sorted(lookup, key=len, reverse=True):
        if re.search(rf'(?<![a-z]){re.escape(phrase)}(?![a-z])', text):
            names = lookup[phrase]
            label = names[0] if len(names) == 1 else phrase
            return names, label
    return [], None


def match_merchant(text, descriptions):
    """The longest phrase (1-3 words) in `text` found in a transaction
    description, ignoring common words; or None."""
    words = [w for w in re.findall(r'[a-z0-9]+', text)]
    phrases = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            gram = words[i:i + size]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            phrase = ' '.join(gram)
            if len(phrase) >= 3 and not phrase.isdigit():
                phrases.append(phrase)
    for phrase in phrases:
        pattern = re.compile(rf'\b{re.escape(phrase)}\b')
        if any(pattern.search(d) for d in descriptions):
            return phrase
    return None


CHANNEL_PATTERNS = (
    ('all', r'\b(mtn\s*(?:momo|money)?\s*(?:vs\.?|versus|and|or)\s*airtel'
            r'|airtel\s*(?:money)?\s*(?:vs\.?|versus|and|or)\s*mtn'
            r'|by\s+(wallet|channel|payment\s+method)|all\s+wallets|mobile\s+money\s+breakdown)\b'),
    ('mtn', r'\b(mtn\s*momo|mtn\s*money|momo\s*pay|momo)\b'),
    ('airtel', r'\b(airtel\s*money)\b'),
    ('cash', r'\b(cash\s+(?:expenses?|spending|spent|transactions?|purchases?)'
             r'|in\s+cash|with\s+cash|paid\s+cash|spent\s+cash|how\s+much\s+cash)\b'),
    ('bank', r'\b(bank\s+(?:sync|card|account)\s*(?:spending|transactions?|expenses?)?)\b'),
)


def _without_channel(text):
    for _, pattern in CHANNEL_PATTERNS:
        text = re.sub(pattern, ' ', text)
    return ' '.join(text.split())


def match_channel(text):
    for key, pattern in CHANNEL_PATTERNS:
        if re.search(pattern, text):
            return key
    return None


def match_receipt_item(text, item_names):
    """Match a 1-3 word phrase in `text` against known Receipt_Item names."""
    if not item_names:
        return None
    words = [w for w in re.findall(r'[a-z0-9]+', text)]
    phrases = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            gram = words[i:i + size]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            phrase = ' '.join(gram)
            if len(phrase) >= 3 and not phrase.isdigit():
                phrases.append(phrase)
    for phrase in phrases:
        pattern = re.compile(rf'\b{re.escape(phrase)}\b')
        if any(pattern.search(name) for name in item_names):
            return phrase
    return None


INTENTS = (
    ('help', r'\b(help|what can you (do|answer)|what (can|should) i ask)\b'),
    ('whatif', r'\bwhat\s+if\b|\bif\s+i\s+(cut|reduce|trim|cancel|drop|stop|save)\b'
               r'|\bcancel(ling)?\s+[a-z0-9]+'),
    ('cutback', r'\b(overspend(ing)?|over\s+budget|cut\s*back|where\s+can\s+i\s+(save|cut|trim)'
                r'|unusual\s+(expenses?|spending)|alerts?|nudges?)\b'),
    ('receipt', r'\bwhat\s+did\s+i\s+buy\b|\breceipt\s+items?\b|\bitemised\b'),
    ('left', r'\bsafe\s+to\s+spend\b|\bcan\s+i\s+spend\s+(per|a)\s+day\b'
             r'|\b(per|a)\s+day\s+until\s+pay\s*day\b'),
    ('payday', r'\bpay\s*day\b|\bget paid\b|\bnext salary\b|\bsalary (come|arrive|land)'),
    ('afford', r'\bafford\b|\bcan i (buy|spend|get)\b|\bshould i (buy|spend|get)\b'),
    ('compare', r'\bcompare|\bversus\b|\bvs\b|\b(more|less) than (last|previous)'
                r'|\bchange(d)? (from|since)\b|\bup or down\b'),
    ('left', r'\b(left|remaining|remain|budget|allowance|limit)\b'),
    ('top', r'\b(biggest|largest|top|highest|most)\b|\bwhere (does|did|is) my money'),
    ('forecast', r'\b(forecast|predict|projection|expect)|\bnext month\b'),
    ('goals', r'\bgoals?\b|\bsaving(s)?\b'),
    ('subscriptions', r'\bsubscriptions?\b|\brecurring\b'),
    ('income', r'\b(income|earn(ed)?|received|credits?)\b'),
    ('recent', r'\b(recent|latest)\b|\blast \d* ?(transactions|purchases|payments)\b'
               r'|\bshow (me )?(my )?(transactions|purchases|payments)\b'),
    ('count', r'\bhow (many times|often)\b|\bhow many (transactions|purchases|times)\b'),
    ('average', r'\baverage\b|\bper day\b|\bdaily\b|\ba day\b'),
    ('spend', r'\b(spend|spent|spending|cost|costs|paid|pay for|how much)\b'),
)
# "... with last month" names the baseline of a comparison, not the period
# asked about; it is removed before the period is parsed.
_COMPARE_TARGET = re.compile(
    r'\b(with|to|than|against|vs\.?|versus)\s+(the\s+)?(last|previous)\s+'
    r'(pay\s*)?(month|cycle|week)\b')

FOLLOWUP = re.compile(r'^\s*(and|what about|how about|what of|and for|same for|also)\b'
                      r'|^\s*(for|in|on|at)\b')


def parse(question, category_names, descriptions, today, salary_day,
          context=None, item_names=None):
    text = ' '.join((question or '').lower().split())
    query = Query(text=text)
    query.channel = match_channel(text)
    if query.channel != 'all':
        # The wallet phrase is removed first, so "airtel money" or "momo" is
        # never read as a category ("Airtime & Data"), while "groceries with
        # cash" still keeps its category.
        category_text = _without_channel(text) if query.channel else text
        query.categories, query.category_label = match_categories(
            category_text, category_names)
    query.period, query.period_explicit = parse_period(
        _COMPARE_TARGET.sub(' ', text), today, salary_day)
    baseline = _COMPARE_TARGET.search(text)
    if baseline and not query.period_explicit:
        # "vs last cycle" compares this cycle; "vs last week" this week.
        unit = 'cycle' if baseline.group(5) == 'cycle' else baseline.group(5)
        query.period, _ = parse_period(f'this {unit}', today, salary_day)
        query.period_explicit = True
    query.amount = parse_amount(text)
    for intent, pattern in INTENTS:
        if re.search(pattern, text):
            query.intent = intent
            break
    if query.intent == 'whatif' and re.search(
            r'\b(buy|get|purchase|pay\s+for|spend|order)\b', text):
        query.intent = 'afford'
    if query.channel == 'all' and query.intent in (None, 'spend', 'compare'):
        query.intent = 'channels'
    if (not query.categories and not query.channel
            and query.intent not in ('help', 'payday', 'cutback', 'whatif')):
        query.merchant = match_merchant(text, descriptions)
    if (not query.categories and not query.merchant and not query.channel
            and query.intent not in ('help', 'payday', 'cutback', 'whatif')):
        query.receipt_item = match_receipt_item(text, item_names or set())
        if query.receipt_item and query.intent in (None, 'spend', 'count'):
            query.intent = 'receipt'

    context = context or {}
    has_subject = bool(query.categories or query.merchant or query.receipt_item or query.channel)
    query.followup = bool(context) and (
        bool(FOLLOWUP.search(text))
        or (query.intent is None and (has_subject or query.period_explicit)))
    # "Compare with last month" right after "how much on groceries?" means
    # groceries: these intents take the previous subject when they name none.
    if (context and not has_subject and not query.followup
            and query.intent in ('compare', 'average', 'count')):
        query.categories = context.get('categories') or []
        query.category_label = context.get('category_label')
        query.merchant = context.get('merchant')
    if query.followup:
        query.intent = query.intent or context.get('intent')
        if not has_subject:
            query.categories = context.get('categories') or []
            query.category_label = context.get('category_label')
            query.merchant = context.get('merchant')
        if not query.period_explicit and context.get('period'):
            start, end, label, kind = context['period']
            query.period = Period(date.fromisoformat(start), date.fromisoformat(end),
                                  label, kind)
    if query.intent is None and (has_subject or query.period_explicit):
        query.intent = 'spend'
    return query


# --- Data --------------------------------------------------------------------

def _money(value):
    return f'<strong>K{value:,.2f}</strong>'


def _subject(query):
    if query.categories:
        return f'<strong>{escape(query.category_label)}</strong>'
    if query.merchant:
        return f'<strong>{escape(query.merchant.title())}</strong>'
    return None


def _filter_sql(query, alias='t'):
    sql = (f"{alias}.user_id = ? AND {alias}.transaction_type = 'DEBIT' "
           f"AND {database.spending_filter(alias)} "
           f"AND {alias}.transaction_date BETWEEN ? AND ?")
    return sql


_CHANNEL_LABELS = {
    'mtn': 'MTN MoMo',
    'airtel': 'Airtel Money',
    'bank': 'Bank',
    'cash': 'Cash',
}


def _row_channel(row):
    ih = (row.get('import_hash') or '').lower()
    desc = (row.get('description') or '').lower()
    if ih.startswith('sync-momo-mtn-') or 'mtn momo' in desc or 'momo pay' in desc:
        return 'mtn'
    if ih.startswith('sync-momo-airtel-') or 'airtel money' in desc:
        return 'airtel'
    if ih.startswith('manual-'):
        return 'cash'
    if ih.startswith('sync-bank-'):
        return 'bank'
    return 'bank'


def _link(href, label):
    return f' <a class="chat-link" href="{escape(href)}">{escape(label)} →</a>'


def _txns_link(query):
    params = {}
    if query.categories and len(query.categories) == 1:
        params['category'] = query.categories[0]
    elif query.merchant:
        params['q'] = query.merchant
    if query.period and query.period.kind == 'month':
        params['month'] = query.period.start.strftime('%Y-%m')
    qs = ('?' + urlencode(params)) if params else ''
    return _link(f'/transactions{qs}', 'View transactions')


def _debits(conn, user_id, query, period=None, limit=None, order='t.transaction_date DESC'):
    period = period or query.period
    params = [user_id, period.start.isoformat(), period.end.isoformat()]
    sql = ('SELECT t.transaction_id, t.transaction_date, t.description, t.amount, '
           't.import_hash, c.category_name FROM Transaction_Record t '
           'LEFT JOIN Category c ON t.category_id = c.category_id '
           f'WHERE {_filter_sql(query)}')
    if query.categories:
        sql += f" AND c.category_name IN ({','.join('?' * len(query.categories))})"
        params += query.categories
    sql += f' ORDER BY {order}'
    rows = [dict(r) for r in conn.execute(sql, params)]
    if query.channel and query.channel in _CHANNEL_LABELS:
        rows = [r for r in rows if _row_channel(r) == query.channel]
    if query.merchant:
        pattern = re.compile(rf'\b{re.escape(query.merchant)}\b')
        rows = [r for r in rows
                if pattern.search(categorization.normalise(r['description']))]
    return rows[:limit] if limit else rows


def _total(rows):
    return round(sum(float(r['amount']) for r in rows), 2)


def _budget(conn, user, today):
    if not behavioural.learning_status(conn, user, today)['complete']:
        return None
    return recommendation.build_budget(conn, user, today)


def _next_payday(today, salary_day):
    last = behavioural.last_salary_date(today, salary_day)
    probe = last + timedelta(days=32)
    nxt = behavioural.last_salary_date(probe, salary_day)
    return nxt if nxt > today else behavioural.last_salary_date(
        nxt + timedelta(days=32), salary_day)


# --- Answers -----------------------------------------------------------------

def _answer_spend(conn, user, query, today):
    rows = _debits(conn, user['user_id'], query)
    total = _total(rows)
    subject = _subject(query)
    if query.channel and query.channel in _CHANNEL_LABELS:
        where = f' via <strong>{_CHANNEL_LABELS[query.channel]}</strong>'
        if query.categories:
            where = f' on {subject}' + where
    else:
        where = f' on {subject}' if query.categories else (f' at {subject}' if subject else '')
    if not rows:
        return f'No spending{where} {escape(query.period.label)}.'
    text = (f'You spent {_money(total)}{where} {escape(query.period.label)}, '
            f'across {len(rows)} transaction{"s" if len(rows) != 1 else ""}.')
    if query.period.label == 'this month':
        budget = _budget(conn, user, today)
        if budget and query.categories:
            rows_b = [r for r in budget['rows'] if r['category'] in query.categories]
            allocation = sum(r['recommended'] for r in rows_b)
            if allocation:
                left = allocation - total
                text += (f' That is {total / allocation * 100:.0f}% of its '
                         f'{_money(allocation)} budget, ' +
                         (f'with {_money(left)} left.' if left >= 0 else
                          f'{_money(-left)} over.'))
        elif not subject and not query.channel:
            salary = float(user['salary_amount'])
            if salary:
                text += f' That is {total / salary * 100:.0f}% of your salary.'
    return text + _txns_link(query)


def _answer_compare(conn, user, query, today):
    current = query.period
    previous = current.previous()
    now = _total(_debits(conn, user['user_id'], query, current))
    before = _total(_debits(conn, user['user_id'], query, previous))
    subject = _subject(query)
    on = f' on {subject}' if query.categories else (f' at {subject}' if subject else '')
    head = (f'You spent {_money(now)}{on} {escape(current.label)} and '
            f'{_money(before)} {escape(previous.label)}.')
    if not before:
        return head
    change = (now - before) / before * 100
    if abs(change) < 1:
        return head + ' About the same.'
    direction = 'more' if change > 0 else 'less'
    text = head + f' That is <strong>{abs(change):.0f}% {direction}</strong>'
    if current.label == 'this month' and current.end >= today:
        text += ', though this month is not over yet'
    return text + '.'


def _answer_left(conn, user, query, today):
    budget = _budget(conn, user, today)
    payday = _next_payday(today, int(user['salary_day']))
    days = max(1, (payday - today).days)
    if budget is None:
        start = behavioural.last_salary_date(today, int(user['salary_day']))
        cycle = Period(start, today, 'this pay cycle', 'cycle')
        spent = _total(_debits(conn, user['user_id'], Query(text=''), cycle))
        left = float(user['salary_amount']) - spent
        return (f'Budgets start once the learning phase is complete. Meanwhile: '
                f'of your {_money(float(user["salary_amount"]))} salary you have '
                f'{_money(left)} left this pay cycle, about {_money(max(0, left) / days)} '
                f'a day for the {days} day{"s" if days != 1 else ""} until payday.')
    if query.categories:
        rows = [r for r in budget['rows'] if r['category'] in query.categories]
        if not rows:
            return (f'There is no budget for <strong>{escape(query.category_label)}'
                    f'</strong> this month.')
        allocation = sum(r['recommended'] for r in rows)
        spent = sum(r['spent'] for r in rows)
        left = allocation - spent
        if left < 0:
            return (f'You are {_money(-left)} over your {_money(allocation)} '
                    f'<strong>{escape(query.category_label)}</strong> budget this month.')
        return (f'You have {_money(left)} left of your {_money(allocation)} '
                f'<strong>{escape(query.category_label)}</strong> budget this month '
                f'(spent {_money(spent)}).')
    left = sum(r['remaining'] for r in budget['rows'])
    over = [r['category'] for r in budget['rows'] if r['status'] == 'over']
    text = (f'You have {_money(max(0, left))} left across your budgets this month, '
            f'about {_money(max(0, left) / days)} a day until payday on '
            f'{payday:%d %B}. Your savings target is {_money(budget["recommended_savings"])}.')
    if over:
        text += (' Already over budget: '
                 + ', '.join(f'<strong>{escape(c)}</strong>' for c in over) + '.')
    return text + _link('/budget', 'Open budget')


def _answer_afford(conn, user, query, today):
    budget = _budget(conn, user, today)
    amount = query.amount
    if budget is None:
        start = behavioural.last_salary_date(today, int(user['salary_day']))
        cycle = Period(start, today, 'this pay cycle', 'cycle')
        free = float(user['salary_amount']) - _total(
            _debits(conn, user['user_id'], Query(text=''), cycle))
        pool, label = free, 'what is left of this pay cycle\'s salary'
    elif query.categories:
        rows = [r for r in budget['rows'] if r['category'] in query.categories]
        pool = sum(max(0.0, r['remaining']) for r in rows)
        label = f'your <strong>{escape(query.category_label)}</strong> budget'
    else:
        pool = sum(max(0.0, r['remaining']) for r in budget['rows']
                   if r['type'] == 'DISCRETIONARY')
        label = 'your discretionary (wants) budget'
    if amount is None:
        return (f'You have {_money(max(0, pool))} left in {label}. Tell me the price, '
                f'e.g. <em>"can I afford K800?"</em>, and I will check it.')
    if amount <= pool:
        return (f'<strong>Yes.</strong> {_money(amount)} fits in {label}, '
                f'leaving {_money(pool - amount)}.')
    savings = budget['recommended_savings'] if budget else 0.0
    if budget and amount <= pool + savings:
        return (f'<strong>Only by dipping into savings.</strong> {label[0].upper()}'
                f'{label[1:]} has {_money(max(0, pool))} left, so {_money(amount)} '
                f'would take {_money(amount - max(0, pool))} from this month\'s '
                f'{_money(savings)} savings target.')
    return (f'<strong>Not this month.</strong> {_money(amount)} is more than the '
            f'{_money(max(0, pool))} left in {label}. Waiting until payday on '
            f'{_next_payday(today, int(user["salary_day"])):%d %B} would be safer.')


def _answer_top(conn, user, query, today):
    rows = _debits(conn, user['user_id'], query)
    if not rows:
        return f'You have no spending {escape(query.period.label)} yet.'
    total = _total(rows)
    by_merchant = bool(re.search(r'\b(shops?|stores?|merchants?|places?)\b',
                                 query.text)) or bool(query.categories)
    groups = {}
    for row in rows:
        key = row['description'] if by_merchant else (row['category_name'] or 'Uncategorised')
        groups[key] = groups.get(key, 0.0) + float(row['amount'])
    top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:3]
    parts = [f'<strong>{escape(name)}</strong> {_money(value)} '
             f'({value / total * 100:.0f}%)' for name, value in top]
    kind = 'places' if by_merchant else 'categories'
    return (f'Your top {kind} {escape(query.period.label)}: ' + '; '.join(parts)
            + f'. Total {_money(total)}.')


def _answer_forecast(conn, user, query, today):
    forecasts = forecasting.generate_forecasts(conn, user['user_id'])
    if not forecasts:
        return ("I need a few complete months of history before I can forecast "
                "your spending. Keep importing statements and I'll start soon.")
    if query.categories:
        found = [(n, f) for n, f in forecasts.items() if n in query.categories]
        if not found:
            return (f'There is not enough history to forecast '
                    f'<strong>{escape(query.category_label)}</strong> yet.')
        amount = sum(f['amount'] for _, f in found)
        method = found[0][1]['method'] if len(found) == 1 else 'per-category models'
        return (f'I forecast about {_money(amount)} on '
                f'<strong>{escape(query.category_label)}</strong> next month '
                f'({escape(method)}).')
    total = sum(f['amount'] for f in forecasts.values())
    top = sorted(forecasts.items(), key=lambda kv: kv[1]['amount'], reverse=True)[:3]
    return (f'I forecast about {_money(total)} of spending next month. Largest: '
            + ', '.join(f'<strong>{escape(n)}</strong> {_money(f["amount"])}'
                        for n, f in top) + '.')


def _answer_goals(conn, user, query, today):
    goals = recommendation.goal_requirements(conn, user['user_id'], today)
    if not goals:
        return ("You haven't set any savings goals yet. Add one on the Savings Goals "
                "page and I'll track it here.")
    parts = [(f"<strong>{escape(g['goal_name'])}</strong> is {g['progress']:.0f}% funded "
              f"({_money(g['monthly_required'])}/month still needed"
              f"{', overdue' if g['overdue'] else ''})") for g in goals[:3]]
    return 'Here is where your savings goals stand: ' + '; '.join(parts) + '.' + _link('/savings', 'Manage goals')


def _answer_subscriptions(conn, user, query, today):
    summary = subscriptions.subscription_summary(
        subscriptions.detect_subscriptions(conn, user['user_id'], persist=False))
    if not summary['count']:
        return ("I haven't found any recurring subscriptions yet. They show up once "
                "a charge repeats at a steady amount each month.")
    names = ', '.join(f"<strong>{escape(e['label'])}</strong> {_money(e['monthly_amount'])}"
                      for e in summary['entries'][:5])
    return (f'You have {summary["count"]} recurring charge'
            f'{"s" if summary["count"] != 1 else ""} costing {_money(summary["monthly_total"])}'
            f' a month ({_money(summary["annual_total"])} a year): {names}. '
            'Cancelling any you no longer use is the easiest saving available.')


def _answer_income(conn, user, query, today):
    period = query.period
    rows = conn.execute(
        "SELECT amount, is_salary FROM Transaction_Record WHERE user_id = ? "
        "AND transaction_type = 'CREDIT' AND transaction_date BETWEEN ? AND ?",
        (user['user_id'], period.start.isoformat(), period.end.isoformat())).fetchall()
    total = round(sum(float(r['amount']) for r in rows), 2)
    salary = round(sum(float(r['amount']) for r in rows if r['is_salary']), 2)
    if not rows:
        return f'No money came in {escape(period.label)}.'
    return (f'You received {_money(total)} {escape(period.label)}, of which '
            f'{_money(salary)} was salary.')


def _answer_payday(conn, user, query, today):
    payday = _next_payday(today, int(user['salary_day']))
    days = (payday - today).days
    return (f'Your next payday is <strong>{payday:%A %d %B}</strong>, in '
            f'{days} day{"s" if days != 1 else ""}.')


def _answer_recent(conn, user, query, today):
    period = query.period if query.period_explicit else Period(
        date(1900, 1, 1), today, 'recently', 'all')
    rows = _debits(conn, user['user_id'], query, period, limit=5)
    if not rows:
        return 'I found no matching transactions.'
    items = ''.join(f"<li>{r['transaction_date'][:10]}: {escape(r['description'])} "
                    f"{_money(float(r['amount']))}</li>" for r in rows)
    return f'Your latest matching transactions:<ul>{items}</ul>'


def _answer_count(conn, user, query, today):
    rows = _debits(conn, user['user_id'], query)
    subject = _subject(query)
    where = f' on {subject}' if query.categories else (f' at {subject}' if subject else '')
    if not rows:
        return f'You made no purchases{where} {escape(query.period.label)}.'
    total = _total(rows)
    return (f'You made <strong>{len(rows)}</strong> purchase{"s" if len(rows) != 1 else ""}'
            f'{where} {escape(query.period.label)}, {_money(total)} in total '
            f'(about {_money(total / len(rows))} each).')


def _answer_average(conn, user, query, today):
    rows = _debits(conn, user['user_id'], query)
    end = min(query.period.end, today)
    days = max(1, (end - query.period.start).days + 1) if query.period.kind != 'all' \
        else max(1, (today - min((date.fromisoformat(r['transaction_date'][:10])
                                  for r in rows), default=today)).days + 1)
    subject = _subject(query)
    on = f' on {subject}' if query.categories else (f' at {subject}' if subject else '')
    return (f'You spent about {_money(_total(rows) / days)} a day{on} '
            f'{escape(query.period.label)}: {_money(_total(rows))} over {days} days.')


_WHATIF_WORDS = {'what', 'cancel', 'cancelling', 'cut', 'trim', 'reduce', 'drop',
                 'stop', 'save', 'month', 'monthly', 'year', 'subscription'}


def _answer_whatif(conn, user, query, today):
    """Simulate cancelling a subscription or trimming spending and show its
    impact on annual savings and the user's nearest active savings goal."""
    monthly_saving = None
    action_label = None

    # 1. Check if the user mentioned a detected subscription to cancel.
    subs = subscriptions.detect_subscriptions(conn, user['user_id'], persist=False)
    asked = {w for w in re.findall(r'[a-z0-9]+', query.text)
             if len(w) >= 3 and w not in STOPWORDS and w not in _WHATIF_WORDS}
    for sub in subs:
        names = set(re.findall(r'[a-z0-9]+', categorization.normalise(
            f"{sub['label']} {sub['key']}")))
        if asked & names:
            monthly_saving = float(sub['monthly_amount'])
            action_label = f"cancelling <strong>{escape(sub['label'])}</strong>"
            break
    target = re.search(r'\bcancel(?:ling)?\s+(?:my\s+|the\s+)?([a-z0-9][a-z0-9 ]{1,30}?)(?:\?|$|\s+(?:sub|and|then)\b)',
                       query.text)
    if monthly_saving is None and target and not query.amount and not query.categories:
        listed = ', '.join(f"<strong>{escape(s_['label'])}</strong> "
                           f"({_money(s_['monthly_amount'])}/month)" for s_ in subs[:5])
        return (f"I can't find a recurring charge for "
                f"<strong>{escape(target.group(1).strip())}</strong>. "
                + (f'The subscriptions I have detected are {listed}.' if subs else
                   'I have not detected any subscriptions yet; they appear once '
                   'the same charge repeats each month.')
                + _link('/transactions', 'View transactions'))

    # 2. Otherwise use an explicit K-amount ("cut dining out by K300").
    if monthly_saving is None and query.amount:
        monthly_saving = float(query.amount)
        if query.categories:
            action_label = (f"cutting <strong>{escape(query.category_label)}</strong> "
                            f"by {_money(monthly_saving)}/month")
        else:
            action_label = f"saving an extra {_money(monthly_saving)}/month"

    # 3. Or default to a realistic 20% trim on a named category.
    if monthly_saving is None and query.categories:
        budget = _budget(conn, user, today)
        if budget:
            cat_rows = [r for r in budget['rows'] if r['category'] in query.categories]
            base = sum(r['recommended'] for r in cat_rows)
            if base > 0:
                monthly_saving = round(base * 0.20, 2)
                action_label = (f"trimming <strong>{escape(query.category_label)}</strong> "
                                f"by 20% ({_money(monthly_saving)}/month)")

    if not monthly_saving:
        return ('Try a specific scenario, e.g. <em>"what if I cancel Netflix?"</em> '
                'or <em>"if I cut dining out by K300?"</em>, and I will work out '
                'the yearly savings and goal impact.')

    annual = monthly_saving * 12
    text = (f'By {action_label}, you would free up {_money(monthly_saving)} a month '
            f'— <strong>{_money(annual)} a year</strong>.')

    budget = _budget(conn, user, today)
    goals = [g for g in recommendation.goal_requirements(conn, user['user_id'], today)
             if g['shortfall'] > 0]
    if goals:
        goal = goals[0]
        base_rate = (budget['recommended_savings'] if budget else 0.0)
        new_rate = base_rate + monthly_saving
        new_months = max(1, math.ceil(goal['shortfall'] / new_rate))
        if base_rate > 0:
            old_months = max(1, math.ceil(goal['shortfall'] / base_rate))
            saved_months = old_months - new_months
            if saved_months > 0:
                text += (f" Put toward <strong>{escape(goal['goal_name'])}</strong>, "
                         f"that reaches your goal in <strong>{new_months} month"
                         f"{'s' if new_months != 1 else ''}</strong> instead of "
                         f"{old_months} ({saved_months} month"
                         f"{'s' if saved_months != 1 else ''} sooner).")
            else:
                text += (f" Put toward <strong>{escape(goal['goal_name'])}</strong>, "
                         f"it covers {monthly_saving / goal['shortfall'] * 100:.0f}% "
                         f"of the remaining shortfall each month.")
        else:
            text += (f" Put toward <strong>{escape(goal['goal_name'])}</strong>, "
                     f"it would fund the remaining {_money(goal['shortfall'])} in "
                     f"about <strong>{new_months} month"
                     f"{'s' if new_months != 1 else ''}</strong>.")
    return text + _link('/savings', 'Manage goals')


def _answer_cutback(conn, user, query, today):
    """Surface overspending categories, 80% nudges and top discretionary trim targets."""
    budget = _budget(conn, user, today)
    if budget is None:
        rows = _debits(conn, user['user_id'], query)
        if not rows:
            return 'No spending recorded yet to spot cutbacks.'
        groups = {}
        for r in rows:
            cat = r['category_name'] or 'Uncategorised'
            groups[cat] = groups.get(cat, 0.0) + float(r['amount'])
        top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts = ', '.join(f'<strong>{escape(c)}</strong> ({_money(v)})' for c, v in top)
        return (f'Budgets unlock after the learning phase. So far your largest '
                f'spending areas are {parts}.')

    over = [r for r in budget['rows'] if r['status'] == 'over']
    warn = [r for r in budget['rows'] if r['status'] == 'warning']
    disc = sorted(
        [r for r in budget['rows'] if r['type'] == 'DISCRETIONARY' and r['spent'] > 0],
        key=lambda r: r['spent'], reverse=True)

    pieces = []
    if over:
        names = ', '.join(f"<strong>{escape(r['category'])}</strong> "
                          f"({_money(r['spent'] - r['recommended'])} over)"
                          for r in over[:3])
        pieces.append(f'Already over budget this month: {names}.')
    if warn:
        names = ', '.join(f"<strong>{escape(r['category'])}</strong> "
                          f"({r['spent'] / r['recommended'] * 100:.0f}% used)"
                          for r in warn[:3])
        pieces.append(f'Close to their limit (≥80%): {names}.')
    if disc:
        best = disc[0]
        trim = round(best['spent'] * 0.20, 2)
        pieces.append(f"Easiest discretionary trim: cutting <strong>{escape(best['category'])}</strong> "
                      f"by 20% would save about {_money(trim)}/month ({_money(trim * 12)}/year).")
    if not pieces:
        pieces.append('Every category is currently comfortably within its budget this month.')
    return ' '.join(pieces) + _link('/budget', 'Open budget')


def _qty(row):
    try:
        return max(1, int(row['quantity'] or 1))
    except (TypeError, ValueError):
        return 1


def _answer_receipt(conn, user, query, today):
    """Answer item-level questions from the Receipt_Item table."""
    period = query.period if query.period_explicit else Period(
        date(1900, 1, 1), today, 'in total', 'all')
    rows = conn.execute(
        'SELECT ri.item_name, ri.amount, ri.quantity, t.transaction_date, t.description '
        'FROM Receipt_Item ri '
        'JOIN Transaction_Record t ON ri.transaction_id = t.transaction_id '
        'WHERE t.user_id = ? AND t.transaction_date BETWEEN ? AND ? '
        'ORDER BY t.transaction_date DESC, ri.item_id DESC',
        (user['user_id'], period.start.isoformat(), period.end.isoformat())
    ).fetchall()

    if query.merchant:
        pat = re.compile(rf'\b{re.escape(query.merchant)}\b')
        matched = [r for r in rows if pat.search(categorization.normalise(r['description']))]
        if not matched:
            return (f'No itemised receipt items recorded for '
                    f'<strong>{escape(query.merchant.title())}</strong> '
                    f'{escape(period.label)}. Scan a receipt or add items on the '
                    f'Transactions page.')
        total = sum(float(r['amount']) for r in matched)
        items_html = ''.join(
            f"<li>{r['transaction_date'][:10]}: {_qty(r)}× "
            f"{escape(r['item_name'])} {_money(float(r['amount']))}</li>"
            for r in matched[:8])
        return (f'Itemised purchases at <strong>{escape(query.merchant.title())}</strong> '
                f'{escape(period.label)} ({_money(total)} across {len(matched)} '
                f'line{"s" if len(matched) != 1 else ""}):<ul>{items_html}</ul>'
                + _txns_link(query))

    if query.receipt_item:
        pat = re.compile(rf'\b{re.escape(query.receipt_item)}\b')
        matched = [r for r in rows if pat.search(categorization.normalise(r['item_name']))]
        if not matched:
            return (f'No itemised receipt lines matching '
                    f'<strong>{escape(query.receipt_item)}</strong> {escape(period.label)}.')
        total = sum(float(r['amount']) for r in matched)
        qty = sum(_qty(r) for r in matched)
        return (f'You spent {_money(total)} on <strong>{escape(query.receipt_item.title())}</strong> '
                f'{escape(period.label)} ({qty} item{"s" if qty != 1 else ""} across '
                f'{len(matched)} receipt line{"s" if len(matched) != 1 else ""}).')

    if not rows:
        return ('No receipt items have been scanned or added yet. Use '
                '<strong>Scan Receipt</strong> on any debit in Transactions to '
                'track individual items.')
    items_html = ''.join(
        f"<li>{r['transaction_date'][:10]} ({escape(r['description'])}): "
        f"{_qty(r)}× {escape(r['item_name'])} {_money(float(r['amount']))}</li>"
        for r in rows[:6])
    return f'Your latest itemised receipt purchases:<ul>{items_html}</ul>'


def _answer_channels(conn, user, query, today):
    """Compare spending across MTN MoMo, Airtel Money, Bank and Cash."""
    rows = _debits(conn, user['user_id'], Query(text='', period=query.period))
    if not rows:
        return f'No spending recorded {escape(query.period.label)}.'
    totals = {'mtn': 0.0, 'airtel': 0.0, 'bank': 0.0, 'cash': 0.0}
    counts = {'mtn': 0, 'airtel': 0, 'bank': 0, 'cash': 0}
    for r in rows:
        ch = _row_channel(r)
        totals[ch] += float(r['amount'])
        counts[ch] += 1
    grand = sum(totals.values())
    active = [(ch, totals[ch], counts[ch]) for ch in ('mtn', 'airtel', 'bank', 'cash')
              if totals[ch] > 0]
    active.sort(key=lambda item: item[1], reverse=True)
    parts = [f"<strong>{_CHANNEL_LABELS[ch]}</strong> {_money(val)} "
             f"({val / grand * 100:.0f}%, {cnt} txn{'s' if cnt != 1 else ''})"
             for ch, val, cnt in active]
    return (f'Spending by wallet &amp; channel {escape(query.period.label)}: '
            + '; '.join(parts) + f'. Total {_money(grand)}.'
            + _link('/transactions', 'View transactions'))


HELP = ("I can answer questions about your own money, entirely on this device. Try: "
        "<em>how much did I spend on transport last month?</em>, "
        "<em>how much do I have left?</em>, <em>can I afford K800?</em>, "
        "<em>compare groceries with last month</em>, <em>my biggest expenses this "
        "cycle</em>, <em>how much at Shoprite?</em>, <em>when is payday?</em>, "
        "<em>forecast for next month</em>, <em>my subscriptions</em> or "
        "<em>how are my goals?</em> You can follow up with <em>and last month?</em>")

UNKNOWN = ("I couldn't quite catch that. " + HELP.split('. ', 1)[1])

ANSWERS = {
    'spend': _answer_spend, 'compare': _answer_compare, 'left': _answer_left,
    'afford': _answer_afford, 'top': _answer_top, 'forecast': _answer_forecast,
    'goals': _answer_goals, 'subscriptions': _answer_subscriptions,
    'income': _answer_income, 'payday': _answer_payday, 'recent': _answer_recent,
    'count': _answer_count, 'average': _answer_average,
    'whatif': _answer_whatif, 'cutback': _answer_cutback,
    'receipt': _answer_receipt, 'channels': _answer_channels,
}

SUGGESTIONS = {
    'spend': ['And last month?', 'Compare with last month', 'How much do I have left?'],
    'compare': ['My biggest expenses this month', 'How much do I have left?'],
    'left': ['Can I afford K500?', 'When is payday?', 'My biggest expenses this month'],
    'afford': ['How much do I have left?', 'When is payday?'],
    'top': ['Compare with last month', 'Forecast for next month'],
    'forecast': ['How much do I have left?', 'How are my goals?'],
    'goals': ['How much do I have left?', 'Can I afford K500?'],
    'subscriptions': ['How much do I have left?', 'My biggest expenses this month'],
    'payday': ['How much can I spend per day until payday?', 'Daily average this cycle'],
    'whatif': ['Where can I cut back?', 'How are my goals?'],
    'cutback': ['What if I cut dining out by K300?', 'My subscriptions'],
    'receipt': ['My biggest expenses this month', 'How much do I have left?'],
    'channels': ['How much did I spend on MTN MoMo?', 'How much cash did I spend this month?'],
    None: ['How much can I spend per day until payday?', 'Where can I cut back?',
           'MTN MoMo vs Airtel Money this month', 'What if I cancel Netflix?'],
}


def answer(conn, user, question, context=None, today=None):
    """Answer one question. Returns a Reply; never raises on odd input."""
    today = today or date.today()
    category_names = [r['category_name'] for r in conn.execute(
        "SELECT category_name FROM Category WHERE category_name NOT IN "
        "('Income', 'Cash Withdrawal', 'Uncategorised')")]
    descriptions = {categorization.normalise(r['description']) for r in conn.execute(
        "SELECT DISTINCT description FROM Transaction_Record WHERE user_id = ? "
        "AND transaction_type = 'DEBIT'", (user['user_id'],))}
    item_names = {categorization.normalise(r['item_name']) for r in conn.execute(
        "SELECT DISTINCT ri.item_name FROM Receipt_Item ri "
        "JOIN Transaction_Record t ON ri.transaction_id = t.transaction_id "
        "WHERE t.user_id = ?", (user['user_id'],))}
    query = parse(question, category_names, descriptions, today,
                  int(user['salary_day']), context, item_names=item_names)

    if query.intent == 'help':
        return Reply(HELP, SUGGESTIONS[None])
    handler = ANSWERS.get(query.intent)
    if handler is None:
        return Reply(UNKNOWN, SUGGESTIONS[None])
    period = query.period
    return Reply(
        handler(conn, user, query, today),
        SUGGESTIONS.get(query.intent, SUGGESTIONS[None]),
        {'intent': query.intent, 'categories': query.categories,
         'category_label': query.category_label, 'merchant': query.merchant,
         'period': [period.start.isoformat(), period.end.isoformat(),
                    period.label, period.kind]})
