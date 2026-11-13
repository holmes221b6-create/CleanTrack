from datetime import datetime, timezone
from app.extensions import db

def utc_now():
    return datetime.now(timezone.utc)

class Meeting(db.Model):
    __tablename__ = "meetings"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    zoom_meeting_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    topic = db.Column(db.String(200), nullable=False)
    agenda = db.Column(db.Text)
    start_at = db.Column(db.DateTime(timezone=True), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=40)
    status = db.Column(db.String(20), nullable=False, default="scheduled", index=True)
    join_url = db.Column(db.String(1000), nullable=False)
    start_url = db.Column(db.Text)
    password = db.Column(db.String(100))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at = db.Column(db.DateTime(timezone=True))
    team = db.relationship("Team")
    creator = db.relationship("User", foreign_keys=[created_by_id])
