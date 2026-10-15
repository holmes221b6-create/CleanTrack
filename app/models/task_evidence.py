from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class TaskEvidence(db.Model):
    __tablename__ = "task_evidence"

    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=False, index=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    evidence_type = db.Column(db.String(30), nullable=False, default="document")
    stage = db.Column(db.String(20), nullable=False, default="execution")
    original_filename = db.Column(db.String(255), nullable=False)
    storage_key = db.Column(db.String(500), nullable=False, unique=True)
    content_type = db.Column(db.String(120), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    # Bytes live in the persistent database, never in a deploy's ephemeral filesystem.
    file_data = db.Column(db.LargeBinary, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    operation = db.relationship("Operation", backref=db.backref("evidence", lazy=True))
    uploader = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "operation_id": self.operation_id,
            "uploaded_by_id": self.uploaded_by_id,
            "uploaded_by_name": self.uploader.full_name if self.uploader else None,
            "evidence_type": self.evidence_type,
            "stage": self.stage,
            "original_filename": self.original_filename,
            "content_type": self.content_type,
            "file_size": self.file_size,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
