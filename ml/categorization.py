"""Expense Categorisation Module (section 3.6.3, FR-02).

Two classifiers are evaluated as the report specifies (section 3.7.3): a
Multinomial Naive Bayes baseline and a Random Forest alternative. The better
macro F1 under 5-fold cross-validation wins and is persisted with joblib.

A keyword rule seed solves the cold-start problem. A supervised model cannot be
trained before labelled data exists, and labelled data cannot exist before
something assigns labels. The rule seed produces the first labels; from then on
the model trains on those labels plus every user correction (FR-03), which is
the incremental retraining described in section 3.6.3.
"""
import os
import re
import threading

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import f1_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder

import config
import database

# Feature columns the models train and predict on. Naive Bayes (the text-only
# baseline) uses only 'text'; Random Forest additionally uses the numeric and
# temporal features, which is the split the report describes in section 3.7.2
# and 3.7.3 — RF as the more powerful alternative that can weigh non-text
# signals (amount, and the day-of-week/day-of-month timing of paydays and bill
# cycles) alongside the merchant text.
FEATURE_COLUMNS = ['text', 'amount', 'day_of_week', 'day_of_month']

# --- Rule seed -------------------------------------------------------------
# Keyword -> category. Ordered: the first match wins, so specific merchants
# come before generic words. Tuned for common Zambian merchants and billers.
RULE_SEED = [
    # Mobile-money wallets first: "airtel money" is a transfer, not airtime.
    (r'\b((airtel|mtn|zamtel)\s*money|mobile\s*money|momo\s*transfer)\b', 'Transfers'),
    (r'\b(shoprite|pick\s*n\s*pay|picknpay|spar|game\s*store|choppies|melissa|'
     r'food\s*lover|supermarket|grocer|butchery|market)\b', 'Groceries'),
    (r'\b(puma|total|totalenergies|engen|mount\s*meru|petroda|fuel|petrol|diesel|'
     r'filling\s*station|yango|ulendo|taxi|bus\s*fare|intercity|parking|toll)\b',
     'Transport'),
    (r'\b(zesco|prepaid\s*units|lwsc|water\s*bill|nwasco|electricity|utility|'
     r'garbage|refuse)\b', 'Utilities'),
    (r'\b(rent|rental|landlord|mortgage|body\s*corporate|service\s*charge)\b',
     'Housing'),
    (r'\b(pharmacy|chemist|clinic|hospital|medical|dental|dentist|health|'
     r'laborator)\b', 'Healthcare'),
    (r'\b(school\s*fees|tuition|unza|cbu|university|college|exam\s*fee|'
     r'stationery|textbook)\b', 'Education'),
    (r'\b(airtel|mtn|zamtel|airtime|data\s*bundle|talktime|recharge)\b',
     'Airtime & Data'),
    (r'\b(loan|repayment|instalment|installment|credit\s*card|overdraft|bayport|'
     r'izwe|microfinance|arrears)\b', 'Loan & Debt'),
    (r'\b(netflix|showmax|spotify|dstv|gotv|multichoice|apple\s*com|google\s*play|'
     r'youtube\s*premium|microsoft|adobe|subscription|amazon\s*prime)\b',
     'Subscriptions'),
    (r'\b(kfc|hungry\s*lion|debonairs|steers|pizza|mcdonald|cafe|coffee|'
     r'restaurant|takeaway|take\s*away|chicken\s*inn|grill|lounge)\b', 'Dining Out'),
    (r'\b(cinema|movie|ster\s*kinekor|betting|betway|premierbet|gaming|concert|'
     r'ticket|lodge|resort|holiday)\b', 'Entertainment'),
    (r'\b(mr\s*price|jet\b|truworths|edgars|pep\b|woolworths|clothing|boutique|'
     r'electronics|hardware|furniture|jumia|shopping)\b', 'Shopping'),
    (r'\b(salon|barber|spa\b|cosmetic|beauty|gym|fitness)\b', 'Personal Care'),
    (r'\b(transfer|sent\s*to|mobile\s*money|momo|zoona|western\s*union|'
     r'atm\s*withdrawal|cash\s*withdrawal)\b', 'Transfers'),
]

_COMPILED_RULES = [(re.compile(p, re.IGNORECASE), name) for p, name in RULE_SEED]

