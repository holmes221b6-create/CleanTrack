from apscheduler.schedulers.background import BackgroundScheduler
from app.extensions import db
from app.routes.quality import process_due_inspections

def start_quality_scheduler(app):
    if app.config.get("TESTING") or app.config.get("DISABLE_QUALITY_SCHEDULER"):
        return None
    scheduler=BackgroundScheduler(timezone="UTC",daemon=True)
    def dispatch():
        with app.app_context():
            try: process_due_inspections()
            except Exception:
                db.session.rollback()
                app.logger.exception("Quality inspection scheduler dispatch failed.")
            finally: db.session.remove()
    scheduler.add_job(dispatch,trigger="interval",seconds=60,id="mechnar-quality-inspection-dispatch",
        max_instances=1,coalesce=True,replace_existing=True)
    scheduler.start()
    app.extensions["mechnar_quality_scheduler"]=scheduler
    return scheduler
