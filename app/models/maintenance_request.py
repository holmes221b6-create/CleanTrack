from datetime import datetime, timezone
from app.extensions import db

class MaintenanceRequest(db.Model):
    __tablename__ = "maintenance_requests"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True, index=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True, index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    issue_type = db.Column(db.String(60), nullable=False, default="General repair")
    description = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=False, default="Normal")
    asset_reference = db.Column(db.String(160), nullable=True)
    safety_impact = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(40), nullable=False, default="Awaiting Admin review", index=True)
    admin_note = db.Column(db.Text, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    team = db.relationship("Team")
    location = db.relationship("Location")
    zone = db.relationship("Zone")
    requester = db.relationship("User", foreign_keys=[requested_by_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])

    def to_dict(self):
        return {
            "id": self.id, "organization_id": self.organization_id,
            "team_id": self.team_id, "team_name": self.team.name if self.team else None,
            "location_id": self.location_id, "location_name": self.location.name if self.location else None,
            "zone_id": self.zone_id, "zone_name": self.zone.name if self.zone else None,
            "requested_by_id": self.requested_by_id,
            "requested_by_name": self.requester.full_name if self.requester else None,
            "title": self.title, "issue_type": self.issue_type, "description": self.description,
            "priority": self.priority, "asset_reference": self.asset_reference,
            "safety_impact": bool(self.safety_impact), "status": self.status,
            "admin_note": self.admin_note,
            "reviewed_by_name": self.reviewer.full_name if self.reviewer else None,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
