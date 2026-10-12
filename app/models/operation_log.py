
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class OperationLog(db.Model):
    __tablename__ = "operation_logs"

    id = db.Column(db.Integer, primary_key=True)

    operation_id = db.Column(
        db.Integer,
        db.ForeignKey("operations.id"),
        nullable=False,
        index=True,
    )

    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    action = db.Column(db.String(50), nullable=False)
    previous_status = db.Column(db.String(30), nullable=True)
    new_status = db.Column(db.String(30), nullable=True)
    message = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    operation = db.relationship(
        "Operation",
        backref=db.backref(
            "history",
            lazy="select",
            order_by="OperationLog.created_at",
        ),
    )

    actor = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "operation_id": self.operation_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "message": self.message,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }