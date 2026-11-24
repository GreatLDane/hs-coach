"""Stripe Pro subscription Checkout, Customer Portal, and webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from hs_coach.coach import _load_dotenv_if_present
from hs_coach.storage import GameStore

STRIPE_API_BASE = "https://api.stripe.com/v1"


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    pro_price_id: str
    public_url: str


def resolve_stripe_config() -> StripeConfig:
    """Resolve Stripe credentials from environment or local .env."""
    _load_dotenv_if_present()
    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    webhook = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    price_id = os.environ.get("STRIPE_PRO_PRICE_ID", "").strip()
    public_url = os.environ.get("HS_COACH_PUBLIC_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    return StripeConfig(
        secret_key=secret,
        webhook_secret=webhook,
        pro_price_id=price_id,
        public_url=public_url,
    )


def create_checkout_session(
    cfg: StripeConfig,
    *,
    user_id: str,
    email: str,
    customer_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for the Pro monthly subscription."""
    if not cfg.secret_key or not cfg.pro_price_id:
        raise ValueError("Stripe Checkout is not configured (missing STRIPE_SECRET_KEY or STRIPE_PRO_PRICE_ID).")

    form: dict[str, str] = {
        "mode": "subscription",
        "success_url": f"{cfg.public_url}/?billing=success",
        "cancel_url": f"{cfg.public_url}/?billing=cancel",
        "client_reference_id": user_id,
        "metadata[user_id]": user_id,
        "line_items[0][price]": cfg.pro_price_id,
        "line_items[0][quantity]": "1",
    }
    if customer_id:
        form["customer"] = customer_id
    else:
        form["customer_email"] = email

    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.post(
            f"{STRIPE_API_BASE}/checkout/sessions",
            headers={"Authorization": f"Bearer {cfg.secret_key}"},
            data=form,
        )
        resp.raise_for_status()
        return dict(resp.json())
    finally:
        if owns_client:
            http.close()


def create_billing_portal_session(
    cfg: StripeConfig,
    *,
    customer_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create a Stripe Customer Portal session so Pro users can manage or cancel."""
    if not cfg.secret_key:
        raise ValueError("Stripe is not configured (missing STRIPE_SECRET_KEY).")
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.post(
            f"{STRIPE_API_BASE}/billing_portal/sessions",
            headers={"Authorization": f"Bearer {cfg.secret_key}"},
            data={
                "customer": customer_id,
                "return_url": f"{cfg.public_url}/",
            },
        )
        resp.raise_for_status()
        return dict(resp.json())
    finally:
        if owns_client:
            http.close()


def fetch_stripe_subscription(
    cfg: StripeConfig,
    subscription_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch a Stripe Subscription object by ID to verify price and status server-side."""
    if not cfg.secret_key:
        raise ValueError("Stripe is not configured (missing STRIPE_SECRET_KEY).")
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(
            f"{STRIPE_API_BASE}/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {cfg.secret_key}"},
        )
        resp.raise_for_status()
        return dict(resp.json())
    finally:
        if owns_client:
            http.close()


def _extract_price_ids(items: list[Any]) -> set[str]:
    found: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if isinstance(price, dict) and price.get("id"):
            found.add(str(price["id"]))
        elif isinstance(price, str) and price:
            found.add(price)
        plan = item.get("plan")
        if isinstance(plan, dict) and plan.get("id"):
            found.add(str(plan["id"]))
        elif isinstance(plan, str) and plan:
            found.add(plan)
    return found


def _subscription_has_price(obj: dict[str, Any], pro_price_id: str) -> bool | None:
    """Return True if pro_price_id appears, False if other prices appear, or None if omitted."""
    found: set[str] = set()
    for container_key in ("items", "line_items"):
        container = obj.get(container_key)
        if isinstance(container, dict) and isinstance(container.get("data"), list):
            found |= _extract_price_ids(container["data"])
    plan = obj.get("plan")
    if isinstance(plan, dict) and plan.get("id"):
        found.add(str(plan["id"]))
    if not found:
        return None
    return pro_price_id in found


def verify_stripe_signature(
    payload: bytes,
    sig_header: str,
    webhook_secret: str,
    *,
    tolerance_seconds: int = 300,
    now_ts: int | None = None,
) -> bool:
    """Verify a Stripe-Signature header (`t=<ts>,v1=<hex>`) using HMAC-SHA256."""
    if not sig_header or not webhook_secret:
        return False
    timestamp: int | None = None
    v1_sigs: list[str] = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        if k == "t":
            try:
                timestamp = int(v)
            except ValueError:
                return False
        elif k == "v1":
            v1_sigs.append(v.strip())

    if timestamp is None or not v1_sigs:
        return False

    cur = int(time.time()) if now_ts is None else now_ts
    if abs(cur - timestamp) > tolerance_seconds:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, cand) for cand in v1_sigs)


def handle_stripe_event(
    event: dict[str, Any],
    store: GameStore,
    *,
    config: StripeConfig | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Apply a verified Stripe webhook event idempotently with price and ordering checks."""
    event_id = str(event.get("id") or "").strip()
    etype = str(event.get("type", ""))
    try:
        created_ts = int(event.get("created") or 0)
    except (TypeError, ValueError):
        created_ts = 0

    if event_id and not store.record_stripe_event(event_id, etype, created_ts):
        return {
            "handled": False,
            "duplicate": True,
            "event_type": etype,
            "reason": "duplicate_event",
        }

    obj = (event.get("data") or {}).get("object") or {}
    pro_price_id = config.pro_price_id if config else ""

    if etype == "checkout.session.completed":
        if str(obj.get("mode", "")).lower() != "subscription":
            return {"handled": False, "event_type": etype, "reason": "non_subscription_mode"}
        if str(obj.get("payment_status", "")).lower() not in ("paid", "no_payment_required"):
            return {"handled": False, "event_type": etype, "reason": "payment_not_completed"}
        if str(obj.get("status", "complete")).lower() != "complete":
            return {"handled": False, "event_type": etype, "reason": "session_incomplete"}

        meta = obj.get("metadata") or {}
        uid = meta.get("user_id") or obj.get("client_reference_id")
        user = store.get_user_by_id(str(uid)) if uid else None
        if user is None:
            return {"handled": False, "event_type": etype, "reason": "user_not_found"}

        if created_ts > 0 and created_ts < int(user.get("stripe_last_event_ts") or 0):
            return {"handled": False, "event_type": etype, "reason": "stale_event"}

        cust_id = obj.get("customer")
        sub_id = obj.get("subscription")

        if pro_price_id:
            has_price = _subscription_has_price(obj, pro_price_id)
            if has_price is None and sub_id and config and config.secret_key:
                try:
                    sub_obj = fetch_stripe_subscription(
                        config, str(sub_id), client=client
                    )
                    sub_status = str(sub_obj.get("status", "")).lower()
                    if sub_status in ("active", "trialing"):
                        has_price = _subscription_has_price(sub_obj, pro_price_id)
                    else:
                        has_price = False
                except Exception:
                    has_price = False
            if has_price is not True:
                return {
                    "handled": False,
                    "event_type": etype,
                    "reason": "price_or_status_ineligible",
                }

        store.update_user_billing(
            user["user_id"],
            plan="pro",
            stripe_customer_id=str(cust_id) if cust_id else None,
            stripe_subscription_id=str(sub_id) if sub_id else None,
            last_event_ts=created_ts if created_ts > 0 else None,
        )
        return {
            "handled": True,
            "event_type": etype,
            "user_id": user["user_id"],
            "plan": "pro",
        }

    if etype == "customer.subscription.updated":
        cust_id = obj.get("customer")
        sub_id = obj.get("id")
        status = str(obj.get("status", "")).lower()
        user = store.get_user_by_stripe_customer(str(cust_id)) if cust_id else None
        if user is None:
            return {"handled": False, "event_type": etype, "reason": "customer_not_found"}
        if created_ts > 0 and created_ts < int(user.get("stripe_last_event_ts") or 0):
            return {"handled": False, "event_type": etype, "reason": "stale_event"}
        if pro_price_id and _subscription_has_price(obj, pro_price_id) is not True:
            return {"handled": False, "event_type": etype, "reason": "price_mismatch"}

        current_sub = user.get("stripe_subscription_id")
        if (
            current_sub
            and (not sub_id or str(sub_id) != current_sub)
            and status not in ("active", "trialing")
        ):
            return {"handled": False, "event_type": etype, "reason": "unrelated_subscription"}

        if status in ("active", "trialing"):
            new_plan = "pro"
        elif status in ("canceled", "unpaid", "past_due", "incomplete_expired"):
            new_plan = "free"
        else:
            return {"handled": False, "event_type": etype, "reason": f"ignored_status:{status}"}
        store.update_user_billing(
            user["user_id"],
            plan=new_plan,
            stripe_customer_id=str(cust_id),
            stripe_subscription_id=str(sub_id) if sub_id else None,
            last_event_ts=created_ts if created_ts > 0 else None,
        )
        return {
            "handled": True,
            "event_type": etype,
            "user_id": user["user_id"],
            "plan": new_plan,
        }

    if etype == "customer.subscription.deleted":
        cust_id = obj.get("customer")
        sub_id = obj.get("id")
        user = store.get_user_by_stripe_customer(str(cust_id)) if cust_id else None
        if user is None:
            return {"handled": False, "event_type": etype, "reason": "customer_not_found"}
        if created_ts > 0 and created_ts < int(user.get("stripe_last_event_ts") or 0):
            return {"handled": False, "event_type": etype, "reason": "stale_event"}

        current_sub = user.get("stripe_subscription_id")
        if current_sub and (not sub_id or str(sub_id) != current_sub):
            return {"handled": False, "event_type": etype, "reason": "unrelated_subscription"}

        store.update_user_billing(
            user["user_id"],
            plan="free",
            stripe_customer_id=str(cust_id),
            stripe_subscription_id=None,
            last_event_ts=created_ts if created_ts > 0 else None,
        )
        return {
            "handled": True,
            "event_type": etype,
            "user_id": user["user_id"],
            "plan": "free",
        }

    return {"handled": False, "event_type": etype, "reason": "unhandled_event_type"}
