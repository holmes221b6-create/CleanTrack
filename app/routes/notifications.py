from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity
from app.extensions import db
from app.models.notification import Notification
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.permissions import get_current_user, roles_required

notifications_bp = Blueprint("notifications", __name__, url_prefix="/api/notifications")


def _activity_label(action):
    labels = {
        "announcement.created": "created an announcement",
        "announcement.published": "published an announcement",
        "announcement.updated": "updated an announcement",
        "announcement.archived": "archived an announcement",
        "announcement.alert_escalated": "escalated an operational alert",
        "announcement.staff_urgent_alert_created": "raised an urgent alert",
    }
    return labels.get(action, action.replace(".", " ").replace("_", " "))


def _sync_organisation_activity(user):
    """Materialise recent organisation audit events as per-user notifications."""
    if not user.organization_id:
        return
    audit_rows = AuditLog.query.filter(
        AuditLog.organization_id == user.organization_id
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(50).all()
    if not audit_rows:
        return

    audit_ids = [str(row.id) for row in audit_rows]
    if not audit_ids:
        return
    existing = {
        row.resource_id for row in Notification.query.filter(
            Notification.recipient_id == user.id,
            Notification.notification_type == "organisation_activity",
            Notification.resource_type == "AuditLog",
            Notification.resource_id.in_(audit_ids),
        ).all()
    }
    actor_ids = {row.actor_id for row in audit_rows if row.actor_id}
    actors = {
        actor.id: actor.full_name
        for actor in User.query.filter(User.id.in_(actor_ids)).all()
    } if actor_ids else {}

    for event in audit_rows:
        # Audience members and the publisher already receive a purpose-built
        # announcement notification. Other organisation members still receive
        # the corresponding organisation-activity event.
        if (
            str(event.id) in existing
            or (
                event.resource_type == "Announcement"
                and str(event.resource_id) in already_notified_announcements
            )
        ):
            continue
        actor_name = actors.get(event.actor_id, "Organisation system")
        action_text = _activity_label(event.action)
        resource = (event.resource_type or "organisation record").replace("_", " ").lower()
        details = event.details if isinstance(event.details, dict) else {}
        subject = details.get("title") or details.get("name") or details.get("label")
        message = f"{actor_name} {action_text}"
        if subject:
            message += f": {str(subject)[:180]}"
        elif event.resource_type:
            message += f" ({resource})"
        db.session.add(Notification(
            recipient_id=user.id,
            organization_id=user.organization_id,
            title="Organisation activity",
            message=message[:1000],
            notification_type="organisation_activity",
            resource_type="AuditLog",
            resource_id=str(event.id),
            created_at=event.created_at,
        ))
    db.session.commit()


@notifications_bp.get("")
@roles_required("Admin", "Supervisor", "Staff")
def list_notifications():
    user = get_current_user()
    try:
        _sync_organisation_activity(user)
    except Exception:
        db.session.rollback()
        # Existing personal/system notifications remain available if activity
        # feed synchronisation encounters a transient issue.
    # The header bell is an organisation activity feed: never mix in records
    # belonging to another organisation or notifications without this tenant.
    rows = Notification.query.filter_by(
        recipient_id=user.id,
        organization_id=user.organization_id,
    ).order_by(
        Notification.created_at.desc(), Notification.id.desc()
    ).limit(50).all()
    unread_count = Notification.query.filter_by(
        recipient_id=user.id,
        organization_id=user.organization_id,
        is_read=False,
    ).count()
    return jsonify({
        "notifications": [row.to_dict() for row in rows],
        "unread_count": unread_count,
    }), 200


@notifications_bp.patch("/<int:notification_id>/read")
@roles_required("Admin", "Supervisor", "Staff")
def mark_notification_read(notification_id):
    user = get_current_user()
    row = db.session.get(Notification, notification_id)
    if row is None or row.recipient_id != user.id:
        return jsonify({"error": "Notification not found."}), 404
    if not row.is_read:
        row.is_read = True
        row.read_at = db.func.now()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "Notification could not be updated."}), 500
    return jsonify({"notification": row.to_dict()}), 200


@notifications_bp.post("/read-all")
@roles_required("Admin", "Supervisor", "Staff")
def mark_all_notifications_read():
    user = get_current_user()
    Notification.query.filter_by(recipient_id=user.id, is_read=False).update({
        "is_read": True,
        "read_at": db.func.now(),
    }, synchronize_session=False)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Notifications could not be updated."}), 500
    return jsonify({"message": "Notifications marked as read."}), 200
