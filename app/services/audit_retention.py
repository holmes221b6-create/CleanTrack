from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.audit_log import AuditLog


AUDIT_RETENTION_DAYS = 7


def audit_cutoff(now=None):
    return (now or datetime.now(timezone.utc)) - timedelta(days=AUDIT_RETENTION_DAYS)


def purge_expired_audit_logs():
    """Permanently remove audit rows older than seven days."""
    try:
        deleted = AuditLog.query.filter(
            AuditLog.created_at < audit_cutoff()
        ).delete(synchronize_session=False)
        if deleted:
            db.session.commit()
        return deleted
    except Exception:
        db.session.rollback()
        raise
