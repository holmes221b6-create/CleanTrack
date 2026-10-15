from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class OperationExecution(db.Model):
    """A single real attempt to carry out an operation, separate from its definition."""

    __tablename__ = "operation_executions"

    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=False, index=True)
    performed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    attempt_number = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(30), nullable=False, default="In Progress", index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completion_notes = db.Column(db.Text, nullable=True)
    checklist_results = db.Column(db.JSON, nullable=False, default=list)
    verification_required = db.Column(db.Boolean, nullable=False, default=True)
    verified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    review_feedback = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    operation = db.relationship("Operation", backref=db.backref(
        "executions", lazy="select", order_by="OperationExecution.attempt_number"
    ))
    performer = db.relationship("User", foreign_keys=[performed_by_id])
    verifier = db.relationship("User", foreign_keys=[verified_by_id])

    __table_args__ = (
        db.UniqueConstraint("operation_id", "attempt_number", name="uq_operation_execution_attempt"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "operation_id": self.operation_id,
            "performed_by_id": self.performed_by_id,
            "performed_by_name": self.performer.full_name if self.performer else None,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "completion_notes": self.completion_notes,
            "checklist_results": self.checklist_results if isinstance(self.checklist_results, list) else [],
            "verification_required": bool(self.verification_required),
            "verified_by_id": self.verified_by_id,
            "verified_by_name": self.verifier.full_name if self.verifier else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "review_feedback": self.review_feedback,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
