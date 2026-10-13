
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=True)

    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(20), nullable=False, default="Employee")
    account_status = db.Column(
        db.String(20),
        nullable=False,
        default="pending",
    )

    email_verified = db.Column(db.Boolean, nullable=False, default=False)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=True,
        index=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    termination_requested_at = db.Column(db.DateTime(timezone=True), nullable=True)
    termination_effective_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name,
            "phone": self.phone,
            "role": self.role,
            "account_status": self.account_status,
            "email_verified": self.email_verified,
            "organization_id": self.organization_id,
            "organization_name": (
                self.organization.name
                if self.organization is not None
                else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
            "termination_requested_at": (
                self.termination_requested_at.isoformat()
                if self.termination_requested_at else None
            ),
            "termination_effective_at": (
                self.termination_effective_at.isoformat()
                if self.termination_effective_at else None
            ),
        }
