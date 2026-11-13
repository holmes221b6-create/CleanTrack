from datetime import datetime, timezone
from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class IncidentReport(db.Model):
    """Staff-originated incident/issue with a traceable Supervisor/Admin lifecycle."""
    __tablename__ = "incident_reports"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    reported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=True, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False)
    report_type = db.Column(db.String(20), nullable=False, default="issue")
    category = db.Column(db.String(40), nullable=False, default="other")
    severity = db.Column(db.String(20), nullable=False, default="medium")
    status = db.Column(db.String(30), nullable=False, default="reported", index=True)
    staff_resolution_note = db.Column(db.Text)
    staff_resolved_at = db.Column(db.DateTime(timezone=True))
    supervisor_acknowledged_at = db.Column(db.DateTime(timezone=True))
    forwarded_at = db.Column(db.DateTime(timezone=True))
    forwarded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    supervisor_note = db.Column(db.Text)
    admin_acknowledged_at = db.Column(db.DateTime(timezone=True))
    admin_acknowledged_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    admin_resolution_acknowledged_at = db.Column(db.DateTime(timezone=True))
    admin_resolution_acknowledged_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    reporter = db.relationship("User", foreign_keys=[reported_by_id])
    supervisor = db.relationship("User", foreign_keys=[supervisor_id])
    forwarder = db.relationship("User", foreign_keys=[forwarded_by_id])
    admin_acknowledger = db.relationship("User", foreign_keys=[admin_acknowledged_by_id])
    admin_resolution_acknowledger = db.relationship("User", foreign_keys=[admin_resolution_acknowledged_by_id])
    operation = db.relationship("Operation")
    location = db.relationship("Location")
    zone = db.relationship("Zone")

    def to_dict(self):
        return {
            "id": self.id,
            "reference": f"INC-{self.id:06d}",
            "title": self.title,
            "description": self.description,
            "report_type": self.report_type,
            "category": self.category,
            "severity": self.severity,
            "status": self.status,
            "staff_resolution_note": self.staff_resolution_note,
            "staff_resolved_at": self.staff_resolved_at.isoformat() if self.staff_resolved_at else None,
            "supervisor_acknowledged_at": self.supervisor_acknowledged_at.isoformat() if self.supervisor_acknowledged_at else None,
            "forwarded_at": self.forwarded_at.isoformat() if self.forwarded_at else None,
            "supervisor_note": self.supervisor_note,
            "admin_acknowledged_at": self.admin_acknowledged_at.isoformat() if self.admin_acknowledged_at else None,
            "admin_resolution_acknowledged_at": self.admin_resolution_acknowledged_at.isoformat() if self.admin_resolution_acknowledged_at else None,
            "reported_by_id": self.reported_by_id,
            "reported_by": self.reporter.full_name if self.reporter else None,
            "supervisor_id": self.supervisor_id,
            "operation_id": self.operation_id,
            "operation_title": self.operation.title if self.operation else None,
            "location_id": self.location_id,
            "location_name": self.location.name if self.location else None,
            "zone_id": self.zone_id,
            "zone_name": self.zone.name if self.zone else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class IncidentReportEvent(db.Model):
    __tablename__ = "incident_report_events"
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("incident_reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    actor = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "actor": self.actor.full_name if self.actor else "User",
            "event_type": self.event_type,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
