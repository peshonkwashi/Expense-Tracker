"""Categorisation model evaluation (section 3.7.4, NFR-02).

Cross-validated comparison of the two candidate classifiers, returning the raw
predictions and metrics so a notebook or script can tabulate and plot them.

This is kept separate from ml/categorization.py on purpose: that module trains
and persists the winning model for production use, whereas this one only
measures, and is the code the evaluation notebook and the report draw on. It
holds no matplotlib dependency so it stays importable and unit-testable.
"""
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

import config
from ml.categorization import FEATURE_COLUMNS, _candidate_pipelines


def evaluate_candidates(frame, max_splits=5, random_state=42):
    """Cross-validate each candidate classifier on a labelled feature frame.

    Uses the same stratified k-fold scheme as training (section 3.7.4): the
    fold count is capped by the rarest class so every fold contains every
    category. Returns (results, n_splits) where results maps each model name to
    a dict of y_true, y_pred, the ordered labels, macro F1, accuracy, the
    per-category classification report (dict) and the confusion matrix.

    Raises ValueError when the data is too small or single-class to evaluate
    honestly, rather than returning a flattering resubstitution score.
    """
    if frame is None or frame.empty:
        raise ValueError('No labelled data to evaluate.')

    features = frame[FEATURE_COLUMNS]
    labels = frame['category_name']
    counts = labels.value_counts()
    if len(counts) < 2:
        raise ValueError('Need at least two categories to evaluate.')

    n_splits = int(min(max_splits, counts.min()))
    if n_splits < 2:
        raise ValueError('The rarest category has fewer than two examples, so '
                         'stratified cross-validation is not possible.')

    ordered_labels = sorted(labels.unique())
    splitter = StratifiedKFold(n_splits=n_splits, shuffle=True,
                               random_state=random_state)

    results = {}
    for name, pipeline in _candidate_pipelines().items():
        predicted = cross_val_predict(pipeline, features, labels, cv=splitter)
        results[name] = {
            'y_true': labels.to_numpy(),
            'y_pred': predicted,
            'labels': ordered_labels,
            'macro_f1': float(f1_score(labels, predicted, average='macro',
                                       zero_division=0)),
            'accuracy': float((labels.to_numpy() == predicted).mean()),
            'report': classification_report(labels, predicted,
                                            labels=ordered_labels,
                                            output_dict=True, zero_division=0),
            'confusion': confusion_matrix(labels, predicted, labels=ordered_labels),
        }
    return results, n_splits


def best_model(results):
    """Return (name, macro_f1) of the highest macro-F1 model, ties to first."""
    name = max(results, key=lambda key: results[key]['macro_f1'])
    return name, results[name]['macro_f1']


def meets_target(macro_f1):
    """Whether a macro F1 clears the NFR-02 acceptance criterion."""
    return macro_f1 >= config.TARGET_F1
