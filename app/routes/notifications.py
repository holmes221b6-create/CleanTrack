from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity
from app.extensions import db
from app.models.notification import Notification
from app.models.user import User
from app.services.permissions import get_current_user, roles_required

notifications_bp = Blueprint("notifications", __name__, url_prefix="/api/notifications")


@notifications_bp.get("")
@roles_required("Admin", "Supervisor", "Employee")
def list_notifications():
    user = get_current_user()
    rows = Notification.query.filter_by(recipient_id=user.id).order_by(
        Notification.created_at.desc()
    ).limit(50).all()
    unread_count = Notification.query.filter_by(
        recipient_id=user.id, is_read=False
    ).count()
    return jsonify({
        "notifications": [row.to_dict() for row in rows],
        "unread_count": unread_count,
    }), 200


@notifications_bp.patch("/<int:notification_id>/read")
@roles_required("Admin", "Supervisor", "Employee")
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
@roles_required("Admin", "Supervisor", "Employee")
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
