from datetime import datetime, timezone
from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ReportArtifact(db.Model):
    """Private, organization-scoped generated report retained for 30 days."""
    __tablename__ = "report_artifacts"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    report_type = db.Column(db.String(50), nullable=False)
    file_format = db.Column(db.String(10), nullable=False)
    mime_type = db.Column(db.String(120), nullable=False)
    file_size = db.Column(db.Integer, nullable=False, default=0)
    filters = db.Column(db.JSON, nullable=False, default=dict)
    file_bytes = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "report_type": self.report_type,
            "file_format": self.file_format, "mime_type": self.mime_type,
            "file_size": self.file_size, "filters": self.filters or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
