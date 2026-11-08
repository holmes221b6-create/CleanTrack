from datetime import datetime, timezone
from app.extensions import db

def utc_now():
    return datetime.now(timezone.utc)

class PersonnelConversation(db.Model):
    __tablename__ = "personnel_conversations"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    supervisor_cleared_at = db.Column(db.DateTime(timezone=True), nullable=True)
    staff_cleared_at = db.Column(db.DateTime(timezone=True), nullable=True)
    supervisor_deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    staff_deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    __table_args__ = (db.UniqueConstraint("organization_id", "supervisor_id", "staff_id", name="uq_personnel_conversation_pair"),)

class PersonnelMessage(db.Model):
    __tablename__ = "personnel_messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("personnel_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    sender = db.relationship("User", foreign_keys=[sender_id])
