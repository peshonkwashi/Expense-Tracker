"""Central configuration for the Salary-Linked Expense Tracker.

Every tunable constant referenced by the project report lives here so that the
thresholds quoted in the requirements (FR-09, NFR-02, NFR-03, section 5.9) have
exactly one definition in the codebase.
"""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# --- Storage (all local, NFR-05) ------------------------------------------
DATABASE_PATH = os.path.join(BASE_DIR, 'expense_tracker.db')
SCHEMA_PATH = os.path.join(BASE_DIR, 'schema.sql')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
MODEL_DIR = os.path.join(BASE_DIR, 'ml', 'models')
CATEGORISER_PATH = os.path.join(MODEL_DIR, 'categoriser.joblib')
SECRET_KEY_FILE = os.path.join(BASE_DIR, '.flask_secret')

CURRENCY = 'ZMW'

# --- Security (section 5.9) -----------------------------------------------
SESSION_TIMEOUT_MINUTES = 10
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {'.csv'}

# --- Behavioural Learning Phase (FR-05) -----------------------------------
# The phase completes once the transaction history spans one full salary cycle.
LEARNING_PHASE_DAYS = 30
MIN_LEARNING_TRANSACTIONS = 15

# --- Categorisation (FR-02, NFR-02) ---------------------------------------
MIN_TRAINING_SAMPLES = 20        # below this the rule seed carries the system
MODEL_CONFIDENCE_FLOOR = 0.60    # below this we fall back to the rule seed
TARGET_F1 = 0.85                 # NFR-02 acceptance criterion

# --- Forecasting (FR-06, NFR-03) ------------------------------------------
MIN_MONTHS_FOR_ARIMA = 4
MAX_MAE_RATIO = 0.15             # NFR-03 acceptance criterion

# --- Subscription detection (FR-08) ---------------------------------------
SUBSCRIPTION_MIN_OCCURRENCES = 3
SUBSCRIPTION_AMOUNT_TOLERANCE = 0.15   # +/- 15% of the median amount
SUBSCRIPTION_MIN_INTERVAL_DAYS = 24
SUBSCRIPTION_MAX_INTERVAL_DAYS = 38

# --- Budgeting and nudges (FR-07, FR-09) ----------------------------------
ESSENTIAL_BUFFER = 0.05          # 5% headroom on essential categories
NUDGE_THRESHOLD = 0.80           # alert at 80% of the recommended allocation
DEFAULT_SAVINGS_RATE = 0.10      # floor applied when the user has no goals
