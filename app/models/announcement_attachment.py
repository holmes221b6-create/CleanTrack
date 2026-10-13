from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class AnnouncementAttachment(db.Model):
    __tablename__ = "announcement_attachments"

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(20), nullable=False, default="file")
    size = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    announcement = db.relationship("Announcement", backref=db.backref("attachments", cascade="all, delete-orphan", lazy="selectin"))
    uploaded_by = db.relationship("User")
