from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ZoneFloorPlan(db.Model):
    """Private, database-persisted floor-plan image for one operational zone."""
    __tablename__ = "zone_floor_plans"

    id = db.Column(db.Integer, primary_key=True)
    zone_id = db.Column(
        db.Integer,
        db.ForeignKey("zones.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(40), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    zone = db.relationship("Zone")
    uploaded_by = db.relationship("User")
