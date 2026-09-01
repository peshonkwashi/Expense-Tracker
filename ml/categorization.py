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
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import f1_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline

import config
import database

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


def normalise(description):
    """Lowercase, strip punctuation, collapse whitespace (section 3.7.1)."""
    text = str(description or '').lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


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


def classify(description):
    """Assign a category to one description.

    Returns (category_name, confidence, source) where source is 'model', 'rule'
    or 'default'. The source is shown in the UI so the user can see why a
    transaction was labelled the way it was (section 5.9, explainability).
    """
    text = normalise(description)
    pipeline = _load_model()

    if pipeline is not None and text:
        try:
            probabilities = pipeline.predict_proba([text])[0]
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
    "SELECT t.description, c.category_name, t.category_source "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.transaction_type = 'DEBIT' AND c.category_name != 'Uncategorised'"
)


def training_frame(conn, user_id=None):
    """Labelled examples: user corrections plus confidently labelled history."""
    query = TRAINING_QUERY
    params = []
    if user_id is not None:
        query += ' AND t.user_id = ?'
        params.append(user_id)

    frame = pd.read_sql_query(query, conn, params=params)
    if frame.empty:
        return frame

    frame['text'] = frame['description'].map(normalise)
    frame = frame[frame['text'].str.len() > 0]
    if frame.empty:
        return frame

    # A user correction is ground truth, so repeat it to weight it. This is the
    # cheapest way to let corrections outvote the rule seed for the same
    # merchant without threading sample_weight through the pipeline.
    corrections = frame[frame['category_source'] == 'user']
    if not corrections.empty:
        frame = pd.concat([frame] + [corrections] * 2, ignore_index=True)
    return frame


def _candidate_pipelines():
    return {
        'MultinomialNB': Pipeline([
            ('tfidf', TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
            ('clf', MultinomialNB(alpha=0.1)),
        ]),
        'RandomForest': Pipeline([
            ('tfidf', TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
            ('clf', RandomForestClassifier(n_estimators=200, random_state=42,
                                           class_weight='balanced_subsample')),
        ]),
    }


def train_categorization_model(conn, user_id=None):
    """Evaluate both classifiers, persist the better one, record NFR-02 metrics.

    Returns a result dict. 'trained' is False when there is not yet enough
    labelled data, in which case the rule seed keeps carrying the system.
    """
    frame = training_frame(conn, user_id)
    result = {'trained': False, 'reason': None, 'algorithm': None,
              'f1': None, 'samples': 0, 'classes': 0, 'scores': {}}

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

    features, labels = frame['text'], frame['category_name']

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
    "SELECT t.transaction_id, t.description "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.user_id = ? AND t.category_source != 'user' "
    "AND c.category_name = 'Uncategorised'"
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
        name, confidence, source = classify(row['description'])
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
