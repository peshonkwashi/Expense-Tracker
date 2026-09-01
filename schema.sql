-- Salary-Linked Expense Tracker and Budget Advisor
-- Relational schema (section 5.7 of the project report).
-- Every statement is idempotent so the file can be replayed on every launch.

CREATE TABLE IF NOT EXISTS User (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    salary_amount REAL NOT NULL,
    salary_day INTEGER NOT NULL CHECK(salary_day BETWEEN 1 AND 31),
    learning_started_on DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS Category (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name TEXT NOT NULL UNIQUE,
    category_type TEXT NOT NULL CHECK(category_type IN ('ESSENTIAL', 'DISCRETIONARY'))
);

CREATE TABLE IF NOT EXISTS Transaction_Record (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date DATE NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('CREDIT', 'DEBIT')),
    is_salary INTEGER NOT NULL DEFAULT 0,
    is_subscription INTEGER NOT NULL DEFAULT 0,
    -- How the category was assigned: model | rule | user | default (FR-03, transparency)
    category_source TEXT DEFAULT 'default',
    category_confidence REAL,
    -- Deduplication key for repeated statement uploads (FR-01)
    import_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER,
    category_id INTEGER,
    FOREIGN KEY(user_id) REFERENCES User(user_id),
    FOREIGN KEY(category_id) REFERENCES Category(category_id)
);

CREATE TABLE IF NOT EXISTS BudgetRecommendation (
    recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_year TEXT NOT NULL,
    recommended_amount REAL NOT NULL,
    forecast_amount REAL NOT NULL,
    forecast_method TEXT,
    explanation TEXT,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER,
    category_id INTEGER,
    FOREIGN KEY(user_id) REFERENCES User(user_id),
    FOREIGN KEY(category_id) REFERENCES Category(category_id)
);

CREATE TABLE IF NOT EXISTS SavingsGoal (
    goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_name TEXT NOT NULL,
    target_amount REAL NOT NULL CHECK(target_amount > 0),
    target_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER,
    FOREIGN KEY(user_id) REFERENCES User(user_id)
);

CREATE TABLE IF NOT EXISTS ModelMetrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT NOT NULL,        -- CATEGORISATION | FORECASTING
    algorithm TEXT,                  -- MultinomialNB | RandomForest | ARIMA | Mean
    f1_score REAL,
    mae REAL,
    mae_ratio REAL,
    sample_count INTEGER,
    notes TEXT,
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER,
    FOREIGN KEY(user_id) REFERENCES User(user_id)
);

-- Application-level key/value store: password hash, first-launch data notice.
CREATE TABLE IF NOT EXISTS AppSetting (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Seed categories. Names are stable identifiers used by the rule seed in
-- ml/categorization.py, so they must not be renamed without updating it.
INSERT OR IGNORE INTO Category (category_name, category_type) VALUES
('Groceries',     'ESSENTIAL'),
('Transport',     'ESSENTIAL'),
('Utilities',     'ESSENTIAL'),
('Housing',       'ESSENTIAL'),
('Healthcare',    'ESSENTIAL'),
('Education',     'ESSENTIAL'),
('Airtime & Data','ESSENTIAL'),
('Loan & Debt',   'ESSENTIAL'),
('Dining Out',    'DISCRETIONARY'),
('Entertainment', 'DISCRETIONARY'),
('Subscriptions', 'DISCRETIONARY'),
('Shopping',      'DISCRETIONARY'),
('Personal Care', 'DISCRETIONARY'),
('Transfers',     'DISCRETIONARY'),
('Uncategorised', 'DISCRETIONARY');
