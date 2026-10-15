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
    priority = db.Column(db.String(20), nullable=False, default="Normal")
    instructions = db.Column(db.Text, nullable=True)
    checklist = db.Column(db.JSON, nullable=True)
    required_evidence = db.Column(db.JSON, nullable=True)
    estimated_minutes = db.Column(db.Integer, nullable=True)
    requires_verification = db.Column(db.Boolean, nullable=False, default=True)

    status = db.Column(db.String(30), nullable=False, default="Pending", index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True, index=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    original_assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    assigned_supervisor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    due_at = db.Column(db.DateTime(timezone=True), nullable=True)
    original_due_at = db.Column(db.DateTime(timezone=True), nullable=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    review_feedback = db.Column(db.Text, nullable=True)
    overdue = db.Column(db.Boolean, nullable=False, default=False)

    # Recurrence preferences are stored independently from the execution log.
    # The actual occurrence scheduler must process these definitions before
    # these settings can create work automatically.
    schedule_type = db.Column(db.String(20), nullable=False, default="one_time")
    recurrence_rule = db.Column(db.JSON, nullable=True)
    schedule_timezone = db.Column(db.String(80), nullable=False, default="UTC")
    occurrence_key = db.Column(db.String(180), nullable=True, unique=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    organization = db.relationship("Organization")
    team = db.relationship("Team")
    location = db.relationship("Location")
    zone = db.relationship("Zone")
    creator = db.relationship("User", foreign_keys=[created_by_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to_id])
    original_assignee = db.relationship("User", foreign_keys=[original_assigned_to_id])
    assigned_supervisor = db.relationship("User", foreign_keys=[assigned_supervisor_id])

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "category": self.task_type,
            "priority": self.priority or "Normal",
            "instructions": self.instructions,
            "checklist": self.checklist if isinstance(self.checklist, list) else [],
            "required_evidence": self.required_evidence if isinstance(self.required_evidence, list) else [],
            "estimated_minutes": self.estimated_minutes,
            "requires_verification": bool(self.requires_verification),
            "status": self.status,
            "organization_id": self.organization_id,
            "team_id": self.team_id,
            "location_id": self.location_id,
            "zone_id": self.zone_id,
            "created_by_id": self.created_by_id,
            "created_by_name": self.creator.full_name if self.creator else None,
            "assigned_to_id": self.assigned_to_id,
            "assigned_to_name": self.assignee.full_name if self.assignee else None,
            "assigned_to_role": self.assignee.role if self.assignee else None,
            "original_assigned_to_id": self.original_assigned_to_id,
            "original_assigned_to_name": self.original_assignee.full_name if self.original_assignee else None,
            "assigned_supervisor_id": self.assigned_supervisor_id,
            "assigned_supervisor_name": self.assigned_supervisor.full_name if self.assigned_supervisor else None,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "original_due_at": self.original_due_at.isoformat() if self.original_due_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_feedback": self.review_feedback,
            "overdue": bool(self.overdue),
            "schedule_type": self.schedule_type or "one_time",
            "recurrence_rule": self.recurrence_rule,
            "schedule_timezone": self.schedule_timezone or "UTC",
            "occurrence_key": self.occurrence_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
