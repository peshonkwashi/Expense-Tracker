"""Expenditure Forecasting Module (section 3.6.3, FR-06).

Builds a per-category monthly spending series and produces a month-ahead
forecast with ARIMA, falling back to a trailing mean when a category has too
short a history to identify an autoregressive model.

Two things the naive version of this module gets wrong, and why they matter:

1. The current month is still in progress. Including it as a data point tells
   the model that spending collapsed, dragging every forecast down. Training
   stops at the last complete month.
2. A category with no spending in a month is absent from a GROUP BY result, not
   zero. Without reindexing onto a continuous month range the series silently
   skips months and ARIMA fits the wrong lag structure.
"""
import warnings

import numpy as np
import pandas as pd

import config
import database

MONTHLY_SPEND_QUERY = (
    "SELECT strftime('%Y-%m', t.transaction_date) AS month, "
    "       c.category_name AS category, "
    "       SUM(t.amount) AS total_amount "
    "FROM Transaction_Record t "
    "JOIN Category c ON t.category_id = c.category_id "
    "WHERE t.transaction_type = 'DEBIT' AND t.is_salary = 0 AND t.user_id = ? "
    "GROUP BY month, c.category_name "
    "ORDER BY month"
)


def current_month(today=None):
    today = today or pd.Timestamp.today()
    return pd.Timestamp(today).to_period('M')


def monthly_matrix(conn, user_id, include_current_month=False, today=None):
    """Return a months x categories DataFrame of spending, gaps filled with 0.

    Rows are a continuous monthly index; columns are category names. The
    in-progress month is excluded by default so partial data never trains a
    model (see module docstring).
    """
    frame = pd.read_sql_query(MONTHLY_SPEND_QUERY, conn, params=[user_id])
    if frame.empty:
        return pd.DataFrame()

    frame['period'] = pd.PeriodIndex(frame['month'], freq='M')
    pivot = frame.pivot_table(index='period', columns='category',
                              values='total_amount', aggfunc='sum', fill_value=0.0)

    full_index = pd.period_range(pivot.index.min(), pivot.index.max(), freq='M')
    pivot = pivot.reindex(full_index, fill_value=0.0).sort_index()

    if not include_current_month:
        pivot = pivot[pivot.index < current_month(today)]
    return pivot


def _predict_arima(values):
    from statsmodels.tsa.arima.model import ARIMA
    # Convergence warnings are expected on series this short and are handled by
    # method selection, which simply scores ARIMA lower when it fits badly.
    # The filter is scoped to this call rather than set globally, so warnings
    # from the rest of the application still surface.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        model = ARIMA(np.asarray(values, dtype=float), order=(1, 0, 0))
        fitted = model.fit()
        return float(np.asarray(fitted.forecast(steps=1))[0])


def _predict_mean(values):
    return float(np.mean(np.asarray(values, dtype=float)[-3:]))


def _predict_median(values):
    return float(np.median(np.asarray(values, dtype=float)[-3:]))


# Candidate one-step-ahead forecasters. ARIMA is the primary model per section
# 3.7.3; the trailing mean and median are the robust alternatives that tend to
# win on short, noisy series where a 7-point autoregression overfits.
CANDIDATES = {
    'arima': _predict_arima,
    'mean': _predict_mean,
    'median': _predict_median,
}

METHOD_LABELS = {
    'arima': 'ARIMA(1,0,0)',
    'mean': '3-month average',
    'median': '3-month median',
}

# ARIMA is kept when it is within this margin of the best candidate, so the
# designated primary model is not displaced by rounding noise.
ARIMA_PREFERENCE_MARGIN = 1.02


def _safe_predict(method, values):
    try:
        prediction = CANDIDATES[method](values)
    except Exception:
        return None
    if prediction is None or not np.isfinite(prediction):
        return None
    return max(0.0, float(prediction))


def select_method(values, min_origins=3):
    """Choose a forecaster by rolling-origin one-step-ahead error.

    For each of the last few months in turn, every candidate is fitted on the
    months before it and scored against what actually happened. The candidate
    with the lowest mean absolute error is chosen for this category.

    This is time-series cross-validation: training always precedes testing, so
    no future observation informs a prediction of the past.

    Returns (method_name, cv_mae) where cv_mae is None if there was not enough
    history to score the candidates.
    """
    values = np.asarray(values, dtype=float)
    origins = min(min_origins, len(values) - config.MIN_MONTHS_FOR_ARIMA)
    if origins < 2:
        return 'mean', None

    errors = {}
    for method in CANDIDATES:
        scores = []
        for offset in range(origins, 0, -1):
            history = values[:-offset]
            actual = values[-offset]
            prediction = _safe_predict(method, history)
            if prediction is None:
                scores = []
                break
            scores.append(abs(prediction - actual))
        if scores:
            errors[method] = float(np.mean(scores))

    if not errors:
        return 'mean', None

    best = min(errors, key=errors.get)
    if ('arima' in errors and best != 'arima'
            and errors['arima'] <= errors[best] * ARIMA_PREFERENCE_MARGIN):
        best = 'arima'
    return best, errors[best]


def _forecast_series(series):
    """Forecast one category. Returns (amount, method, basis_text)."""
    values = series.astype(float)
    observations = len(values)

    if observations == 0:
        return 0.0, 'none', 'No completed months of history.'

    if observations < config.MIN_MONTHS_FOR_ARIMA:
        window = values.tail(3)
        mean = float(window.mean())
        return (max(0.0, mean), 'mean',
                f'Average of your last {len(window)} month(s): a stable-average '
                f'estimate is used until {config.MIN_MONTHS_FOR_ARIMA} months of '
                f'history exist for time-series modelling.')

    if float(values.std()) < 1e-9:
        constant = float(values.iloc[-1])
        return (max(0.0, constant), 'constant',
                'This amount has been identical every month, so it is carried '
                'forward unchanged.')

    method, cv_mae = select_method(values.to_numpy(dtype=float))
    prediction = _safe_predict(method, values.to_numpy(dtype=float))

    if prediction is None:
        prediction = max(0.0, _predict_mean(values.to_numpy(dtype=float)))
        return (prediction, 'mean',
                'The time-series model could not be fitted for this category, '
                'so a 3-month average is used instead.')

    basis = (f'{METHOD_LABELS[method]} fitted on {observations} completed months '
             f'of your own spending in this category')
    if cv_mae is not None:
        basis += (f', chosen because it had the lowest error '
                  f'({cv_mae:,.0f} {config.CURRENCY} average miss) when tested '
                  f'against months the model had not seen')
    return prediction, method, basis + '.'


def generate_forecasts(conn, user_id, today=None):
    """Month-ahead forecast per category.

    Returns {category_name: {'amount', 'method', 'basis', 'history', 'months'}}.
    """
    matrix = monthly_matrix(conn, user_id, today=today)
    if matrix.empty:
        return {}

    forecasts = {}
    for category in matrix.columns:
        series = matrix[category]
        # A category the user stopped using months ago should not be budgeted
        # for; require activity in the last three completed months.
        if float(series.tail(3).sum()) == 0:
            continue
        amount, method, basis = _forecast_series(series)
        forecasts[category] = {
            'amount': round(float(amount), 2),
            'method': method,
            'basis': basis,
            'history': [round(float(v), 2) for v in series.tolist()],
            'months': [str(p) for p in series.index],
        }
    return forecasts


def backtest(conn, user_id, today=None):
    """Time-series-aware evaluation of the forecaster (section 3.7.4, NFR-03).

    Trains on months 1..N-1 and tests on month N, preserving temporal ordering
    so no future observation leaks into training. Reports MAE in Kwacha and as
    a ratio of actual spend, which is the form NFR-03 is stated in.
    """
    matrix = monthly_matrix(conn, user_id, today=today)
    result = {'evaluated': False, 'reason': None, 'mae': None,
              'mae_ratio': None, 'per_category': {}, 'months_used': 0}

    if matrix.empty or len(matrix) < 3:
        result['reason'] = ('At least three completed months are needed for a '
                            'hold-out evaluation.')
        return result

    train, test = matrix.iloc[:-1], matrix.iloc[-1]
    errors, actuals = [], []

    for category in matrix.columns:
        history = train[category]
        if float(history.sum()) == 0:
            continue
        predicted, method, _ = _forecast_series(history)
        actual = float(test[category])
        errors.append(abs(predicted - actual))
        actuals.append(actual)
        result['per_category'][category] = {
            'predicted': round(predicted, 2),
            'actual': round(actual, 2),
            'absolute_error': round(abs(predicted - actual), 2),
            'method': method,
        }

    if not errors:
        result['reason'] = 'No category has enough history to evaluate.'
        return result

    mae = float(np.mean(errors))
    total_actual = float(np.sum(actuals))
    mae_ratio = (mae / (total_actual / len(actuals))) if total_actual > 0 else None

    result.update({
        'evaluated': True,
        'mae': round(mae, 2),
        'mae_ratio': round(mae_ratio, 4) if mae_ratio is not None else None,
        'months_used': len(matrix),
        'test_month': str(matrix.index[-1]),
        'meets_target': (mae_ratio is not None and mae_ratio <= config.MAX_MAE_RATIO),
    })

    database.record_metric(
        conn, 'FORECASTING', algorithm='ARIMA(1,0,0) with mean/median selection',
        mae=result['mae'], mae_ratio=result['mae_ratio'],
        sample_count=len(matrix),
        notes=f"Hold-out month {result['test_month']}; "
              f"{len(result['per_category'])} categories evaluated",
        user_id=user_id)
    conn.commit()
    return result
