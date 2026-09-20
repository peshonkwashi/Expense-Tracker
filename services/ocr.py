"""Mock OCR service for the scan-receipt feature.

Tesseract OCR is not available in the local environment, so this simulates
extraction: given a transaction it produces a plausible itemised receipt whose
lines sum to the transaction total.

The items are scenario-specific — the merchant decides what a receipt would
plausibly contain, so a pharmacy yields medication, a supermarket yields
groceries, and a fast-food chain yields menu items. Merchant recognition reuses
the same rule seed the categoriser uses (ml/categorization.rule_category), so
the two never disagree about what a merchant is. A merchant whose purchase is
not really a basket of items (fuel, electricity units, a transfer) gets a
single representative line instead of a nonsensical list of groceries.
"""
import random

from ml.categorization import normalise, rule_category

# Fast-food chains are handled as an explicit exception: whatever category the
# transaction ended up filed under, a receipt from one of these lists menu
# items rather than raw ingredients.
FAST_FOOD_MERCHANTS = (
    'kfc', 'hungry lion', 'debonairs', 'steers', 'mcdonald', 'nandos', 'pizza',
    'chicken inn', 'subway', 'galitos', 'roman', 'ocean basket',
)

# Itemisable scenarios: category -> pool of (item name, unit price in ZMW).
# Prices are indicative Zambian retail values; the selection is trimmed to fit
# the actual transaction total at scan time.
SCENARIO_ITEMS = {
    'Groceries': [            # supermarket
        ('Bread 700g', 25.00), ('Fresh Milk 2L', 40.00), ('Eggs (1 dozen)', 55.00),
        ('Mealie Meal 25kg', 180.00), ('Cooking Oil 2L', 85.00), ('Sugar 2kg', 45.00),
        ('Rice 5kg', 130.00), ('Salt 1kg', 15.00), ('Tomatoes 1kg', 20.00),
        ('Onions 1kg', 18.00), ('Beef 1kg', 120.00), ('Chicken 1.2kg', 95.00),
        ('Kapenta 500g', 90.00), ('Washing Powder 1kg', 60.00), ('Bath Soap', 22.00),
        ('Tea Leaves 250g', 38.00),
    ],
    'Healthcare': [           # pharmacy — medication and health items
        ('Panadol (Paracetamol) 500mg', 35.00), ('Cough Syrup 100ml', 85.00),
        ('Amoxicillin 250mg', 120.00), ('Ibuprofen 400mg', 45.00),
        ('Vitamin C Tablets', 65.00), ('Multivitamins', 150.00),
        ('Malaria Test Kit', 90.00), ('ORS Rehydration Sachets', 30.00),
        ('Antiseptic Cream', 60.00), ('Adhesive Plasters', 25.00),
        ('Cotton Bandage', 40.00), ('Prescription Medication', 250.00),
    ],
    'Dining Out': [           # fast-food chains (the exception)
        ('Burger Meal', 120.00), ('Large Pizza', 180.00), ('Regular Fries', 35.00),
        ('Soft Drink', 25.00), ('Fried Chicken (2pc)', 65.00), ('Zinger Meal', 110.00),
        ('Wings Bucket', 150.00), ('Milkshake', 45.00), ('Coffee', 30.00),
        ('Ice Cream', 28.00),
    ],
    'Personal Care': [        # salon / toiletries
        ('Shampoo 400ml', 55.00), ('Toothpaste', 30.00), ('Deodorant', 48.00),
        ('Body Lotion', 65.00), ('Razor Pack', 40.00), ('Haircut', 80.00),
        ('Bath Towel', 90.00),
    ],
    'Shopping': [             # general retail
        ('T-Shirt', 150.00), ('Jeans', 380.00), ('Sneakers', 550.00),
        ('Phone Charger', 120.00), ('Notebook', 45.00), ('Kitchenware Set', 320.00),
        ('AA Batteries (4pk)', 60.00),
    ],
}

# Categories that do not itemise into a basket: a receipt is a single line.
SINGLE_LINE_LABELS = {
    'Transport': 'Fuel / Transport',
    'Utilities': 'Utility Units',
    'Airtime & Data': 'Airtime / Data Bundle',
    'Housing': 'Rent / Housing',
    'Loan & Debt': 'Loan Repayment',
    'Transfers': 'Transfer',
    'Subscriptions': 'Subscription',
    'Education': 'Tuition / Fees',
    'Entertainment': 'Entertainment',
}

DEFAULT_TARGET = 300.0


def scenario_for(description=None, category=None):
    """Decide which receipt scenario a transaction belongs to.

    Resolution order, most specific first:
      1. A known fast-food chain in the description wins outright (the
         exception: the merchant, not the stored category, decides).
      2. The merchant recognised by the shared rule seed, if it is itemisable.
      3. The transaction's own stored category, if it is itemisable.
      4. Otherwise the merchant/stored category name, used for a single line.
    """
    text = normalise(description)
    if text and any(chain in text for chain in FAST_FOOD_MERCHANTS):
        return 'Dining Out'

    merchant_category = rule_category(description)
    if merchant_category in SCENARIO_ITEMS:
        return merchant_category
    if category in SCENARIO_ITEMS:
        return category
    return merchant_category or category


def scan_receipt(image_path, target_amount=None, category=None, description=None):
    """Simulate OCR extraction, returning a list of item dicts.

    Each item is {'name', 'price', 'quantity'} and the prices sum to
    target_amount, so the itemised receipt always reconciles to the transaction.
    """
    if target_amount is None or target_amount <= 0:
        target_amount = DEFAULT_TARGET
    target_amount = round(float(target_amount), 2)

    scenario = scenario_for(description, category)
    pool = SCENARIO_ITEMS.get(scenario)

    if not pool:
        # Fuel, utilities, a transfer, etc. — one representative line, not a
        # basket. Better an honest single line than invented groceries.
        label = SINGLE_LINE_LABELS.get(scenario, 'Purchase')
        return [{'name': label, 'price': target_amount, 'quantity': 1}]

    items = []
    current = 0.0
    candidates = pool[:]
    random.shuffle(candidates)
    for name, price in candidates:
        if round(current + price, 2) <= target_amount:
            items.append({'name': name, 'price': price, 'quantity': 1})
            current = round(current + price, 2)

    # A final line reconciles the itemised list to the exact transaction total,
    # standing in for whatever the greedy fill could not place precisely.
    remainder = round(target_amount - sum(i['price'] for i in items), 2)
    if remainder > 0:
        items.append({'name': 'Other Items', 'price': remainder, 'quantity': 1})
    return items
