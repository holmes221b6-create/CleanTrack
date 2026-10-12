
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class Operation(db.Model):
    __tablename__ = "operations"

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    task_type = db.Column(db.String(100), nullable=False, default="General")

    status = db.Column(
        db.String(30),
        nullable=False,
        default="Created",
        index=True,
    )

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id"),
        nullable=True,
        index=True,
    )
    location_id = db.Column(
        db.Integer,
        db.ForeignKey("locations.id"),
        nullable=True,
        index=True,
    )
    zone_id = db.Column(
        db.Integer,
        db.ForeignKey("zones.id"),
        nullable=True,
        index=True,
    )

    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
    )
    assigned_to_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    due_at = db.Column(db.DateTime(timezone=True), nullable=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    review_feedback = db.Column(db.Text, nullable=True)
    overdue = db.Column(db.Boolean, nullable=False, default=False)

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
    team = db.relationship("Team")
    location = db.relationship("Location")
    zone = db.relationship("Zone")
    creator = db.relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    assignee = db.relationship(
        "User",
        foreign_keys=[assigned_to_id],
    )

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "status": self.status,
            "organization_id": self.organization_id,
            "team_id": self.team_id,
            "location_id": self.location_id,
            "zone_id": self.zone_id,
            "created_by_id": self.created_by_id,
            "assigned_to_id": self.assigned_to_id,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "submitted_at": (
                self.submitted_at.isoformat()
                if self.submitted_at
                else None
            ),
            "reviewed_at": (
                self.reviewed_at.isoformat()
                if self.reviewed_at
                else None
            ),
            "review_feedback": self.review_feedback,
            "overdue": self.overdue,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }