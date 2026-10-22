from datetime import datetime, timezone
from app.extensions import db

def utc_now():
    return datetime.now(timezone.utc)

class OrganizationSubscription(db.Model):
    """One subscription per CleanTrack organisation."""
    __tablename__ = "organization_subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    plan_key = db.Column(db.String(30), nullable=False, default="free")
    status = db.Column(db.String(24), nullable=False, default="free", index=True)
    billing_interval = db.Column(db.String(12))
    currency = db.Column(db.String(3), nullable=False, default="INR")
    amount_paise = db.Column(db.Integer)  # intentionally unset until pricing is approved
    trial_started_at = db.Column(db.DateTime(timezone=True))
    trial_ends_at = db.Column(db.DateTime(timezone=True))
    trial_used_at = db.Column(db.DateTime(timezone=True))
    current_period_start = db.Column(db.DateTime(timezone=True))
    current_period_end = db.Column(db.DateTime(timezone=True))
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    provider = db.Column(db.String(30))
    provider_customer_id = db.Column(db.String(180))
    provider_subscription_id = db.Column(db.String(180), unique=True)
    billing_contact_name = db.Column(db.String(120))
    billing_email = db.Column(db.String(254))
    billing_legal_name = db.Column(db.String(180))
    billing_address = db.Column(db.Text)
    billing_gstin = db.Column(db.String(15))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    organization = db.relationship("Organization", backref=db.backref("subscription", uselist=False))
    def to_dict(self):
        return {
            "id": self.id, "organization_id": self.organization_id, "plan_key": self.plan_key,
            "status": self.status, "billing_interval": self.billing_interval, "currency": self.currency,
            "amount_paise": self.amount_paise,
            "trial_started_at": self.trial_started_at.isoformat() if self.trial_started_at else None,
            "trial_ends_at": self.trial_ends_at.isoformat() if self.trial_ends_at else None,
            "trial_used": self.trial_used_at is not None,
            "current_period_start": self.current_period_start.isoformat() if self.current_period_start else None,
            "current_period_end": self.current_period_end.isoformat() if self.current_period_end else None,
            "cancel_at_period_end": bool(self.cancel_at_period_end), "provider": self.provider,
            "billing_contact_name": self.billing_contact_name, "billing_email": self.billing_email,
            "billing_legal_name": self.billing_legal_name, "billing_address": self.billing_address,
            "billing_gstin": self.billing_gstin, "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

class BillingProviderEvent(db.Model):
    """Idempotency ledger for signature-verified provider webhooks; no raw payment data."""
    __tablename__ = "billing_provider_events"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(30), nullable=False, default="razorpay")
    provider_event_id = db.Column(db.String(180), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(120), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