_model_lock = threading.Lock()
_model_cache = {'pipeline': None, 'mtime': None}
_seed_cache = {'frame': None, 'mtime': None}


def normalise(description):
    """Lowercase, strip punctuation, collapse whitespace (section 3.7.1)."""
    text = str(description or '').lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def build_features(descriptions, amounts=None, dates=None):
    """Build the model feature frame from raw transaction fields (section 3.7.2).

    Columns: normalised merchant `text`, `amount`, and the `day_of_week` (0-6)
    and `day_of_month` (1-31) the transaction fell on. Amount and dates are
    optional so a bare description can still be classified (with neutral
    defaults), which the fallback callers rely on.
    """
    descriptions = list(descriptions)
    count = len(descriptions)
    amounts = list(amounts) if amounts is not None else [0.0] * count
    dates = list(dates) if dates is not None else [None] * count

    parsed = pd.to_datetime(pd.Series(dates), errors='coerce')
    day_of_week = parsed.dt.dayofweek.fillna(0).astype(int)
    day_of_month = parsed.dt.day.fillna(1).astype(int)
    amount = pd.to_numeric(pd.Series(amounts), errors='coerce').fillna(0.0)

    return pd.DataFrame({
        'text': [normalise(d) for d in descriptions],
        'amount': amount.to_numpy(dtype=float),
        'day_of_week': day_of_week.to_numpy(dtype=int),
        'day_of_month': day_of_month.to_numpy(dtype=int),
    }, columns=FEATURE_COLUMNS)


def rule_category(description):
    """Return the seed category for a description, or None when unmatched."""
    text = normalise(description)
    if not text:
        return None
    for pattern, name in _COMPILED_RULES:
        if pattern.search(text):
            return name
    return None


def _load_model():
    """Load the persisted pipeline, re-reading only when the file changes."""
    path = config.CATEGORISER_PATH
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    with _model_lock:
        if _model_cache['pipeline'] is None or _model_cache['mtime'] != mtime:
            try:
                _model_cache['pipeline'] = joblib.load(path)
                _model_cache['mtime'] = mtime
            except Exception as exc:  # corrupt or version-mismatched artefact
                print(f'[categorisation] could not load model: {exc}')
                return None
        return _model_cache['pipeline']


def reset_model_cache():
    with _model_lock:
        _model_cache['pipeline'] = None
        _model_cache['mtime'] = None


def classify(description, amount=None, transaction_date=None):
    """Assign a category to one transaction.

    Amount and date feed the Random Forest's non-text features; they are
    optional so a description alone still classifies (Naive Bayes ignores them,
    and the Random Forest falls back to neutral defaults). Returns
    (category_name, confidence, source) where source is 'model', 'rule' or
    'default' — surfaced in the UI so the user can see why a transaction was
    labelled the way it was (section 5.9, explainability).
    """
    text = normalise(description)
    pipeline = _load_model()

    if pipeline is not None and text:
        try:
            features = build_features([description], [amount], [transaction_date])
            probabilities = pipeline.predict_proba(features)[0]
            best = probabilities.argmax()
            confidence = float(probabilities[best])
            if confidence >= config.MODEL_CONFIDENCE_FLOOR:
                return str(pipeline.classes_[best]), confidence, 'model'
        except Exception as exc:
            print(f'[categorisation] prediction failed: {exc}')

    seeded = rule_category(description)
    if seeded:
        return seeded, 1.0, 'rule'
    return 'Uncategorised', 0.0, 'default'


TRAINING_QUERY = (
    "SELECT t.description, t.amount, t.transaction_date, "
    "       c.category_name, t.category_source "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.transaction_type = 'DEBIT' AND c.category_name != 'Uncategorised'"
)


def reset_seed_cache():
    _seed_cache['frame'] = None
    _seed_cache['mtime'] = None


