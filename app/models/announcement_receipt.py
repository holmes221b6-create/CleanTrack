from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class AnnouncementReceipt(db.Model):
    __tablename__ = "announcement_receipts"

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)
    acknowledged_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    announcement = db.relationship("Announcement", backref=db.backref("receipts", cascade="all, delete-orphan", lazy="selectin"))
    user = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_receipt_user"),)

    def to_dict(self):
        return {
            "id": self.id,
            "announcement_id": self.announcement_id,
            "user_id": self.user_id,
            "user_name": self.user.full_name if self.user else "Unknown",
            "email": self.user.email if self.user else None,
            "role": self.user.role if self.user else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "state": "acknowledged" if self.acknowledged_at else "read" if self.read_at else "unread",
        }
