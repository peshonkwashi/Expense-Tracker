"""Read emailed receipts from the user's own mailbox over IMAP.

Most digital receipts (Uber, Bolt, Yango, food delivery, online shops) arrive
as an HTML email with the line items in the body and no attachment, so both
are read: the body text is parsed directly, and a PDF or image attachment is
handed to OCR when the body has no priced lines.

The mailbox is opened read-only (EXAMINE plus BODY.PEEK), so scanning never
marks a message as read, moves it or deletes it.
"""
import email
import imaplib
import os
import tempfile
from datetime import date, datetime, timedelta
from email.header import decode_header
from html.parser import HTMLParser

ATTACHMENT_EXTENSIONS = ('.pdf', '.jpg', '.jpeg', '.png')

# Well-known providers. Anything else falls back to IMAP_SERVER, then to
# imap.<domain>, which is the convention most hosts follow.
IMAP_SERVERS = {
    'gmail.com': 'imap.gmail.com',
    'googlemail.com': 'imap.gmail.com',
    'yahoo.com': 'imap.mail.yahoo.com',
    'ymail.com': 'imap.mail.yahoo.com',
    'icloud.com': 'imap.mail.me.com',
    'me.com': 'imap.mail.me.com',
    'mac.com': 'imap.mail.me.com',
    'outlook.com': 'outlook.office365.com',
    'hotmail.com': 'outlook.office365.com',
    'live.com': 'outlook.office365.com',
}


def imap_server_for(email_address):
    """The IMAP host for an address: IMAP_SERVER if set, else by domain."""
    override = os.environ.get('IMAP_SERVER', '').strip()
    if override:
        return override
    domain = (email_address or '').rsplit('@', 1)[-1].strip().lower()
    return IMAP_SERVERS.get(domain, f'imap.{domain}')


def connect_imap(email_address, app_password, imap_server=None, timeout=15):
    """Connect to the IMAP server over SSL and log in.

    A connect timeout keeps a hung or unreachable mail server from blocking the
    request indefinitely — this feature is optional and must never freeze the app.
    """
    server = imap_server or imap_server_for(email_address)
    if server == 'imap.gmail.com':
        # Google displays app passwords as "abcd efgh ijkl mnop"; the spaces
        # are not part of the password.
        app_password = app_password.replace(' ', '')
    mail = imaplib.IMAP4_SSL(server, timeout=timeout)
    mail.login(email_address, app_password)
    return mail


def _safe_keyword(keyword):
    """Strip the two characters that are special inside an IMAP quoted string,
    so a merchant keyword can never break out of its "..." literal."""
    return (keyword or '').replace('\\', '').replace('"', '').strip()


def _imap_date(value):
    return value.strftime('%d-%b-%Y')  # e.g. 01-Sep-2026


# --- Turning an email into receipt text -------------------------------------

