"""
SafeRefund MCP — internal refund backend for an online store.

Five atomic functions that an AI agent (or Synapse CLI) can compose into a
single smart refund tool:

    1. get_order_details          — look up an order
    2. check_refund_policy        — validate the refund against store policy
    3. calculate_refund_amount    — compute the exact amount to return
    4. execute_refund_transaction — mark the order refunded and persist it
    5. send_refund_notification   — build the customer-facing receipt

Data lives in ``store_db.json`` (in the current working directory). If the file
does not exist, it is created automatically and seeded with mock orders.

Only the Python standard library is used.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

DB_PATH: str = "store_db.json"

REFUND_WINDOW_DAYS: int = 30
NON_REFUNDABLE_CATEGORIES: List[str] = ["digital", "perishable"]
VALID_STATUSES: List[str] = ["delivered", "refunded", "in_transit"]

# Share of the paid amount returned to the customer, by refund reason.
REFUND_RATES: Dict[str, float] = {
    "defective": 1.00,      # full refund
    "changed_mind": 0.90,   # 10% withheld to cover return shipping
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RefundError(Exception):
    """Base class for all SafeRefund errors."""


class OrderNotFoundError(RefundError):
    """Raised when an order ID does not exist in the database."""


class InvalidReasonError(RefundError):
    """Raised when a refund reason is not one of the supported reasons."""


class RefundNotAllowedError(RefundError):
    """Raised when a refund operation would violate store policy or data integrity."""


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _build_mock_data() -> Dict[str, Any]:
    """Build the seed dataset with purchase dates relative to the current time."""
    now = _now()
    return {
        "orders": {
            "ORD-101": {
                "order_id": "ORD-101",
                "customer": {"name": "Alice Johnson", "email": "alice@example.com"},
                "product": {"name": "Sony Headphones", "category": "electronics"},
                "amount": 250.0,
                "currency": "USD",
                "purchase_date": _iso(now - timedelta(days=5)),
                "status": "delivered",
                "refund": None,
            },
            "ORD-102": {
                "order_id": "ORD-102",
                "customer": {"name": "Bob Smith", "email": "bob@example.com"},
                "product": {"name": "Keyboard", "category": "electronics"},
                "amount": 120.0,
                "currency": "USD",
                "purchase_date": _iso(now - timedelta(days=45)),
                "status": "delivered",
                "refund": None,
            },
            "ORD-103": {
                "order_id": "ORD-103",
                "customer": {"name": "Carol White", "email": "carol@example.com"},
                "product": {"name": "Software License", "category": "digital"},
                "amount": 60.0,
                "currency": "USD",
                "purchase_date": _iso(now - timedelta(days=2)),
                "status": "delivered",
                "refund": None,
            },
            "ORD-104": {
                "order_id": "ORD-104",
                "customer": {"name": "David Brown", "email": "david@example.com"},
                "product": {"name": "Jacket", "category": "clothing"},
                "amount": 180.0,
                "currency": "USD",
                "purchase_date": _iso(now - timedelta(days=10)),
                "status": "refunded",
                "refund": {
                    "refund_id": "RF-ORD-104-SEED",
                    "amount": 180.0,
                    "refunded_at": _iso(now - timedelta(days=3)),
                },
            },
        },
        "transactions": [
            {
                "refund_id": "RF-ORD-104-SEED",
                "order_id": "ORD-104",
                "amount": 180.0,
                "refunded_at": _iso(now - timedelta(days=3)),
            }
        ],
    }


def _save_db(db: Dict[str, Any]) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def _load_db() -> Dict[str, Any]:
    """Load the database, creating it with mock data if it does not exist yet."""
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        db = _build_mock_data()
        _save_db(db)
        return db


def _get_order(db: Dict[str, Any], order_id: str) -> Dict[str, Any]:
    if not isinstance(order_id, str) or not order_id.strip():
        raise OrderNotFoundError("order_id must be a non-empty string.")
    order = db["orders"].get(order_id.strip().upper())
    if order is None:
        raise OrderNotFoundError(f"Order '{order_id}' was not found.")
    return order


def _normalize_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise InvalidReasonError("reason must be a string.")
    normalized = reason.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized not in REFUND_RATES:
        raise InvalidReasonError(
            f"Unsupported refund reason '{reason}'. "
            f"Supported reasons: {', '.join(REFUND_RATES)}."
        )
    return normalized


def reset_database() -> Dict[str, Any]:
    """
    Recreate ``store_db.json`` from scratch with fresh mock data.

    Useful between demo runs (e.g. after ORD-101 has been refunded).

    Returns:
        dict: ``{"reset": True, "orders": [<order ids>]}``.
    """
    db = _build_mock_data()
    _save_db(db)
    return {"reset": True, "orders": list(db["orders"])}


# ---------------------------------------------------------------------------
# 1. Order lookup
# ---------------------------------------------------------------------------

def get_order_details(order_id: str) -> dict:
    """
    Look up an order by its ID.

    Args:
        order_id (str): Order identifier, e.g. ``"ORD-101"`` (case-insensitive).

    Returns:
        dict: Order information::

            {
                "order_id": str,
                "customer_name": str,
                "customer_email": str,
                "product_name": str,
                "category": str,           # e.g. "electronics", "digital"
                "amount": float,           # amount paid
                "currency": str,
                "purchase_date": str,      # ISO 8601, UTC
                "days_since_purchase": int,
                "status": str,             # "delivered" | "refunded" | "in_transit"
                "refund": dict | None      # {"refund_id", "amount", "refunded_at"} if refunded
            }

    Raises:
        OrderNotFoundError: If ``order_id`` is empty or does not exist.
    """
    db = _load_db()
    order = _get_order(db, order_id)
    purchase_date = datetime.fromisoformat(order["purchase_date"])
    return {
        "order_id": order["order_id"],
        "customer_name": order["customer"]["name"],
        "customer_email": order["customer"]["email"],
        "product_name": order["product"]["name"],
        "category": order["product"]["category"],
        "amount": float(order["amount"]),
        "currency": order["currency"],
        "purchase_date": order["purchase_date"],
        "days_since_purchase": (_now() - purchase_date).days,
        "status": order["status"],
        "refund": order["refund"],
    }


# ---------------------------------------------------------------------------
# 2. Policy check
# ---------------------------------------------------------------------------

def check_refund_policy(order_id: str, reason: str) -> dict:
    """
    Check whether an order is eligible for a refund under store policy.

    Rules:
        * The purchase was made less than 30 days ago.
        * The product category is not non-refundable (``digital``, ``perishable``).
        * The order has not already been refunded.
        * The order has been delivered (``in_transit`` orders must be cancelled,
          not refunded).
        * The reason is supported (``defective`` or ``changed_mind``).

    A policy violation is NOT raised as an exception — it is reported in the
    result so the caller can explain it to the customer.

    Args:
        order_id (str): Order identifier, e.g. ``"ORD-101"``.
        reason (str): Refund reason: ``"defective"`` or ``"changed_mind"``.

    Returns:
        dict::

            {
                "order_id": str,
                "reason": str,
                "eligible": bool,
                "days_since_purchase": int,
                "refund_window_days": int,
                "violations": list[str]    # human-readable reasons; empty if eligible
            }

    Raises:
        OrderNotFoundError: If the order does not exist.
    """
    order = get_order_details(order_id)
    violations: List[str] = []

    normalized_reason = reason
    try:
        normalized_reason = _normalize_reason(reason)
    except InvalidReasonError as e:
        violations.append(str(e))

    if order["status"] == "refunded":
        refunded_at = order["refund"]["refunded_at"] if order["refund"] else "unknown date"
        violations.append(f"Order has already been refunded (on {refunded_at}).")
    elif order["status"] == "in_transit":
        violations.append("Order is still in transit; it must be delivered before a refund.")

    if order["days_since_purchase"] >= REFUND_WINDOW_DAYS:
        violations.append(
            f"Refund window expired: purchased {order['days_since_purchase']} days ago "
            f"(limit is less than {REFUND_WINDOW_DAYS} days)."
        )

    if order["category"] in NON_REFUNDABLE_CATEGORIES:
        violations.append(f"Category '{order['category']}' is non-refundable.")

    return {
        "order_id": order["order_id"],
        "reason": normalized_reason,
        "eligible": not violations,
        "days_since_purchase": order["days_since_purchase"],
        "refund_window_days": REFUND_WINDOW_DAYS,
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# 3. Amount calculation
# ---------------------------------------------------------------------------

def calculate_refund_amount(order_id: str, reason: str) -> dict:
    """
    Calculate the exact refund amount for an order.

    * ``defective``    → 100% of the paid amount.
    * ``changed_mind`` → 90% of the paid amount (10% return-shipping fee withheld).

    This function only calculates; it does not check policy or change any data.

    Args:
        order_id (str): Order identifier, e.g. ``"ORD-101"``.
        reason (str): Refund reason: ``"defective"`` or ``"changed_mind"``.

    Returns:
        dict::

            {
                "order_id": str,
                "reason": str,
                "original_amount": float,
                "fee_percent": float,      # 0.0 or 10.0
                "fee_amount": float,
                "refund_amount": float,    # rounded to 2 decimals
                "currency": str
            }

    Raises:
        OrderNotFoundError: If the order does not exist.
        InvalidReasonError: If ``reason`` is not supported.
    """
    normalized_reason = _normalize_reason(reason)
    order = get_order_details(order_id)

    original = order["amount"]
    refund_amount = round(original * REFUND_RATES[normalized_reason], 2)
    fee_amount = round(original - refund_amount, 2)

    return {
        "order_id": order["order_id"],
        "reason": normalized_reason,
        "original_amount": original,
        "fee_percent": round((1 - REFUND_RATES[normalized_reason]) * 100, 2),
        "fee_amount": fee_amount,
        "refund_amount": refund_amount,
        "currency": order["currency"],
    }


# ---------------------------------------------------------------------------
# 4. Refund execution
# ---------------------------------------------------------------------------

def execute_refund_transaction(order_id: str, amount: float) -> dict:
    """
    Execute a refund: set the order status to ``refunded`` and persist the
    transaction (amount and timestamp) in ``store_db.json``.

    As a last line of defence this function refuses to double-refund an order,
    refund an undelivered order, or refund more than was paid. It does NOT
    re-check the 30-day window or category rules — call
    ``check_refund_policy`` first.

    Args:
        order_id (str): Order identifier, e.g. ``"ORD-101"``.
        amount (float): Amount to refund. Must be > 0 and <= the paid amount.

    Returns:
        dict::

            {
                "success": True,
                "refund_id": str,          # e.g. "RF-ORD-101-20261123143005"
                "order_id": str,
                "amount": float,
                "currency": str,
                "refunded_at": str,        # ISO 8601, UTC
                "new_status": "refunded"
            }

    Raises:
        OrderNotFoundError: If the order does not exist.
        RefundNotAllowedError: If the order is already refunded, not delivered,
            or ``amount`` is not a positive number <= the paid amount.
    """
    db = _load_db()
    order = _get_order(db, order_id)

    if order["status"] == "refunded":
        raise RefundNotAllowedError(f"Order '{order['order_id']}' has already been refunded.")
    if order["status"] != "delivered":
        raise RefundNotAllowedError(
            f"Order '{order['order_id']}' has status '{order['status']}'; "
            "only delivered orders can be refunded."
        )
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise RefundNotAllowedError("amount must be a number.")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise RefundNotAllowedError("amount must be greater than 0.")
    if amount > order["amount"]:
        raise RefundNotAllowedError(
            f"amount {amount:.2f} exceeds the paid amount {order['amount']:.2f}."
        )

    now = _now()
    refund_id = f"RF-{order['order_id']}-{now.strftime('%Y%m%d%H%M%S')}"
    refunded_at = _iso(now)

    order["status"] = "refunded"
    order["refund"] = {"refund_id": refund_id, "amount": amount, "refunded_at": refunded_at}
    db["transactions"].append(
        {
            "refund_id": refund_id,
            "order_id": order["order_id"],
            "amount": amount,
            "refunded_at": refunded_at,
        }
    )
    _save_db(db)

    return {
        "success": True,
        "refund_id": refund_id,
        "order_id": order["order_id"],
        "amount": amount,
        "currency": order["currency"],
        "refunded_at": refunded_at,
        "new_status": "refunded",
    }


# ---------------------------------------------------------------------------
# 5. Customer notification
# ---------------------------------------------------------------------------

def send_refund_notification(order_id: str, refund_id: str, amount: float) -> dict:
    """
    Build the refund notification / receipt for the customer.

    The message is returned (not actually emailed). The refund must already be
    recorded in the database and match ``refund_id`` and ``amount``, so a
    receipt can never be issued for a refund that did not happen.

    Args:
        order_id (str): Order identifier, e.g. ``"ORD-101"``.
        refund_id (str): ID returned by ``execute_refund_transaction``.
        amount (float): Refunded amount.

    Returns:
        dict::

            {
                "sent": True,
                "channel": "email",
                "recipient": str,          # customer email
                "subject": str,
                "message": str             # full receipt text
            }

    Raises:
        OrderNotFoundError: If the order does not exist.
        RefundNotAllowedError: If the order has no recorded refund, or the
            recorded refund does not match ``refund_id`` / ``amount``.
    """
    order = get_order_details(order_id)
    refund = order["refund"]

    if order["status"] != "refunded" or not refund:
        raise RefundNotAllowedError(f"Order '{order['order_id']}' has no recorded refund.")
    if refund["refund_id"] != refund_id:
        raise RefundNotAllowedError(
            f"refund_id '{refund_id}' does not match the recorded refund "
            f"'{refund['refund_id']}' for order '{order['order_id']}'."
        )
    if round(float(amount), 2) != round(float(refund["amount"]), 2):
        raise RefundNotAllowedError(
            f"amount {float(amount):.2f} does not match the recorded refund "
            f"amount {refund['amount']:.2f}."
        )

    currency = order["currency"]
    subject = f"Your refund for order {order['order_id']} has been processed"
    message = (
        f"Hi {order['customer_name']},\n\n"
        f"We have processed a refund for your order {order['order_id']}.\n\n"
        f"  Product:        {order['product_name']}\n"
        f"  Paid:           {order['amount']:.2f} {currency}\n"
        f"  Refunded:       {refund['amount']:.2f} {currency}\n"
        f"  Refund ID:      {refund['refund_id']}\n"
        f"  Processed at:   {refund['refunded_at']}\n\n"
        f"The money will appear on your original payment method within 5-10 "
        f"business days.\n\n"
        f"Thank you for shopping with us!"
    )

    return {
        "sent": True,
        "channel": "email",
        "recipient": order["customer_email"],
        "subject": subject,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Read-only smoke check: `python refund_service.py`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for oid in ["ORD-101", "ORD-102", "ORD-103", "ORD-104"]:
        details = get_order_details(oid)
        policy = check_refund_policy(oid, "defective")
        verdict = "ELIGIBLE" if policy["eligible"] else "REJECTED: " + " ".join(policy["violations"])
        print(f"{oid} | {details['product_name']:<16} | {details['status']:<9} | {verdict}")