def load_seed_frame():
    """Load and preprocess the curated seed dataset (section 3.7.1).

    Returns a frame with `category_name`, the FEATURE_COLUMNS and a
    `category_source` of 'seed', or an empty frame when the seed is disabled or
    absent. The seed carries hand-labelled merchant text with neutral non-text
    features (no real amount or date), so it is text-labelled examples rather
    than full transactions — its value is giving the classifier diverse
    merchant variants per category from the first upload. Cached and reloaded
    when the file changes.
    """
    path = config.SEED_DATASET_PATH
    if not config.USE_SEED_DATASET or not os.path.exists(path):
        return pd.DataFrame()

    mtime = os.path.getmtime(path)
    if _seed_cache['frame'] is None or _seed_cache['mtime'] != mtime:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
        raw = raw[(raw['description'].str.strip() != '')
                  & (raw['category'].str.strip() != '')]
        frame = build_features(raw['description'].tolist())  # neutral amount/date
        frame['category_name'] = raw['category'].str.strip().to_numpy()
        frame['category_source'] = 'seed'
        frame = frame[frame['text'].str.len() > 0].reset_index(drop=True)
        _seed_cache['frame'] = frame
        _seed_cache['mtime'] = mtime
    return _seed_cache['frame'].copy()


def training_frame(conn, user_id=None, include_seed=False):
    """The user's own labelled transactions as a feature frame.

    Returns a frame carrying the label plus the FEATURE_COLUMNS, so the same
    frame trains both the text-only baseline and the feature-rich Random Forest.
    By default this is the user's data alone — the measure NFR-02 is about.
    Pass include_seed=True to append the curated seed (the trainer does this
    only to bootstrap a cold start; see train_categorization_model).
    """
    query = TRAINING_QUERY
    params = []
    if user_id is not None:
        query += ' AND t.user_id = ?'
        params.append(user_id)

    frame = pd.read_sql_query(query, conn, params=params)
    parts = []

    if not frame.empty:
        frame['text'] = frame['description'].map(normalise)
        frame = frame[frame['text'].str.len() > 0].copy()
    if not frame.empty:
        parsed = pd.to_datetime(frame['transaction_date'], errors='coerce')
        frame['day_of_week'] = parsed.dt.dayofweek.fillna(0).astype(int)
        frame['day_of_month'] = parsed.dt.day.fillna(1).astype(int)
        frame['amount'] = pd.to_numeric(frame['amount'], errors='coerce').fillna(0.0)

        # A user correction is ground truth, so repeat it to weight it. This is
        # the cheapest way to let corrections outvote the seed for the same
        # merchant without threading sample_weight through the pipeline.
        corrections = frame[frame['category_source'] == 'user']
        if not corrections.empty:
            frame = pd.concat([frame] + [corrections] * 2, ignore_index=True)
        parts.append(frame)

    if include_seed:
        seed = load_seed_frame()
        if not seed.empty:
            parts.append(seed)

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _tfidf():
    return TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)


def _candidate_pipelines():
    """The two classifiers compared in section 3.7.3.

    Naive Bayes is the text-only baseline: it takes just the TF-IDF of the
    merchant description. Random Forest is the more powerful alternative that
    also weighs the non-text features (section 3.7.2) — the normalised amount
    and the day-of-week / day-of-month, which carry the payday and bill-cycle
    timing that helps separate fixed recurring charges from ad-hoc spending.
    Both consume the same FEATURE_COLUMNS frame; each pipeline selects what it
    uses, so model selection and persistence stay uniform.
    """
    nb = Pipeline([
        ('features', ColumnTransformer(
            [('tfidf', _tfidf(), 'text')], remainder='drop')),
        ('clf', MultinomialNB(alpha=0.1)),
    ])
    rf = Pipeline([
        ('features', ColumnTransformer([
            ('tfidf', _tfidf(), 'text'),
            ('amount', MinMaxScaler(), ['amount']),
            ('day_of_week', OneHotEncoder(categories=[list(range(7))],
                                          handle_unknown='ignore'), ['day_of_week']),
            ('day_of_month', 'passthrough', ['day_of_month']),
        ], remainder='drop')),
        ('clf', RandomForestClassifier(n_estimators=200, random_state=42,
                                       class_weight='balanced_subsample')),
    ])
    return {'MultinomialNB': nb, 'RandomForest': rf}


