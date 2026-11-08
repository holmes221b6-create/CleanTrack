from datetime import datetime, timezone

from app.extensions import db


class ProfileQualification(db.Model):
    __tablename__ = "profile_qualifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    issuer = db.Column(db.String(180), nullable=True)
    issue_date = db.Column(db.String(20), nullable=True)
    file_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    file_data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
