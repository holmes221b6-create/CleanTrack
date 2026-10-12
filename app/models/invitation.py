
from datetime import datetime, timezone
import secrets

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def generate_invitation_token():
    return secrets.token_urlsafe(32)


class Invitation(db.Model):
    __tablename__ = "invitations"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    invited_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    email = db.Column(db.String(254), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default="Employee")

    # Store only a hash of the secret token, never the raw token.
    token_hash = db.Column(db.String(64), nullable=False, unique=True)

    status = db.Column(db.String(20), nullable=False, default="pending")
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    organization = db.relationship("Organization")
    inviter = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "invited_by_id": self.invited_by_id,
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "expires_at": self.expires_at.isoformat(),
            "accepted_at": (
                self.accepted_at.isoformat()
                if self.accepted_at
                else None
            ),
            "created_at": self.created_at.isoformat(),
        }