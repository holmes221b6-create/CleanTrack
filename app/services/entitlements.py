"""Central subscription entitlement checks for future feature gates."""
from datetime import datetime, timezone

def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value

def subscription_has_premium(subscription, now=None):
    if subscription is None:
        return False
    now = now or datetime.now(timezone.utc)
    status = str(subscription.status or "").lower()
    if status == "active":
        return True
    if status == "trialing":
        end = _aware(subscription.trial_ends_at)
        return bool(end and end > now)
    return False

def get_organization_entitlements(organization):
    subscription = getattr(organization, "subscription", None) if organization else None
    premium = subscription_has_premium(subscription)
    return {
        "plan": "premium" if premium else "free",
        "premium_access": premium,
        "features": {
            "core_operations": True, "locations_and_zones": True, "standard_reports": True,
            "advanced_reports": premium, "extended_audit_history": premium,
            "integrations": premium, "priority_support": premium,
        },
        "enforcement_status": "foundation_only",
    }