def train_categorization_model(conn, user_id=None, include_seed=None):
    """Evaluate both classifiers, persist the better one, record NFR-02 metrics.

    Returns a result dict. 'trained' is False when there is not yet enough
    labelled data, in which case the rule seed keeps carrying the system. The
    curated seed dataset is included in training by default (section 3.7.1), so
    a usable model exists from the first upload; pass include_seed=False to
    train on the user's own data alone.
    """
    frame = training_frame(conn, user_id, include_seed=False)
    result = {'trained': False, 'reason': None, 'algorithm': None,
              'f1': None, 'samples': 0, 'classes': 0, 'scores': {},
              'bootstrapped': False}

    # Bootstrap with the curated seed only while the user's own data is too thin
    # to train on alone (section 3.7.1: the seed is for *initial* seeding). Once
    # they have enough of their own, their patterns take over and the reported
    # F1 reflects their real transactions — the measure NFR-02 is about.
    if include_seed is None:
        insufficient = (frame.empty
                        or len(frame) < config.MIN_TRAINING_SAMPLES
                        or frame['category_name'].nunique() < 2)
        include_seed = insufficient
    if include_seed:
        seed = load_seed_frame()
        if not seed.empty:
            frame = (pd.concat([frame, seed], ignore_index=True)
                     if not frame.empty else seed)
            result['bootstrapped'] = True

    if frame.empty:
        result['reason'] = 'No labelled transactions yet.'
        return result

    result['samples'] = len(frame)
    counts = frame['category_name'].value_counts()
    result['classes'] = len(counts)

    if len(frame) < config.MIN_TRAINING_SAMPLES:
        result['reason'] = (f'Only {len(frame)} labelled transactions; '
                            f'{config.MIN_TRAINING_SAMPLES} needed to train.')
        return result
    if len(counts) < 2:
        result['reason'] = 'At least two spending categories are needed to train.'
        return result

    features, labels = frame[FEATURE_COLUMNS], frame['category_name']

    # Cross-validation needs every class present in every fold, so the fold
    # count is capped by the rarest class (section 3.7.4).
    n_splits = int(min(5, counts.min()))
    scores = {}
    for name, pipeline in _candidate_pipelines().items():
        if n_splits >= 2:
            splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            predicted = cross_val_predict(pipeline, features, labels, cv=splitter)
            scores[name] = float(f1_score(labels, predicted, average='macro',
                                          zero_division=0))
        else:
            # Too few examples of some class to cross-validate honestly. Report
            # nothing rather than a resubstitution score that would flatter.
            scores[name] = None

    ranked = {k: v for k, v in scores.items() if v is not None}
    if ranked:
        best_name = max(ranked, key=ranked.get)
        best_f1 = ranked[best_name]
        note = f'{n_splits}-fold CV; ' + ', '.join(
            f'{k}={v:.3f}' for k, v in ranked.items())
    else:
        best_name, best_f1 = 'MultinomialNB', None
        note = 'Insufficient per-class samples for cross-validation.'

    best_pipeline = _candidate_pipelines()[best_name]
    best_pipeline.fit(features, labels)

    os.makedirs(config.MODEL_DIR, exist_ok=True)
    joblib.dump(best_pipeline, config.CATEGORISER_PATH)
    reset_model_cache()

    database.record_metric(conn, 'CATEGORISATION', algorithm=best_name,
                           f1_score=best_f1, sample_count=len(frame),
                           notes=note, user_id=user_id)
    conn.commit()

    result.update({'trained': True, 'algorithm': best_name, 'f1': best_f1,
                   'scores': scores, 'reason': note})
    return result


UNCATEGORISED_QUERY = (
    "SELECT t.transaction_id, t.description, t.amount, t.transaction_date "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.user_id = ? AND t.category_source != 'user' "
    "AND c.category_name = 'Uncategorised' AND t.transaction_type = 'DEBIT'"
)


def recategorise_uncategorised(conn, user_id):
    """Re-run classification over rows still sitting in Uncategorised.

    Called after each retrain so a newly learned merchant is applied to history
    the system could not label at import time. User-set categories are never
    overwritten.
    """
    rows = conn.execute(UNCATEGORISED_QUERY, (user_id,)).fetchall()

    updated = 0
    for row in rows:
        name, confidence, source = classify(
            row['description'], row['amount'], row['transaction_date'])
        if name == 'Uncategorised':
            continue
        conn.execute(
            'UPDATE Transaction_Record SET category_id = ?, category_source = ?, '
            'category_confidence = ? WHERE transaction_id = ?',
            (database.category_id_for(conn, name), source, confidence,
             row['transaction_id']),
        )
        updated += 1
    conn.commit()
    return updated
