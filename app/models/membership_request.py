
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class MembershipRequest(db.Model):
    __tablename__ = "membership_requests"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
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

    requested_role = db.Column(
        db.String(20),
        nullable=False,
        default="Employee",
    )
    status = db.Column(
        db.String(20),
        nullable=False,
        default="pending",
        index=True,
    )

    message = db.Column(db.Text, nullable=True)
    review_message = db.Column(db.Text, nullable=True)

    reviewed_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
    )
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
    )
    organization = db.relationship("Organization")
    reviewer = db.relationship(
        "User",
        foreign_keys=[reviewed_by_id],
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "requested_role": self.requested_role,
            "status": self.status,
            "message": self.message,
            "review_message": self.review_message,
            "reviewed_by_id": self.reviewed_by_id,
            "reviewed_at": (
                self.reviewed_at.isoformat()
                if self.reviewed_at
                else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }