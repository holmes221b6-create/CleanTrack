import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.subscription import BillingProviderEvent, OrganizationSubscription
from app.services.entitlements import get_organization_entitlements
from app.services.permissions import get_current_user, roles_required

billing_bp = Blueprint("billing", __name__, url_prefix="/api/billing")

def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value

def _provider_readiness():
    required = {
        "RAZORPAY_KEY_ID": bool(os.getenv("RAZORPAY_KEY_ID")),
        "RAZORPAY_KEY_SECRET": bool(os.getenv("RAZORPAY_KEY_SECRET")),
        "RAZORPAY_PLAN_MONTHLY_ID": bool(os.getenv("RAZORPAY_PLAN_MONTHLY_ID")),
        "RAZORPAY_PLAN_ANNUAL_ID": bool(os.getenv("RAZORPAY_PLAN_ANNUAL_ID")),
        "RAZORPAY_WEBHOOK_SECRET": bool(os.getenv("RAZORPAY_WEBHOOK_SECRET")),
    }
    return {
        "provider": "razorpay", "configured_fields": [k for k,v in required.items() if v],
        "missing_fields": [k.lower() for k,v in required.items() if not v],
        "credentials_complete": all(required.values()), "checkout_enabled": False,
        "message": "Checkout stays disabled until credentials, plans, webhook lifecycle mapping, and test transactions are verified.",
    }

def _subscription(org_id):
    item = OrganizationSubscription.query.filter_by(organization_id=org_id).first()
    if item is None:
        item = OrganizationSubscription(organization_id=org_id, plan_key="free", status="free", currency="INR")
        db.session.add(item)
        db.session.flush()
    return item

def _expire_trial(item):
    if item.status == "trialing" and item.trial_ends_at and _aware(item.trial_ends_at) <= datetime.now(timezone.utc):
        item.status = "expired"
        item.plan_key = "free"
        item.cancel_at_period_end = False
        return True
    return False

def _audit(actor, org, action, details):
    db.session.add(AuditLog(actor_id=actor.id if actor else None, organization_id=org.id,
        action=action, resource_type="OrganizationSubscription", resource_id=str(org.id),
        details=details, ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None)))

def _admin_org():
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return admin, None
    return admin, db.session.get(Organization, admin.organization_id)

@billing_bp.get("/current")
@roles_required("Admin")
def current_billing():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    item = _subscription(org.id)
    if _expire_trial(item):
        _audit(None, org, "billing.trial_expired", {"subscription_status": "expired"})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not load subscription state.")
        return jsonify({"error": "Billing information could not be loaded."}), 500
    return jsonify({
        "organization": {"id": org.id, "name": org.name}, "subscription": item.to_dict(),
        "entitlements": get_organization_entitlements(org), "provider": _provider_readiness(),
        "pricing": {"currency": "INR", "monthly_price_paise": None, "annual_price_paise": None,
                    "status": "not_set", "message": "Prices are not set; this page will not charge an amount."},
        "billing_history": [], "billing_history_status": "provider_invoices_not_connected",
    }), 200

@billing_bp.post("/trial/start")
@roles_required("Admin")
def start_trial():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    data = request.get_json(silent=True) or {}
    interval = str(data.get("billing_interval") or "monthly").strip().lower()
    if interval not in {"monthly", "annual"}:
        return jsonify({"error": "Choose monthly or annual billing."}), 400
    item = _subscription(org.id)
    _expire_trial(item)
    if item.status == "active":
        return jsonify({"error": "Your organization already has an active Premium subscription."}), 409
    if item.trial_used_at is not None:
        return jsonify({"error": "This organization has already used its 14-day Premium trial."}), 409
    if item.status == "trialing":
        return jsonify({"error": "Your organization's Premium trial is already active.", "subscription": item.to_dict()}), 409
    now = datetime.now(timezone.utc)
    item.plan_key, item.status, item.billing_interval = "premium", "trialing", interval
    item.trial_started_at, item.trial_ends_at, item.trial_used_at = now, now + timedelta(days=14), now
    item.amount_paise, item.provider = None, None
    _audit(admin, org, "billing.premium_trial_started", {
        "trial_days": 14, "billing_interval": interval,
        "trial_started_at": now.isoformat(), "trial_ends_at": item.trial_ends_at.isoformat(),
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not start Premium trial.")
        return jsonify({"error": "The Premium trial could not be started. Please try again."}), 500
    return jsonify({"message": "Your 14-day Premium trial has started.",
                    "subscription": item.to_dict(), "entitlements": get_organization_entitlements(org)}), 201

@billing_bp.patch("/billing-details")
@roles_required("Admin")
def update_billing_details():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    data = request.get_json(silent=True) or {}
    limits = {"billing_contact_name":120, "billing_email":254, "billing_legal_name":180,
              "billing_address":3000, "billing_gstin":15}
    item = _subscription(org.id)
    updated = []
    for field, limit in limits.items():
        if field not in data:
            continue
        value = data[field]
        if value is not None and not isinstance(value, str):
            return jsonify({"error": field + " must be text."}), 400
        value = (value or "").strip()
        if len(value) > limit:
            return jsonify({"error": field + " exceeds its maximum length."}), 400
        if field == "billing_email" and value and ("@" not in value or "." not in value.rsplit("@",1)[-1]):
            return jsonify({"error": "Enter a valid billing email address."}), 400
        if field == "billing_gstin" and value and (len(value) != 15 or not value.isalnum()):
            return jsonify({"error": "GSTIN must contain 15 letters and numbers."}), 400
        setattr(item, field, value or None)
        updated.append(field)
    _audit(admin, org, "billing.details_updated", {"fields_updated": updated})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Billing details could not be saved."}), 500
    return jsonify({"message": "Billing details saved.", "subscription": item.to_dict()}), 200

@billing_bp.post("/webhook/razorpay")
def razorpay_webhook():
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "Razorpay webhook verification is not configured."}), 503
    raw = request.get_data(cache=True)
    if len(raw) > 1024 * 1024:
        return jsonify({"error": "Webhook payload is too large."}), 413
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return jsonify({"error": "Invalid Razorpay webhook signature."}), 400
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid webhook JSON."}), 400
    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip()
    event_type = str(payload.get("event") or "").strip()
    if not event_id or not event_type or len(event_id) > 180 or len(event_type) > 120:
        return jsonify({"error": "Webhook event ID and valid event type are required."}), 400
    if BillingProviderEvent.query.filter_by(provider_event_id=event_id).first():
        return jsonify({"message": "Webhook event already received.", "duplicate": True}), 200
    db.session.add(BillingProviderEvent(provider="razorpay", provider_event_id=event_id, event_type=event_type))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record verified Razorpay webhook.")
        return jsonify({"error": "Verified webhook event could not be recorded."}), 500
    current_app.logger.info("Verified Razorpay webhook received: %s (%s)", event_type, event_id)
    return jsonify({"message": "Signature verified and event recorded; subscription state was not changed.",
                    "subscription_state_updated": False}), 202
