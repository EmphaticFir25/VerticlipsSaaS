"""Plans, monthly minute allowances, and Stripe Checkout / Customer Portal / webhooks."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Optional

import requests

from . import config, db

PERIOD_SECONDS = 30 * 86400
STRIPE_API = "https://api.stripe.com/v1"


class BillingError(RuntimeError):
    pass


def plan_of(user: dict) -> dict:
    return config.PLANS.get(user.get("plan") or "free", config.PLANS["free"])


def refresh_period(user: dict) -> dict:
    """Start a new monthly allowance when the current one is over. Paid plans also reset
    on each paid invoice (see the webhook), whichever comes first."""
    if time.time() - float(user["period_start"]) >= PERIOD_SECONDS:
        db.update_user(user["id"], minutes_used=0, period_start=time.time())
        user = db.get_user(user["id"])
    return user


def usage(user: dict) -> dict:
    user = refresh_period(user)
    plan = plan_of(user)
    used = round(float(user["minutes_used"]), 1)
    return {
        "plan": user.get("plan") or "free",
        "plan_name": plan["name"],
        "minutes_included": plan["minutes"],
        "minutes_used": used,
        "minutes_left": max(0.0, round(plan["minutes"] - used, 1)),
        "resets_at": float(user["period_start"]) + PERIOD_SECONDS,
        "max_clips": plan["max_clips"],
        "watermark": plan["watermark"],
        "brand_logo": plan["brand_logo"],
    }


def minutes_for(duration_seconds: float) -> int:
    return max(1, math.ceil(duration_seconds / 60))


def check_allowance(user: dict, duration_seconds: float) -> int:
    """Minutes this video will cost. Raises BillingError when the plan cannot cover it."""
    if duration_seconds > config.MAX_SOURCE_MINUTES * 60:
        raise BillingError(f"Videos can be up to {config.MAX_SOURCE_MINUTES} minutes long.")
    need = minutes_for(duration_seconds)
    left = usage(user)["minutes_left"]
    if need > left:
        raise BillingError(f"This video needs {need} minutes and your plan has {left:g} left this month. "
                           "Upgrade on the Billing page for more.")
    return need


# ---------- Stripe ----------

def stripe_enabled() -> bool:
    return bool(config.STRIPE_SECRET_KEY and config.STRIPE_PRICE_PRO)


def _stripe(path: str, data: dict) -> dict:
    if not config.STRIPE_SECRET_KEY:
        raise BillingError("Billing is not set up on this server yet.")
    try:
        r = requests.post(f"{STRIPE_API}/{path}", data=data, auth=(config.STRIPE_SECRET_KEY, ""), timeout=30)
    except requests.RequestException as exc:
        raise BillingError("Could not reach Stripe. Try again.") from exc
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        raise BillingError(body.get("error", {}).get("message", "Stripe rejected the request."))
    return body


def checkout_url(user: dict, plan: str) -> str:
    price = config.stripe_price(plan)
    if plan not in ("pro", "studio") or not price:
        raise BillingError("That plan is not available.")
    data = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": f"{config.PUBLIC_BASE}/billing?checkout=success",
        "cancel_url": f"{config.PUBLIC_BASE}/billing?checkout=cancelled",
        "client_reference_id": user["id"],
        "metadata[user_id]": user["id"],
        "metadata[plan]": plan,
        "subscription_data[metadata][user_id]": user["id"],
        "allow_promotion_codes": "true",
    }
    if user.get("stripe_customer"):
        data["customer"] = user["stripe_customer"]
    else:
        data["customer_email"] = user["email"]
    return _stripe("checkout/sessions", data)["url"]


def cancel_subscription(subscription_id: str) -> None:
    """Cancel immediately so a deleted account is never billed again. Already-cancelled counts as success."""
    if not config.STRIPE_SECRET_KEY:
        return
    try:
        r = requests.delete(f"{STRIPE_API}/subscriptions/{subscription_id}", auth=(config.STRIPE_SECRET_KEY, ""), timeout=30)
    except requests.RequestException as exc:
        raise BillingError("Could not reach Stripe to cancel your subscription. Try again.") from exc
    if r.status_code >= 400 and r.status_code != 404:
        raise BillingError("Stripe could not cancel your subscription. Cancel it from Plan & billing first.")


def portal_url(user: dict) -> str:
    if not user.get("stripe_customer"):
        raise BillingError("You don't have a subscription to manage yet.")
    return _stripe("billing_portal/sessions", {"customer": user["stripe_customer"],
                                               "return_url": f"{config.PUBLIC_BASE}/billing"})["url"]


def verify_signature(payload: bytes, header: str, secret: str, tolerance: int = 300,
                     now: Optional[float] = None) -> bool:
    """Stripe-Signature check: HMAC-SHA256 of '<t>.<payload>' against any v1 signature."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def handle_event(event: dict) -> str:
    """Apply one verified webhook event. Returns a short note for logs."""
    if db.seen_stripe_event(event.get("id", "")):
        return "duplicate"
    kind = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if kind == "checkout.session.completed":
        user = db.get_user(obj.get("client_reference_id") or obj.get("metadata", {}).get("user_id", ""))
        plan = obj.get("metadata", {}).get("plan")
        if not user or plan not in ("pro", "studio"):
            return "ignored"
        db.update_user(user["id"], plan=plan, stripe_customer=obj.get("customer"),
                       stripe_subscription=obj.get("subscription"), minutes_used=0, period_start=time.time())
        return f"upgraded {user['id']} to {plan}"

    if kind in ("customer.subscription.updated", "customer.subscription.created"):
        user = db.get_user_by_customer(obj.get("customer", ""))
        if not user:
            return "ignored"
        if obj.get("status") in ("canceled", "unpaid", "incomplete_expired"):
            db.update_user(user["id"], plan="free", stripe_subscription=None)
            return f"downgraded {user['id']}"
        items = obj.get("items", {}).get("data", [])
        plan = config.plan_for_price(items[0].get("price", {}).get("id", "")) if items else None
        if plan:
            db.update_user(user["id"], plan=plan, stripe_subscription=obj.get("id"))
        return f"subscription {obj.get('status')} for {user['id']}"

    if kind == "customer.subscription.deleted":
        user = db.get_user_by_customer(obj.get("customer", ""))
        if user:
            db.update_user(user["id"], plan="free", stripe_subscription=None)
            return f"downgraded {user['id']}"
        return "ignored"

    if kind == "invoice.paid":
        user = db.get_user_by_customer(obj.get("customer", ""))
        if user and obj.get("billing_reason") in ("subscription_cycle", "subscription_create"):
            db.update_user(user["id"], minutes_used=0, period_start=time.time())
            return f"reset usage for {user['id']}"
        return "ignored"

    return "ignored"


def parse_event(payload: bytes) -> dict:
    try:
        return json.loads(payload)
    except ValueError:
        raise BillingError("Bad webhook payload.")
