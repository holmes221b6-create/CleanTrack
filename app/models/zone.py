from datetime import datetime, timezone
from uuid import uuid4

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class Zone(db.Model):
    __tablename__ = "zones"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    floor_label = db.Column(db.String(120), nullable=True)
    # GeoJSON Polygon geometry in longitude/latitude coordinates.
    # A boundary is optional; no placeholder geometry is created.
    boundary = db.Column(db.JSON, nullable=True)
    # An opaque identifier for a QR deep link. It identifies a zone but grants no access.
    qr_token = db.Column(db.String(64), nullable=False, unique=True, default=lambda: uuid4().hex)
    qr_enabled = db.Column(db.Boolean, nullable=False, default=True)
    qr_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    location_id = db.Column(
        db.Integer,
        db.ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id"),
        nullable=True,
        index=True,
    )
    supervisor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    status = db.Column(db.String(20), nullable=False, default="active")

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
    location = db.relationship("Location")
    team = db.relationship("Team")
    supervisor = db.relationship("User", foreign_keys=[supervisor_id])

    __table_args__ = (
        db.UniqueConstraint(
            "location_id",
            "name",
            name="uq_zone_location_name",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "floor_label": self.floor_label,
            "boundary": self.boundary,
            "qr_token": self.qr_token,
            "qr_enabled": bool(self.qr_enabled),
            "qr_expires_at": self.qr_expires_at.isoformat() if self.qr_expires_at else None,
            "organization_id": self.organization_id,
            "location_id": self.location_id,
            "location_name": self.location.name if self.location else None,
            "location_status": self.location.status if self.location else None,
            "location_team_id": self.location.team_id if self.location else None,
            "team_id": self.team_id,
            "team_name": self.team.name if self.team else None,
            "supervisor_id": self.supervisor_id,
            "supervisor_name": self.supervisor.full_name if self.supervisor else None,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
