
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)

    recipient_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )

    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.String(1000), nullable=False)
    notification_type = db.Column(
        db.String(50),
        nullable=False,
        default="general",
    )

    resource_type = db.Column(db.String(80), nullable=True)
    resource_id = db.Column(db.String(80), nullable=True)

    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    recipient = db.relationship("User")
    organization = db.relationship("Organization")

    def to_dict(self):
        return {
            "id": self.id,
            "recipient_id": self.recipient_id,
            "organization_id": self.organization_id,
            "title": self.title,
            "message": self.message,
            "notification_type": self.notification_type,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "is_read": self.is_read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }