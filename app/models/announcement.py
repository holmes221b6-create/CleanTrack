from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True, index=True)
    target_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True, index=True)
    target_zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True, index=True)

    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    announcement_type = db.Column(db.String(30), nullable=False, default="announcement")
    audience_scope = db.Column(db.String(20), nullable=False, default="organization")
    severity = db.Column(db.String(16), nullable=False, default="info")
    status = db.Column(db.String(20), nullable=False, default="draft")
    alert_status = db.Column(db.String(20), nullable=False, default="open")
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    requires_acknowledgement = db.Column(db.Boolean, nullable=False, default=False)
    escalate_to_admin = db.Column(db.Boolean, nullable=False, default=False)
    escalation_after_minutes = db.Column(db.Integer, nullable=True)
    escalated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    published = db.Column(db.Boolean, nullable=False, default=False, index=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    organization = db.relationship("Organization")
    creator = db.relationship("User", foreign_keys=[created_by_id])
    resolver = db.relationship("User", foreign_keys=[resolved_by_id])
    team = db.relationship("Team")
    target_location = db.relationship("Location", foreign_keys=[target_location_id])
    target_zone = db.relationship("Zone", foreign_keys=[target_zone_id])

    def to_dict(self, include_receipts=False):
        now = utc_now()
        effective_status = self.status or ("published" if self.published else "draft")
        scheduled = self.scheduled_at
        if scheduled is not None and scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if effective_status == "scheduled" and scheduled and scheduled <= now:
            effective_status = "published"
        attachments = [{
            "id": item.id,
            "filename": item.filename,
            "content_type": item.content_type,
            "size": item.size,
            "category": item.category,
            "download_url": f"/api/announcements/attachments/{item.id}/download",
            "created_at": item.created_at.isoformat() if item.created_at else None,
        } for item in sorted(self.attachments, key=lambda row: row.created_at or now)]
        payload = {
            "id": self.id,
            "organization_id": self.organization_id,
            "created_by_id": self.created_by_id,
            "created_by_name": self.creator.full_name if self.creator else "Unknown",
            "team_id": self.team_id,
            "team_name": self.team.name if self.team else None,
            "target_location_id": self.target_location_id,
            "location_name": self.target_location.name if self.target_location else None,
            "target_zone_id": self.target_zone_id,
            "zone_name": self.target_zone.name if self.target_zone else None,
            "title": self.title,
            "message": self.message,
            "announcement_type": self.announcement_type,
            "audience_scope": self.audience_scope,
            "severity": self.severity,
            "status": effective_status,
            "alert_status": self.alert_status,
            "published": effective_status == "published",
            "is_pinned": self.is_pinned,
            "requires_acknowledgement": self.requires_acknowledgement,
            "escalate_to_admin": self.escalate_to_admin,
            "escalation_after_minutes": self.escalation_after_minutes,
            "escalated_at": self.escalated_at.isoformat() if self.escalated_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by_id": self.resolved_by_id,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "attachments": attachments,
        }
        if include_receipts:
            receipts = list(self.receipts)
            payload["receipt_summary"] = {
                "recipient_count": len(receipts),
                "read_count": sum(1 for row in receipts if row.read_at is not None),
                "acknowledged_count": sum(1 for row in receipts if row.acknowledged_at is not None),
            }
        return payload
