
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id"),
        nullable=True,
        index=True,
    )

    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    announcement_type = db.Column(
        db.String(30),
        nullable=False,
        default="announcement",
    )

    published = db.Column(db.Boolean, nullable=False, default=False)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

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

    organization = db.relationship("Organization")
    creator = db.relationship("User")
    team = db.relationship("Team")

    def to_dict(self):
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "created_by_id": self.created_by_id,
            "team_id": self.team_id,
            "title": self.title,
            "message": self.message,
            "announcement_type": self.announcement_type,
            "published": self.published,
            "published_at": (
                self.published_at.isoformat()
                if self.published_at
                else None
            ),
            "expires_at": (
                self.expires_at.isoformat()
                if self.expires_at
                else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }