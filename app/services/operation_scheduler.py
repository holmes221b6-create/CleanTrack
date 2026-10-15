from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler

from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.operation import Operation
from app.models.operation_log import OperationLog
from app.models.organization import Organization
from app.models.user import User


CLOSED = {"completed", "approved", "verified", "cancelled", "canceled"}
DEFAULT_CATCH_UP_DAYS = 7
MAX_CATCH_UP_OCCURRENCES = 10
MAX_SLOT_ITERATIONS = 10000


def as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def next_occurrence(previous_due, recurrence_rule, timezone_name):
    """Return the next wall-clock occurrence, preserving local time across DST."""
    if previous_due is None:
        return None
    zone = ZoneInfo(timezone_name or "UTC")
    local = as_utc(previous_due).astimezone(zone)
    frequency = (recurrence_rule or {}).get("frequency", "weekly")
    try:
        interval = int((recurrence_rule or {}).get("interval", 1))
    except (ValueError, TypeError):
        interval = 1
    interval = max(1, min(interval, 365))
    if frequency == "daily":
        result = local + timedelta(days=interval)
    elif frequency == "weekly":
        result = local + timedelta(weeks=interval)
    elif frequency == "monthly":
        month_index = (local.year * 12 + local.month - 1) + interval
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        day = min(local.day, monthrange(year, month)[1])
        result = local.replace(year=year, month=month, day=day)
    else:
        raise ValueError("Unsupported recurrence frequency.")
    return result.astimezone(timezone.utc)


def _system_event(operation, action, message, details=None, previous_status=None):
    # OperationLog requires an existing actor FK. Attribute scheduler events to
    # the operation creator there, but explicitly mark the audit actor as system.
    db.session.add(OperationLog(
        operation_id=operation.id,
        actor_id=operation.created_by_id,
        action=action,
        previous_status=previous_status or operation.status,
        new_status=operation.status,
        message=message,
    ))
    payload = {
        "actor_type": "system",
        "title": operation.title,
        "message": message,
        "previous_status": previous_status or operation.status,
        "new_status": operation.status,
    }
    if details:
        payload.update(details)
    db.session.add(AuditLog(
        actor_id=None,
        organization_id=operation.organization_id,
        action="operation." + action,
        resource_type="Operation",
        resource_id=str(operation.id),
        details=payload,
    ))


def _notify(organization_id, operation, recipients, title, message, notification_type):
    recipient_ids = set()
    for user in recipients:
        if user and user.account_status == "active" and user.organization_id == organization_id:
            recipient_ids.add(user.id)
    for recipient_id in recipient_ids:
        db.session.add(Notification(
            recipient_id=recipient_id,
            organization_id=organization_id,
            title=title[:160],
            message=message[:1000],
            notification_type=notification_type,
            resource_type="Operation",
            resource_id=str(operation.id),
        ))
    return bool(recipient_ids)


def _operation_recipients(operation):
    ids = {value for value in (operation.assigned_to_id, operation.assigned_supervisor_id) if value}
    if not ids:
        return []
    return User.query.filter(
        User.id.in_(ids),
        User.organization_id == operation.organization_id,
        User.account_status == "active",
    ).all()


def _slot_key(series_id, due_at, timezone_name):
    zone = ZoneInfo(timezone_name or "UTC")
    local = as_utc(due_at).astimezone(zone)
    key = "series:" + str(series_id) + ":" + local.strftime("%Y%m%dT%H%M%S") + ":" + (timezone_name or "UTC")
    return key[:180]


def _new_occurrence(series, slot, key):
    operation = Operation(
        title=series.title,
        description=series.description,
        task_type=series.task_type,
        priority=series.priority,
        instructions=series.instructions,
        checklist=list(series.checklist or []),
        required_evidence=list(series.required_evidence or []),
        estimated_minutes=series.estimated_minutes,
        requires_verification=series.requires_verification,
        status="Assigned" if series.assigned_to_id else "Pending",
        organization_id=series.organization_id,
        team_id=series.team_id,
        location_id=series.location_id,
        zone_id=series.zone_id,
        created_by_id=series.created_by_id,
        assigned_to_id=series.assigned_to_id,
        original_assigned_to_id=series.original_assigned_to_id or series.assigned_to_id,
        assigned_supervisor_id=series.assigned_supervisor_id,
        due_at=slot,
        original_due_at=slot,
        schedule_type="one_time",
        recurrence_rule=None,
        schedule_timezone=series.schedule_timezone or "UTC",
        schedule_series_id=series.id,
        occurrence_key=key,
        reminder_before_minutes=series.reminder_before_minutes,
        escalate_after_minutes=series.escalate_after_minutes,
        escalate_to_admin=series.escalate_to_admin,
    )
    db.session.add(operation)
    db.session.flush()
    _system_event(
        operation,
        "schedule_generated",
        "System generated this scheduled occurrence.",
        {
            "series_id": series.id,
            "occurrence_key": key,
            "scheduled_for": as_utc(slot).isoformat(),
            "assignee_id": operation.assigned_to_id,
        },
        previous_status=None,
    )
    return operation


def _process_one_series(series, now):
    # Serialize workers through the persisted series row so multiple web workers
    # cannot create the same slot in parallel. The unique occurrence key is a
    # second line of defence.
    series = Operation.query.filter_by(id=series.id).populate_existing().with_for_update().first()
    if series is None or series.schedule_next_run_at is None or as_utc(series.schedule_next_run_at) > now:
        return
    rule = series.recurrence_rule or {}
    zone_name = series.schedule_timezone or "UTC"
    try:
        ZoneInfo(zone_name)
        next_at = as_utc(series.schedule_next_run_at)
        if next_at is None:
            return
        slots = []
        cursor = next_at
        while cursor <= now and len(slots) < MAX_SLOT_ITERATIONS:
            slots.append(cursor)
            cursor = next_occurrence(cursor, rule, zone_name)
        if not slots:
            return
        if cursor <= now:
            # Extreme outage or malformed interval: do not flood old work.
            cursor = next_occurrence(now, rule, zone_name)
            _system_event(series, "schedule_catch_up_capped",
                          "Catch-up iteration limit reached; older missed occurrences were skipped.",
                          {"series_id": series.id, "skipped_count": len(slots)})
            slots = slots[-MAX_CATCH_UP_OCCURRENCES:]

        policy = str(rule.get("catch_up_policy", "latest_only")).lower()
        try:
            window_days = max(0, min(365, int(rule.get("catch_up_window_days", DEFAULT_CATCH_UP_DAYS))))
        except (TypeError, ValueError):
            window_days = DEFAULT_CATCH_UP_DAYS
        if policy not in {"latest_only", "skip", "limited"}:
            policy = "latest_only"

        if policy == "skip":
            selected = []
        elif policy == "limited":
            selected = slots[-MAX_CATCH_UP_OCCURRENCES:]
        else:
            selected = [slots[-1]] if slots[-1] >= now - timedelta(days=window_days) else []

        skipped_count = len(slots) - len(selected)
        for slot in selected:
            key = _slot_key(series.id, slot, zone_name)
            existing = Operation.query.filter_by(occurrence_key=key).first()
            if existing:
                continue
            _new_occurrence(series, slot, key)

        if skipped_count:
            _system_event(
                series,
                "schedule_occurrences_skipped",
                "Missed scheduled occurrences were skipped under the configured catch-up policy.",
                {"series_id": series.id, "skipped_count": skipped_count,
                 "catch_up_policy": policy, "catch_up_window_days": window_days},
            )
        series.schedule_next_run_at = cursor
        db.session.commit()
    except (ZoneInfoNotFoundError, ValueError) as error:
        db.session.rollback()
        # Invalid time zone/rule must not create duplicate occurrences or loop
        # forever. Stop this series and leave an audit message for an Admin.
        series = db.session.get(Operation, series.id)
        if series:
            series.schedule_next_run_at = None
            _system_event(series, "schedule_failed",
                          "Recurring schedule stopped because its time zone or recurrence rule is invalid.",
                          {"error": str(error), "schedule_timezone": series.schedule_timezone})
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def _process_schedules(organization_id, now):
    # Upgrade legacy recurring definitions created before the dispatcher was
    # installed by deriving their first future slot from the stored due date.
    legacy_series = Operation.query.filter(
        Operation.organization_id == organization_id,
        Operation.schedule_type == "recurring",
        Operation.schedule_series_id.is_(None),
        Operation.schedule_next_run_at.is_(None),
        Operation.due_at.isnot(None),
        Operation.recurrence_rule.isnot(None),
    ).order_by(Operation.id.asc()).all()
    for candidate in legacy_series:
        try:
            series = Operation.query.filter_by(id=candidate.id).populate_existing().with_for_update().first()
            if series is None or series.schedule_next_run_at is not None:
                continue
            series.schedule_next_run_at = next_occurrence(
                series.due_at, series.recurrence_rule or {}, series.schedule_timezone or "UTC"
            )
            _system_event(series, "schedule_activated",
                          "Recurring schedule dispatcher activated this existing definition.",
                          {"schedule_next_run_at": series.schedule_next_run_at.isoformat() if series.schedule_next_run_at else None})
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    series_rows = Operation.query.filter(
        Operation.organization_id == organization_id,
        Operation.schedule_type == "recurring",
        Operation.schedule_series_id.is_(None),
        Operation.schedule_next_run_at.isnot(None),
        Operation.schedule_next_run_at <= now,
    ).order_by(Operation.schedule_next_run_at.asc(), Operation.id.asc()).all()
    for series in series_rows:
        try:
            _process_one_series(series, now)
        except Exception:
            db.session.rollback()
            raise


def _process_reminders(organization_id, now):
    candidates = Operation.query.filter(
        Operation.organization_id == organization_id,
        Operation.due_at.isnot(None),
    ).all()
    for candidate in candidates:
        operation = Operation.query.filter_by(id=candidate.id).populate_existing().with_for_update().first()
        if operation is None:
            continue
        status = str(operation.status or "").strip().lower()
        if status in CLOSED or status == "submitted":
            if operation.overdue:
                operation.overdue = False
            continue
        due_at = as_utc(operation.due_at)
        if due_at is None:
            continue
        if due_at <= now:
            operation.overdue = True
        elif operation.overdue:
            operation.overdue = False
        recipients = _operation_recipients(operation)
        if not recipients:
            continue

        try:
            before_minutes = max(0, min(10080, int(operation.reminder_before_minutes or 0)))
        except (TypeError, ValueError):
            before_minutes = 60
        try:
            escalate_minutes = max(0, min(525600, int(operation.escalate_after_minutes if operation.escalate_after_minutes is not None else 120)))
        except (TypeError, ValueError):
            escalate_minutes = 120

        if due_at > now and now >= due_at - timedelta(minutes=before_minutes) and operation.reminder_before_sent_at is None:
            sent = _notify(
                organization_id, operation, recipients,
                "Operation deadline approaching",
                "'" + operation.title + "' is due at " + due_at.isoformat() + ". Review the assigned work before its deadline.",
                "operation_reminder",
            )
            if sent:
                operation.reminder_before_sent_at = now

        if due_at <= now:
            if operation.reminder_after_sent_at is None:
                sent = _notify(
                    organization_id, operation, recipients,
                    "Operation overdue",
                    "'" + operation.title + "' passed its deadline at " + due_at.isoformat() + ". Update the work or contact the assigned supervisor.",
                    "operation_overdue",
                )
                if sent:
                    operation.reminder_after_sent_at = now
                    _system_event(operation, "overdue_reminder_sent", "In-app overdue reminder sent to the assigned team.",
                                  {"due_at": due_at.isoformat()})
            if operation.escalate_to_admin and operation.escalated_at is None and now >= due_at + timedelta(minutes=escalate_minutes):
                admins = User.query.filter_by(
                    organization_id=organization_id, role="Admin", account_status="active"
                ).all()
                sent = _notify(
                    organization_id, operation, admins,
                    "Overdue operation escalation",
                    "'" + operation.title + "' remains unresolved after its configured escalation interval. Please review the assignment and history.",
                    "operation_escalation",
                )
                if sent:
                    operation.escalated_at = now
                    _system_event(operation, "escalated_to_admin", "Overdue operation escalated to organization Admins.",
                                  {"due_at": due_at.isoformat(), "escalation_after_minutes": escalate_minutes})
        elif operation.overdue:
            operation.overdue = False

    db.session.commit()


def process_due_operations():
    now = datetime.now(timezone.utc)
    organization_ids = [
        row[0] for row in db.session.query(Organization.id)
        .filter(Organization.status == "active")
        .order_by(Organization.id.asc()).all()
    ]
    for organization_id in organization_ids:
        try:
            _process_schedules(organization_id, now)
            _process_reminders(organization_id, now)
        except Exception:
            db.session.rollback()
            raise


def start_operation_scheduler(app):
    """Run idempotent recurrence, reminder and escalation dispatch."""
    if app.config.get("TESTING") or app.config.get("DISABLE_OPERATION_SCHEDULER"):
        return None
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    def dispatch():
        with app.app_context():
            try:
                process_due_operations()
            except Exception:
                db.session.rollback()
                app.logger.exception("Operation scheduler dispatch failed.")
            finally:
                db.session.remove()

    scheduler.add_job(
        dispatch,
        trigger="interval",
        seconds=30,
        id="cleantrack-operation-dispatch",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    app.extensions["cleantrack_operation_scheduler"] = scheduler
    return scheduler
