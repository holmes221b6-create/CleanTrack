from apscheduler.schedulers.background import BackgroundScheduler

from app.extensions import db
from app.models.organization import Organization
from app.routes.announcements import _process_due_schedules, _process_escalations


def start_announcement_scheduler(app):
    """Start the recurring dispatcher for scheduled announcements and alerts."""
    if app.config.get("TESTING") or app.config.get("DISABLE_ANNOUNCEMENT_SCHEDULER"):
        return None
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    def dispatch_due_items():
        with app.app_context():
            try:
                organization_ids = [
                    row[0] for row in db.session.query(Organization.id)
                    .filter(Organization.status == "active")
                    .order_by(Organization.id.asc())
                    .all()
                ]
                for organization_id in organization_ids:
                    try:
                        _process_due_schedules(organization_id)
                        _process_escalations(organization_id)
                    except Exception:
                        db.session.rollback()
                        app.logger.exception(
                            "Announcement dispatch failed for organization %s",
                            organization_id,
                        )
            finally:
                db.session.remove()

    scheduler.add_job(
        dispatch_due_items,
        trigger="interval",
        seconds=30,
        id="cleantrack-announcement-dispatch",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    app.extensions["cleantrack_announcement_scheduler"] = scheduler
    return scheduler
