import hashlib
import hmac
import json
import os
import urllib.request
import urllib.error
import urllib.parse
import base64
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
    razorpay = {
        "RAZORPAY_KEY_ID": bool(os.getenv("RAZORPAY_KEY_ID")),
        "RAZORPAY_KEY_SECRET": bool(os.getenv("RAZORPAY_KEY_SECRET")),
        "RAZORPAY_PLAN_MONTHLY_ID": bool(os.getenv("RAZORPAY_PLAN_MONTHLY_ID")),
        "RAZORPAY_PLAN_ANNUAL_ID": bool(os.getenv("RAZORPAY_PLAN_ANNUAL_ID")),
        "RAZORPAY_WEBHOOK_SECRET": bool(os.getenv("RAZORPAY_WEBHOOK_SECRET")),
    }
    paypal = {
        "PAYPAL_CLIENT_ID": bool(os.getenv("PAYPAL_CLIENT_ID")),
        "PAYPAL_CLIENT_SECRET": bool(os.getenv("PAYPAL_CLIENT_SECRET")),
        "PAYPAL_PLAN_MONTHLY_ID": bool(os.getenv("PAYPAL_PLAN_MONTHLY_ID")),
        "PAYPAL_PLAN_ANNUAL_ID": bool(os.getenv("PAYPAL_PLAN_ANNUAL_ID")),
        "PAYPAL_WEBHOOK_ID": bool(os.getenv("PAYPAL_WEBHOOK_ID")),
    }
    def summary(provider, fields):
        return {
            "provider": provider,
            "configured_fields": [key for key, value in fields.items() if value],
            "missing_fields": [key.lower() for key, value in fields.items() if not value],
            "credentials_complete": all(fields.values()),
            "checkout_enabled": all(fields.values()) and os.getenv("BILLING_CHECKOUT_ENABLED", "false").lower() == "true",
        }
    result = summary("razorpay", razorpay)
    result["live_checkout_switch"] = os.getenv("BILLING_CHECKOUT_ENABLED", "false").lower() == "true"
    result["message"] = "Razorpay checkout requires credentials, both plan IDs, a webhook secret, sandbox verification and BILLING_CHECKOUT_ENABLED=true."
    result["paypal"] = summary("paypal", paypal)
    result["paypal"]["checkout_enabled"] = result["paypal"]["checkout_enabled"] and os.getenv("BILLING_CHECKOUT_ENABLED", "false").lower() == "true" and os.getenv("PAYPAL_CURRENCY", "USD").upper() != "INR"
    result["paypal"]["mode"] = os.getenv("PAYPAL_MODE", "sandbox").lower()
    result["paypal"]["currency"] = os.getenv("PAYPAL_CURRENCY", "USD").upper()
    result["paypal"]["message"] = "PayPal subscriptions require supported-currency plans; INR recurring subscriptions are not supported."
    return result

def _provider_http(url, method="GET", payload=None, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(1024 * 1024)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        current_app.logger.warning("Payment provider returned HTTP %s.", exc.code)
        raise ValueError("The payment provider rejected the request. Check its configuration.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        current_app.logger.warning("Payment provider request failed: %s", exc)
        raise ValueError("The payment provider is temporarily unavailable.") from exc

def _razorpay_call(path, payload=None, method="POST"):
    key_id, secret = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key_id or not secret:
        raise ValueError("Razorpay credentials are not configured on the server.")
    token = base64.b64encode((key_id + ":" + secret).encode()).decode()
    return _provider_http("https://api.razorpay.com/v1/" + path.lstrip("/"), method, payload,
                          {"Authorization": "Basic " + token})

def _paypal_base():
    return "https://api-m.sandbox.paypal.com" if os.getenv("PAYPAL_MODE", "sandbox").lower() != "live" else "https://api-m.paypal.com"

def _paypal_token():
    client = os.getenv("PAYPAL_CLIENT_ID", "")
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    if not client or not secret:
        raise ValueError("PayPal REST credentials are not configured on the server.")
    token = base64.b64encode((client + ":" + secret).encode()).decode()
    req = urllib.request.Request(
        _paypal_base() + "/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + token, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read(65536).decode())
            return data["access_token"]
    except Exception as exc:
        current_app.logger.warning("PayPal authentication failed.")
        raise ValueError("PayPal authentication failed. Check the server credentials and mode.") from exc

def _paypal_call(path, payload=None, method="POST"):
    token = _paypal_token()
    return _provider_http(_paypal_base() + path, method, payload, {
        "Authorization": "Bearer " + token,
        "Prefer": "return=representation",
    })

def _billing_profile_complete(item):
    values = (item.billing_contact_name, item.billing_email, item.billing_legal_name, item.billing_address)
    return all(str(value or "").strip() for value in values)

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
    required_details = {
        "billing_contact_name": item.billing_contact_name,
        "billing_email": item.billing_email,
        "billing_legal_name": item.billing_legal_name,
        "billing_address": item.billing_address,
    }
    missing_details = [field for field, value in required_details.items() if not str(value or "").strip()]
    if missing_details:
        return jsonify({
            "error": "Complete and save your billing contact name, billing email, legal/organisation name, and billing address before starting the 14-day trial.",
            "missing_fields": missing_details,
        }), 400
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

@billing_bp.post("/checkout/razorpay/create")
@roles_required("Admin")
def create_razorpay_checkout():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    item = _subscription(org.id)
    _expire_trial(item)
    if item.status == "active":
        return jsonify({"error": "This organisation already has an active Premium subscription."}), 409
    if not _billing_profile_complete(item):
        return jsonify({"error": "Save the required billing contact, email, legal name and billing address first."}), 400
    interval = str((request.get_json(silent=True) or {}).get("billing_interval") or "monthly").lower()
    if interval not in {"monthly", "annual"}:
        return jsonify({"error": "Choose monthly or annual billing."}), 400
    plan_id = os.getenv("RAZORPAY_PLAN_MONTHLY_ID" if interval == "monthly" else "RAZORPAY_PLAN_ANNUAL_ID", "")
    if not plan_id or not os.getenv("RAZORPAY_WEBHOOK_SECRET") or os.getenv("BILLING_CHECKOUT_ENABLED", "false").lower() != "true":
        return jsonify({"error": "Razorpay checkout is not enabled. Configure the provider, test the sandbox flow and set BILLING_CHECKOUT_ENABLED=true only when approved."}), 503
    try:
        data = _razorpay_call("subscriptions", {
            "plan_id": plan_id, "total_count": 120, "quantity": 1,
            "customer_notify": 1,
            "notes": {"organization_id": str(org.id), "organization_name": str(org.name)[:200],
                      "billing_interval": interval},
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    subscription_id = str(data.get("id") or "")
    if not subscription_id:
        return jsonify({"error": "Razorpay did not return a subscription ID."}), 502
    item.provider = "razorpay"
    item.provider_subscription_id = subscription_id
    item.billing_interval = interval
    item.plan_key = "premium"
    item.status = "pending_payment"
    item.currency = "INR"
    _audit(admin, org, "billing.razorpay_checkout_created", {
        "subscription_id": subscription_id, "billing_interval": interval,
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save Razorpay subscription.")
        return jsonify({"error": "The checkout was created but could not be linked to the organisation. Contact support before retrying."}), 500
    return jsonify({
        "provider": "razorpay", "key_id": os.getenv("RAZORPAY_KEY_ID"),
        "subscription_id": subscription_id, "name": str(org.name),
        "description": "CleanTrack Premium " + interval,
        "prefill": {"name": item.billing_contact_name, "email": item.billing_email},
        "status": "pending_payment",
    }), 201


@billing_bp.post("/checkout/razorpay/verify")
@roles_required("Admin")
def verify_razorpay_checkout():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    data = request.get_json(silent=True) or {}
    payment_id = str(data.get("razorpay_payment_id") or "").strip()
    subscription_id = str(data.get("razorpay_subscription_id") or "").strip()
    signature = str(data.get("razorpay_signature") or "").strip()
    item = OrganizationSubscription.query.filter_by(organization_id=org.id).first()
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not item or item.provider != "razorpay" or not item.provider_subscription_id or item.provider_subscription_id != subscription_id:
        return jsonify({"error": "This checkout does not match a pending subscription for your organisation."}), 400
    if not payment_id or not signature or not secret:
        return jsonify({"error": "Razorpay checkout verification details are incomplete."}), 400
    expected = hmac.new(secret.encode(), (payment_id + "|" + subscription_id).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Razorpay payment signature could not be verified."}), 400
    try:
        payment = _razorpay_call("payments/" + urllib.parse.quote(payment_id, safe=""), method="GET")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    if payment.get("status") not in {"authorized", "captured"}:
        return jsonify({"error": "Razorpay has not confirmed an authorised payment yet.", "payment_status": payment.get("status")}), 409
    if payment.get("subscription_id") and payment.get("subscription_id") != subscription_id:
        return jsonify({"error": "The verified payment is not linked to this subscription."}), 400
    _audit(admin, org, "billing.razorpay_checkout_signature_verified", {
        "subscription_id": subscription_id, "payment_id": payment_id,
        "payment_status": payment.get("status"),
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "The verified payment could not be recorded."}), 500
    return jsonify({"message": "Payment signature verified. Premium activates after provider confirmation.", "status": item.status}), 200


@billing_bp.post("/checkout/paypal/create")
@roles_required("Admin")
def create_paypal_checkout():
    admin, org = _admin_org()
    if not org:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    item = _subscription(org.id)
    _expire_trial(item)
    if item.status == "active":
        return jsonify({"error": "This organisation already has an active Premium subscription."}), 409
    if not _billing_profile_complete(item):
        return jsonify({"error": "Save the required billing contact, email, legal name and billing address first."}), 400
    interval = str((request.get_json(silent=True) or {}).get("billing_interval") or "monthly").lower()
    if interval not in {"monthly", "annual"}:
        return jsonify({"error": "Choose monthly or annual billing."}), 400
    if os.getenv("PAYPAL_CURRENCY", "USD").upper() == "INR":
        return jsonify({"error": "PayPal recurring subscriptions do not support INR. Configure a supported currency and matching PayPal plans."}), 400
    plan_id = os.getenv("PAYPAL_PLAN_MONTHLY_ID" if interval == "monthly" else "PAYPAL_PLAN_ANNUAL_ID", "")
    base_url = (os.getenv("CLEANTRACK_PUBLIC_URL") or os.getenv("FRONTEND_URL") or "").rstrip("/")
    if not plan_id or not os.getenv("PAYPAL_WEBHOOK_ID") or not base_url or os.getenv("BILLING_CHECKOUT_ENABLED", "false").lower() != "true":
        return jsonify({"error": "PayPal checkout is not enabled. Configure the provider, test the sandbox flow and set BILLING_CHECKOUT_ENABLED=true only when approved."}), 503
    try:
        result = _paypal_call("/v1/billing/subscriptions", {
            "plan_id": plan_id,
            "subscriber": {"name": {"given_name": item.billing_contact_name[:120]},
                           "email_address": item.billing_email},
            "custom_id": str(org.id),
            "application_context": {
                "brand_name": "CleanTrack",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": base_url + "/?billing=paypal-return",
                "cancel_url": base_url + "/?billing=paypal-cancelled",
            },
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    subscription_id = str(result.get("id") or "")
    approval_url = next((x.get("href") for x in result.get("links", [])
                         if x.get("rel") == "approve" and str(x.get("href", "")).startswith("https://")), None)
    if not subscription_id or not approval_url:
        return jsonify({"error": "PayPal did not return a usable subscription approval link."}), 502
    item.provider, item.provider_subscription_id = "paypal", subscription_id
    item.billing_interval, item.plan_key, item.status = interval, "premium", "pending_payment"
    item.currency = os.getenv("PAYPAL_CURRENCY", "USD").upper()
    _audit(admin, org, "billing.paypal_checkout_created", {
        "subscription_id": subscription_id, "billing_interval": interval,
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save PayPal subscription.")
        return jsonify({"error": "PayPal checkout was created but could not be linked to the organisation. Contact support before retrying."}), 500
    return jsonify({"provider": "paypal", "subscription_id": subscription_id,
                    "approval_url": approval_url, "status": "pending_payment"}), 201


@billing_bp.post("/webhook/paypal")
def paypal_webhook():
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    if not webhook_id:
        return jsonify({"error": "PayPal webhook verification is not configured."}), 503
    raw = request.get_data(cache=True)
    if len(raw) > 1024 * 1024:
        return jsonify({"error": "Webhook payload is too large."}), 413
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid webhook JSON."}), 400
    headers = {
        "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO", ""),
        "cert_url": request.headers.get("PAYPAL-CERT-URL", ""),
        "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME", ""),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    if not all(headers.get(key) for key in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")):
        return jsonify({"error": "PayPal signature headers are incomplete."}), 400
    try:
        verification = _paypal_call("/v1/notifications/verify-webhook-signature", headers)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    if verification.get("verification_status") != "SUCCESS":
        return jsonify({"error": "PayPal webhook signature could not be verified."}), 400
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    if not event_id or not event_type:
        return jsonify({"error": "PayPal event ID and type are required."}), 400
    if BillingProviderEvent.query.filter_by(provider_event_id=event_id).first():
        return jsonify({"message": "Webhook event already received.", "duplicate": True}), 200
    resource = event.get("resource") or {}
    subscription_id = str(resource.get("id") if event_type.startswith("BILLING.SUBSCRIPTION.") else
                           (resource.get("billing_agreement_id") or resource.get("subscription_id") or ""))
    item = OrganizationSubscription.query.filter_by(provider="paypal", provider_subscription_id=subscription_id).first() if subscription_id else None
    changed = False
    if item and event_type == "PAYMENT.SALE.COMPLETED":
        item.status, item.plan_key = "active", "premium"
        changed = True
    elif item and event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"}:
        item.status = "cancelled" if event_type.endswith("CANCELLED") else "expired"
        if item.status == "expired":
            item.plan_key = "free"
        item.cancel_at_period_end = False
        changed = True
    elif item and event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        item.status = "past_due"
        changed = True
    if item and changed:
        org = db.session.get(Organization, item.organization_id)
        if org:
            _audit(None, org, "billing.paypal_subscription_updated", {
                "provider_event": event_type, "subscription_status": item.status,
            })
    db.session.add(BillingProviderEvent(provider="paypal", provider_event_id=event_id, event_type=event_type))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record verified PayPal webhook.")
        return jsonify({"error": "Verified webhook event could not be recorded."}), 500
    return jsonify({"message": "Verified PayPal webhook processed.", "subscription_state_updated": changed}), 202


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
    payload_data = payload.get("payload") or {}
    sub_data = ((payload_data.get("subscription") or {}).get("entity") or {})
    invoice_data = ((payload_data.get("invoice") or {}).get("entity") or {})
    pay_data = ((payload_data.get("payment") or {}).get("entity") or {})
    sub_id = str(sub_data.get("id") or invoice_data.get("subscription_id") or pay_data.get("subscription_id") or "")
    item = OrganizationSubscription.query.filter_by(provider_subscription_id=sub_id).first() if sub_id else None
    changed = False
    if item and event_type in {"subscription.charged", "invoice.paid"}:
        item.status, item.plan_key = "active", "premium"
        changed = True
    elif item and event_type == "subscription.cancelled":
        item.status, item.cancel_at_period_end = "cancelled", False
        changed = True
    elif item and event_type == "subscription.halted":
        item.status = "past_due"
        changed = True
    if item and changed:
        org = db.session.get(Organization, item.organization_id)
        if org:
            _audit(None, org, "billing.provider_subscription_updated", {
                "provider_event": event_type, "subscription_status": item.status,
            })
    db.session.add(BillingProviderEvent(provider="razorpay", provider_event_id=event_id, event_type=event_type))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record verified Razorpay webhook.")
        return jsonify({"error": "Verified webhook event could not be recorded."}), 500
    current_app.logger.info("Verified Razorpay webhook received: %s (%s)", event_type, event_id)
    return jsonify({"message": "Verified webhook event processed.",
                    "subscription_state_updated": changed}), 202
