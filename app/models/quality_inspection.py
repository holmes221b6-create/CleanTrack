from datetime import datetime, timezone
from app.extensions import db

def utc_now():
    return datetime.now(timezone.utc)

class TeamLeaderAssignment(db.Model):
    __tablename__ = "team_leader_assignments"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    promoted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    team = db.relationship("Team")
    user = db.relationship("User", foreign_keys=[user_id])
    promoted_by = db.relationship("User", foreign_keys=[promoted_by_id])

class InspectionTemplate(db.Model):
    __tablename__ = "inspection_templates"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(140), nullable=False)
    category = db.Column(db.String(40), nullable=False, default="general")
    description = db.Column(db.String(500), nullable=True)
    checklist = db.Column(db.JSON, nullable=False, default=list)
    is_archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    creator = db.relationship("User")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "category": self.category, "description": self.description or "",
                "checklist": self.checklist or [], "is_archived": self.is_archived,
                "created_at": self.created_at.isoformat() if self.created_at else None}

class InspectionSchedule(db.Model):
    __tablename__ = "inspection_schedules"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    template_id = db.Column(db.Integer, db.ForeignKey("inspection_templates.id"), nullable=True)
    title = db.Column(db.String(180), nullable=False)
    checklist_snapshot = db.Column(db.JSON, nullable=False, default=list)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=True)
    first_due_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    recurrence = db.Column(db.String(20), nullable=False, default="once")
    recurrence_days = db.Column(db.JSON, nullable=False, default=list)
    recurrence_day = db.Column(db.Integer, nullable=True)
    timezone_name = db.Column(db.String(80), nullable=False, default="Asia/Kolkata")
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    creator = db.relationship("User", foreign_keys=[created_by_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to_id])
    template = db.relationship("InspectionTemplate")
    location = db.relationship("Location")
    zone = db.relationship("Zone")
    operation = db.relationship("Operation")

class InspectionRecord(db.Model):
    __tablename__ = "inspection_records"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("inspection_schedules.id", ondelete="SET NULL"), nullable=True, index=True)
    legacy_source_id = db.Column(db.Integer, nullable=True, unique=True)
    template_id = db.Column(db.Integer, db.ForeignKey("inspection_templates.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    inspected_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)
    title = db.Column(db.String(180), nullable=False)
    checklist = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(24), nullable=False, default="scheduled", index=True)
    outcome = db.Column(db.String(12), nullable=True)
    findings = db.Column(db.Text, nullable=True)
    score = db.Column(db.Float, nullable=True)
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    forwarded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    admin_reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    admin_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    schedule = db.relationship("InspectionSchedule")
    creator = db.relationship("User", foreign_keys=[created_by_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to_id])
    inspector = db.relationship("User", foreign_keys=[inspected_by_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])
    location = db.relationship("Location")
    zone = db.relationship("Zone")
    operation = db.relationship("Operation")
    __table_args__ = (db.UniqueConstraint("schedule_id", "scheduled_at", name="uq_inspection_schedule_occurrence"),)

    def to_dict(self):
        return {"id": self.id, "title": self.title, "team_id": self.team_id, "schedule_id": self.schedule_id, "legacy": self.legacy_source_id is not None,
                "template_id": self.template_id, "checklist": self.checklist or [], "status": self.status,
                "outcome": self.outcome, "findings": self.findings, "score": self.score,
                "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
                "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
                "review_notes": self.review_notes, "forwarded_at": self.forwarded_at.isoformat() if self.forwarded_at else None,
                "admin_reviewed_at": self.admin_reviewed_at.isoformat() if self.admin_reviewed_at else None,
                "admin_notes": self.admin_notes, "created_by_id": self.created_by_id,
                "created_by": self.creator.full_name if self.creator else None, "assigned_to_id": self.assigned_to_id,
                "assigned_to": self.assignee.full_name if self.assignee else None,
                "inspected_by": self.inspector.full_name if self.inspector else None,
                "reviewed_by": self.reviewer.full_name if self.reviewer else None,
                "operation_id": self.operation_id, "operation_title": self.operation.title if self.operation else None,
                "location_id": self.location_id, "location_name": self.location.name if self.location else None,
                "zone_id": self.zone_id, "zone_name": self.zone.name if self.zone else None,
                "created_at": self.created_at.isoformat() if self.created_at else None}
