"""Receipt OCR for the scan-receipt feature.

Reads an uploaded receipt image or PDF with Tesseract (via pytesseract) and
extracts itemised lines. Only real OCR is used: no items are made up. If the
OCR stack is missing or the image cannot be read, scan_receipt raises
ReceiptScanError with a message the user can act on. If OCR runs but finds no
priced lines, it returns no items and the user can enter them manually.

Install the Python packages with `pip install -r requirements-ocr.txt`, plus the
Tesseract binary (and poppler, for PDFs). On Windows both install with winget:

    winget install UB-Mannheim.TesseractOCR
    winget install oschwartz10612.Poppler

The binaries are found automatically on PATH or in their standard Windows
install locations. TESSERACT_CMD and POPPLER_PATH (env var or config) override
the search.
"""
import glob
import os
import re
import shutil
from dataclasses import dataclass, field

import config
from ml.categorization import normalise


class ReceiptScanError(Exception):
    """A receipt could not be scanned. The message is safe to show the user."""


@dataclass
class ReceiptScan:
    """Result of a scan: the itemised lines and the printed total, if found."""
    items: list = field(default_factory=list)
    total: float = None

    @property
    def items_total(self):
        return round(sum(i['price'] for i in self.items), 2)


# --- Email-receipt merchants (used by the auto-fetcher) ----------------------

# Merchants that deliver a digital / email receipt, so the email auto-fetcher
# can plausibly pull one from an inbox. Physical, cash- or
# transfer-only merchants — a fuel pump, a prepaid utility meter, a barber, rent,
# a loan repayment — are deliberately absent: they do not email an itemised
# receipt, so the fetcher skips them the way a real inbox scan would find nothing
# from them. Keywords are matched as normalised substrings of the description, so
# 'shoprite' matches 'Shoprite Manda Hill'. Short or ambiguous fragments are
# avoided to prevent false matches (e.g. no bare 'total', which is a fuel brand
# here).
EMAIL_RECEIPT_MERCHANTS = (
    # Ride-hailing & taxi apps
    'uber', 'bolt', 'lyft', 'yango', 'indrive', 'ubuntu cab', 'little cab',
    'careem', 'didi',
    # Food delivery & fast-food apps
    'uber eats', 'glovo', 'jumia food', 'mr d', 'deliveroo', 'doordash',
    'grubhub', 'kfc', 'hungry lion', 'debonairs', 'steers', 'nandos',
    'pizza hut', 'dominos', 'mcdonald', 'chicken inn', 'subway', 'galitos',
    # Online marketplaces & e-commerce
    'amazon', 'takealot', 'jumia', 'aliexpress', 'alibaba', 'shein', 'temu',
    'ebay', 'etsy', 'kilimall', 'konga', 'zando', 'superbalist',
    # Big retailers with e-receipts / loyalty email
    'shoprite', 'pick n pay', 'picknpay', 'spar', 'checkers', 'woolworths',
    'game store', 'game stores', 'mr price', 'edgars', 'builders',
    'cash crusaders',
    # Streaming, app stores & digital subscriptions
    'netflix', 'spotify', 'showmax', 'dstv', 'gotv', 'multichoice', 'youtube',
    'apple', 'itunes', 'app store', 'google', 'play store', 'microsoft', 'xbox',
    'playstation', 'steam', 'canva', 'adobe', 'prime video', 'amazon prime',
    # Telecom airtime / data bought online
    'airtel', 'mtn', 'zamtel', 'vodacom', 'safaricom',
    # Airlines, travel & hospitality
    'airbnb', 'booking', 'expedia', 'emirates', 'kenya airways', 'ethiopian',
    'proflight', 'airlink', 'fastjet', 'mahogany air', 'qatar airways',
    'turkish airlines',
    # Online betting / gaming (email confirmations)
    'betway', 'gal sport', 'premierbet', 'betpawa',
    # Payments / fintech that email confirmations
    'paypal', 'flutterwave', 'payfast', 'stripe',
    # Pharmacies & health retailers with e-receipts
    'link pharmacy', 'clicks', 'dischem', 'pharmacy',
)


def emails_receipt(description):
    """True when a transaction's merchant is one that sends a digital / email
    receipt the auto-fetcher could realistically retrieve.

    Used to filter which transactions the email fetcher acts on, so it only
    looks for receipts from recognised online/app merchants (Uber, Amazon, food
    delivery, streaming, big retailers) and skips cash- or transfer-only ones
    (fuel, utilities, rent, a barber) — the way scanning a real inbox would.
    """
    text = normalise(description)
    return bool(text) and any(keyword in text
                              for keyword in EMAIL_RECEIPT_MERCHANTS)


# --- OCR text parsing --------------------------------------------------------

# A line that is a total, tax, payment or receipt chrome — never a purchased
# item. Counting these as items would double the receipt and pollute the list.
NON_ITEM_KEYWORDS = re.compile(
    r'\b(total|subtotal|sub-total|balance|change|cash|tender|card|visa|master'
    r'|mastercard|vat|tax|gst|amount|due|paid|payment|receipt|invoice|tel|phone'
    r'|thank|welcome|cashier|till|date|time|ref|no|qty|discount)\b',
    re.IGNORECASE)

# Lines that state what the receipt as a whole cost.
TOTAL_LINE = re.compile(
    r'\b(grand\s+total|total|amount\s+due|balance\s+due|total\s+due)\b',
    re.IGNORECASE)
NOT_TOTAL_LINE = re.compile(
    r'\b(sub-?\s?total|vat|tax|gst|items?|qty|savings?|discount)\b',
    re.IGNORECASE)

# A monetary amount: digits with optional thousands commas, then a two-decimal
# part. Comma is a thousands separator here (Zambian receipts use a '.' decimal).
PRICE_PATTERN = re.compile(r'\d[\d,]*\.\d{2}')

# A quantity just before the unit price: "2", "2 x", "2x", "2 @", "2 *".
TRAILING_QUANTITY = re.compile(r'(?:^|\s)(\d{1,2})\s*[xX@*]?\s*$')

# Currency markers left at the end of a name, e.g. "Bread K" or "Milk ZMW".
TRAILING_CURRENCY = re.compile(r'\s+(?:K|ZMW|ZK)$', re.IGNORECASE)

MAX_PLAUSIBLE_PRICE = 100000.0
MAX_QUANTITY = 99


def _to_amount(text):
    try:
        return float(text.replace(',', ''))
    except ValueError:
        return None


def _clean_name(text):
    name = re.sub(r'[^A-Za-z0-9 &/-]+', ' ', text)
    name = re.sub(r'\s+', ' ', name).strip(' -')
    return TRAILING_CURRENCY.sub('', name).strip(' -')


def _parse_item_line(line):
    """Return {name, price, quantity} for one receipt line, or None."""
    matches = list(PRICE_PATTERN.finditer(line))
    if not matches:
        return None
    price = _to_amount(matches[-1].group())
    if price is None or not (0.01 <= price <= MAX_PLAUSIBLE_PRICE):
        return None

    # "Eggs 2 x 12.50 25.00": a unit price that divides the line total evenly
    # tells us the quantity, and the name ends before the quantity.
    quantity = 1
    name_end = matches[-1].start()
    if len(matches) >= 2:
        unit = _to_amount(matches[-2].group())
        if unit and unit > 0:
            ratio = price / unit
            whole = round(ratio)
            if 1 <= whole <= MAX_QUANTITY and abs(ratio - whole) < 0.01:
                quantity = whole
                name_end = matches[-2].start()

    name = line[:name_end]
    if quantity > 1:
        name = TRAILING_QUANTITY.sub('', name)
    name = _clean_name(name)
    if len(name) < 2 or not re.search(r'[A-Za-z]', name):
        return None  # a bare number or price line, not a named item
    return {'name': name[:60], 'price': round(price, 2), 'quantity': quantity}


def parse_receipt_text(text):
    """Extract [{name, price, quantity}] from raw OCR text.

    Heuristics tuned for supermarket and retail receipts:
      * a line counts only if it contains a price,
      * the LAST price on the line is taken as the line total; when the price
        before it divides the total evenly, it is the unit price and gives the
        quantity,
      * total/tax/payment and header/footer lines are skipped, so the items do
        not double-count the receipt total,
      * the item name is the text before the price with stray OCR glyphs and
        currency markers removed.
    """
    items = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or NON_ITEM_KEYWORDS.search(line):
            continue
        item = _parse_item_line(line)
        if item:
            items.append(item)
    return items


def parse_receipt_total(text):
    """Return the receipt's printed grand total, or None if none was read.

    Looks at lines that name a total (but not a subtotal, VAT or item count)
    and takes the largest price among them; a "TOTAL VAT" figure is always
    smaller than the grand total.
    """
    candidates = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not TOTAL_LINE.search(line) or NOT_TOTAL_LINE.search(line):
            continue
        for match in PRICE_PATTERN.findall(line):
            amount = _to_amount(match)
            if amount and 0.01 <= amount <= MAX_PLAUSIBLE_PRICE:
                candidates.append(amount)
    return round(max(candidates), 2) if candidates else None


# --- Locating the OCR binaries -----------------------------------------------

_WINDOWS_TESSERACT_PATHS = (
    r'%ProgramFiles%\Tesseract-OCR\tesseract.exe',
    r'%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe',
    r'%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe',
)

_WINDOWS_POPPLER_GLOBS = (
    r'%LOCALAPPDATA%\Microsoft\WinGet\Packages\oschwartz10612.Poppler_*'
    r'\poppler-*\Library\bin',
    r'%ProgramFiles%\poppler*\Library\bin',
    r'%ProgramFiles%\poppler*\bin',
)


def find_tesseract():
    """Path to the Tesseract binary, or None if it cannot be found."""
    configured = (getattr(config, 'TESSERACT_CMD', None)
                  or os.environ.get('TESSERACT_CMD'))
    if configured:
        return configured
    on_path = shutil.which('tesseract')
    if on_path:
        return on_path
    for candidate in _WINDOWS_TESSERACT_PATHS:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    return None


def find_poppler():
    """Folder holding poppler's pdftoppm, '' if it is on PATH, None if absent."""
    configured = (getattr(config, 'POPPLER_PATH', None)
                  or os.environ.get('POPPLER_PATH'))
    if configured:
        return configured
    if shutil.which('pdftoppm'):
        return ''
    for pattern in _WINDOWS_POPPLER_GLOBS:
        for folder in sorted(glob.glob(os.path.expandvars(pattern)), reverse=True):
            if os.path.isfile(os.path.join(folder, 'pdftoppm.exe')):
                return folder
    return None


def ocr_status():
    """What the OCR stack can do on this machine (for diagnostics)."""
    return {'tesseract': find_tesseract(), 'pdf': find_poppler() is not None}


# --- Scanning ----------------------------------------------------------------

# Tesseract reads best when text is roughly 30px high; phone photos of small
# receipts are often below that, so narrow images are upscaled first.
MIN_OCR_WIDTH = 1600
MAX_UPSCALE = 3.0


def _load_image(image_path):
    from PIL import Image, UnidentifiedImageError

    if os.path.splitext(image_path)[1].lower() == '.pdf':
        from pdf2image import convert_from_path
        from pdf2image.exceptions import (PDFInfoNotInstalledError,
                                          PDFPageCountError, PDFSyntaxError)
        poppler = find_poppler()
        if poppler is None:
            raise ReceiptScanError(
                'PDF receipts need poppler, which is not installed. Install it '
                '(winget install oschwartz10612.Poppler) or upload a photo of '
                'the receipt instead.')
        try:
            # Only the first page: a receipt is one page, and converting the
            # whole document at 300 DPI would load every page into memory.
            pages = convert_from_path(image_path, dpi=300, first_page=1,
                                      last_page=1, poppler_path=poppler or None)
        except PDFInfoNotInstalledError as exc:
            raise ReceiptScanError(
                'PDF receipts need poppler, which could not be started. Set '
                'POPPLER_PATH to its bin folder.') from exc
        except (PDFPageCountError, PDFSyntaxError) as exc:
            raise ReceiptScanError('That PDF could not be read.') from exc
        if not pages:
            raise ReceiptScanError('That PDF has no pages.')
        return pages[0]

    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()  # lets Pillow open iPhone .heic photos
    except ImportError:
        pass
    try:
        img = Image.open(image_path)
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ReceiptScanError(
            'That file could not be opened as an image.') from exc
    return img


def _prepare_for_ocr(img):
    """Upright, grayscale, upscaled and contrast-stretched copy of a photo."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)  # phone photos are often stored rotated
    img = ImageOps.grayscale(img)
    if img.width < MIN_OCR_WIDTH:
        scale = min(MAX_UPSCALE, MIN_OCR_WIDTH / img.width)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(img)


def read_receipt_text(image_path):
    """Run OCR on a receipt image or PDF and return the raw text."""
    if not image_path or not os.path.isfile(image_path):
        raise ReceiptScanError('The receipt file could not be found.')
    try:
        import pytesseract
    except ImportError as exc:
        raise ReceiptScanError(
            'Receipt scanning is not installed. Run '
            '`pip install -r requirements-ocr.txt`.') from exc

    tesseract = find_tesseract()
    if not tesseract:
        raise ReceiptScanError(
            'Tesseract OCR is not installed. Install it (winget install '
            'UB-Mannheim.TesseractOCR) or set TESSERACT_CMD to tesseract.exe.')
    pytesseract.pytesseract.tesseract_cmd = tesseract

    img = _prepare_for_ocr(_load_image(image_path))
    try:
        # --psm 6 treats the image as one uniform block of text, which suits a
        # receipt's single column better than the default page segmentation.
        return pytesseract.image_to_string(img, config='--psm 6')
    except pytesseract.TesseractNotFoundError as exc:
        raise ReceiptScanError(
            f'Tesseract could not be started from {tesseract}. Check '
            'TESSERACT_CMD.') from exc
    except pytesseract.TesseractError as exc:
        raise ReceiptScanError(f'Tesseract failed to read the receipt: '
                               f'{exc.message or exc}') from exc


def scan_receipt(image_path):
    """OCR a receipt into a ReceiptScan (items plus printed total).

    Raises ReceiptScanError when the receipt cannot be scanned at all. A scan
    that runs but finds no priced lines returns a ReceiptScan with no items.
    """
    text = read_receipt_text(image_path)
    return ReceiptScan(items=parse_receipt_text(text),
                       total=parse_receipt_total(text))