class _HTMLText(HTMLParser):
    """Flatten an HTML email to text, one table row or block per line.

    Receipt emails lay items out as table rows ("Trip fare | K 45.00"); joining
    a row's cells with spaces gives the same "name ... price" line a printed
    receipt has, which the OCR line parser already understands.
    """
    BLOCKS = {'tr', 'br', 'p', 'div', 'li', 'table', 'h1', 'h2', 'h3', 'h4',
              'h5', 'h6', 'section', 'article', 'header', 'footer'}
    SKIP = {'style', 'script', 'head', 'title'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append('\n')
        elif tag in ('td', 'th'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)

    def text(self):
        lines = (' '.join(line.split()) for line in ''.join(self.parts).splitlines())
        return '\n'.join(line for line in lines if line)


def html_to_text(html):
    parser = _HTMLText()
    parser.feed(html or '')
    parser.close()
    return parser.text()


def _decoded_payload(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ''
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except LookupError:  # unknown charset name
        return payload.decode('utf-8', errors='replace')


def body_texts(msg):
    """The message body as candidate receipt texts, plain text first.

    Both versions are returned because some senders put only "view this
    receipt online" in the plain part and the real line items in the HTML.
    """
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get_filename() or (part.get('Content-Disposition') or '').lower().startswith('attachment'):
            continue
        ctype = part.get_content_type()
        if ctype == 'text/plain':
            plain.append(_decoded_payload(part))
        elif ctype == 'text/html':
            html.append(html_to_text(_decoded_payload(part)))
    return [text for text in ('\n'.join(plain), '\n'.join(html)) if text.strip()]


def _attachment_name(part):
    filename = part.get_filename()
    if not filename:
        return None
    value, encoding = decode_header(filename)[0]
    if isinstance(value, bytes):
        value = value.decode(encoding or 'utf-8', errors='replace')
    return value


def save_attachment(msg):
    """Write the first PDF/image attachment to a temp file; return its path."""
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        filename = _attachment_name(part)
        if not filename:
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        fd, temp_path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
        return temp_path
    return None


# --- Searching the mailbox ---------------------------------------------------

def _fetch_message(mail, email_id):
    status, msg_data = mail.fetch(email_id, '(BODY.PEEK[])')
    if status != 'OK':
        return None
    for response_part in msg_data or ():
        if isinstance(response_part, tuple):
            return email.message_from_bytes(response_part[1])
    return None


def find_receipt_messages(mail, merchant_keyword, around, days_either_side=3,
                          limit=5):
    """Emails from or mentioning a merchant sent near a transaction date.

    `around` is the transaction date; the window allows for the bank posting a
    card payment a day or two after the receipt was emailed. Returns up to
    `limit` messages, newest first.
    """
    keyword = _safe_keyword(merchant_keyword)
    if not keyword:
        return []
    since = around - timedelta(days=days_either_side)
    before = around + timedelta(days=days_either_side + 1)  # BEFORE is exclusive

    mail.select('INBOX', readonly=True)
    status, found = mail.search(
        None, f'(SINCE "{_imap_date(since)}" BEFORE "{_imap_date(before)}" '
              f'OR FROM "{keyword}" BODY "{keyword}")')
    if status != 'OK' or not found or not found[0]:
        return []

    messages = []
    for email_id in reversed(found[0].split()[-limit:]):
        msg = _fetch_message(mail, email_id)
        if msg is not None:
            messages.append(msg)
    return messages


def fetch_receipt_attachment(mail, merchant_keyword, days_back=7):
    """Download the newest matching email's PDF/image attachment, or None.

    Kept for callers that only want an attachment; the receipt scanner uses
    find_receipt_messages, which also reads receipts in the email body.
    """
    messages = find_receipt_messages(mail, merchant_keyword, date.today(),
                                     days_either_side=days_back, limit=1)
    return save_attachment(messages[0]) if messages else None


# Subject words that mark an email as a receipt rather than marketing.
# IMAP SUBJECT matches substrings, so 'purchase' also finds 'purchased'.
RECEIPT_SUBJECT_WORDS = ('receipt', 'order', 'trip', 'invoice', 'payment',
                         'purchase', 'billing', 'subscription', 'trial')


def is_gmail(mail):
    """True for a Gmail mailbox, which supports Gmail's own search syntax."""
    return 'X-GM-EXT-1' in (getattr(mail, 'capabilities', None) or ())


def _search_ids(mail, *criteria):
    status, found = mail.search(None, *criteria)
    if status != 'OK' or not found or not found[0]:
        return set()
    return set(found[0].split())


def find_recent_receipt_messages(mail, days_back=30, limit=40):
    """Receipt-like emails from the last `days_back` days, newest first.

    Every provider is searched by subject. On Gmail, the Purchases tab
    (category:purchases) is searched too: Gmail files receipts, trials and
    order confirmations there even when the subject says none of our words,
    e.g. "Welcome to Canva Pro".
    """
    since = date.today() - timedelta(days=days_back)
    words = RECEIPT_SUBJECT_WORDS
    # IMAP's OR takes exactly two keys, so n alternatives need n-1 ORs in front.
    subject_any = 'OR ' * (len(words) - 1) + ' '.join(f'SUBJECT "{w}"' for w in words)
    mail.select('INBOX', readonly=True)
    ids = _search_ids(mail, f'(SINCE "{_imap_date(since)}" {subject_any})')
    if is_gmail(mail):
        ids |= _search_ids(mail, 'X-GM-RAW',
                           f'"category:purchases newer_than:{int(days_back)}d"')
    messages = []
    # Message sequence numbers rise with arrival, so the largest are newest.
    for email_id in sorted(ids, key=int, reverse=True)[:limit]:
        msg = _fetch_message(mail, email_id)
        if msg is not None:
            messages.append(msg)
    return messages


def _header_text(msg, name):
    from email.header import make_header
    raw = msg.get(name)
    if not raw:
        return ''
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(raw)


def message_key(msg):
    """A stable identity for an email: its Message-ID, else date+sender+subject."""
    return (msg.get('Message-ID') or '').strip() or '|'.join(
        _header_text(msg, h) for h in ('Date', 'From', 'Subject'))


def message_date(msg):
    """The date an email was sent (local date), or today if unreadable."""
    from email.utils import parsedate_to_datetime
    try:
        sent = parsedate_to_datetime(msg.get('Date'))
        return sent.astimezone().date() if sent.tzinfo else sent.date()
    except (TypeError, ValueError, IndexError):
        return date.today()


def sender_and_subject(msg):
    """(display name, address, subject) of an email, decoded."""
    from email.utils import parseaddr
    name, address = parseaddr(_header_text(msg, 'From'))
    return name.strip(), address.strip().lower(), _header_text(msg, 'Subject')


def parse_transaction_date(value):
    """A transaction_date column value ('2026-11-03' or an ISO datetime) as a date."""
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()
