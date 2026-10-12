
from datetime import datetime, timezone
import secrets
import string

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def generate_organization_code():
    alphabet = string.ascii_uppercase + string.digits
    return "CT-" + "".join(secrets.choice(alphabet) for _ in range(8))


class Organization(db.Model):
    __tablename__ = "organizations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    organization_code = db.Column(
        db.String(11),
        unique=True,
        nullable=False,
        index=True,
        default=generate_organization_code,
    )

    status = db.Column(db.String(20), nullable=False, default="active")
    plan = db.Column(db.String(30), nullable=False, default="Free/Demo")

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

    users = db.relationship(
        "User",
        backref="organization",
        lazy=True,
        foreign_keys="User.organization_id",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "organization_code": self.organization_code,
            "status": self.status,
            "plan": self.plan,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }

