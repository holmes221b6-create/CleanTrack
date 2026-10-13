from datetime import datetime, timedelta, timezone
from io import BytesIO
import os

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename
from sqlalchemy import and_, or_

from app.extensions import db
from app.models.announcement import Announcement
from app.models.announcement_attachment import AnnouncementAttachment
from app.models.announcement_receipt import AnnouncementReceipt
from app.models.audit_log import AuditLog
from app.models.location import Location
from app.models.notification import Notification
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.zone import Zone
from app.services.permissions import get_current_user, roles_required

announcements_bp = Blueprint("announcements", __name__, url_prefix="/api/announcements")
ALLOWED_TYPES = {"announcement", "alert", "instruction"}
ALLOWED_AUDIENCES = {"organization", "team", "supervisors"}
ALLOWED_SEVERITIES = {"info", "low", "medium", "high", "critical"}
ALLOWED_STATUSES = {"draft", "published", "scheduled"}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACH_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".gif": {"image/gif"},
    ".doc": {"application/msword"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".txt": {"text/plain"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".wav": {"audio/wav", "audio/x-wav"},
    ".ogg": {"audio/ogg", "application/ogg"},
    ".webm": {"audio/webm", "video/webm"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "video/mp4"},
}


def _now():
    return datetime.now(timezone.utc)


def _parse_dt(value, field_name):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a date/time string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name} is not a valid date/time.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _team_ids(user):
    return {
        item[0] for item in db.session.query(TeamMember.team_id)
        .join(Team, Team.id == TeamMember.team_id)
        .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id)
        .all()
    }


def _can_view(user, item, include_drafts=False):
    if user.organization_id != item.organization_id or item.archived_at:
        return False
    if user.role == "Admin":
        return True
    now = _now()
    status = item.status or ("published" if item.published else "draft")
    scheduled = item.scheduled_at
    if scheduled is not None and scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    is_live = status == "published" or (status == "scheduled" and scheduled and scheduled <= now)
    if not is_live:
        return include_drafts and item.created_by_id == user.id
    if item.expires_at:
        expires = item.expires_at if item.expires_at.tzinfo else item.expires_at.replace(tzinfo=timezone.utc)
        if expires <= now:
            return False
    if item.audience_scope == "organization":
        return True
    if item.audience_scope == "supervisors":
        return user.role == "Supervisor"
    if item.audience_scope == "team":
        return item.team_id in _team_ids(user)
    return False


def _audience_users(item):
    query = User.query.filter_by(organization_id=item.organization_id, account_status="active")
    if item.audience_scope == "organization":
        return query.all()
    if item.audience_scope == "supervisors":
        return query.filter_by(role="Supervisor").all()
    if item.audience_scope == "team":
        return (query.join(TeamMember, TeamMember.user_id == User.id)
                .filter(TeamMember.team_id == item.team_id).all())
    return []



def _is_target_recipient(user, item):
    if user.organization_id != item.organization_id or user.account_status != "active":
        return False
    if item.audience_scope == "organization":
        return True
    if item.audience_scope == "supervisors":
        return user.role == "Supervisor"
    if item.audience_scope == "team":
        return item.team_id in _team_ids(user)
    return False


def _audit(actor, action, item, details=None):
    db.session.add(AuditLog(
        actor_id=actor.id if actor else None,
        organization_id=item.organization_id if item else (actor.organization_id if actor else None),
        action=action,
        resource_type="Announcement",
        resource_id=str(item.id) if item and item.id else None,
        details=details or {},
        ip_address=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")[:45] or None,
        user_agent=(request.user_agent.string or "")[:300] or None,
    ))


def _ensure_receipts(item):
    if not item.published and item.status != "published":
        return False
    existing = {row.user_id for row in AnnouncementReceipt.query.filter_by(announcement_id=item.id).all()}
    changed = False
    for person in _audience_users(item):
        if person.id == item.created_by_id or person.id in existing:
            continue
        db.session.add(AnnouncementReceipt(announcement_id=item.id, user_id=person.id))
        changed = True
    return changed


def _send_notice(recipient, item, notification_type):
    db.session.add(Notification(
        recipient_id=recipient.id,
        organization_id=item.organization_id,
        title=item.title[:160],
        message=item.message[:1000],
        notification_type=notification_type[:50],
        resource_type="Announcement",
        resource_id=str(item.id),
    ))


def _publish(item, actor=None):
    item.status = "published"
    item.published = True
    item.published_at = _now()
    item.scheduled_at = None
    users = _audience_users(item)
    for recipient in users:
        if recipient.id != item.created_by_id:
            _send_notice(recipient, item, "operational_alert" if item.announcement_type == "alert" else "announcement")
    if item.escalate_to_admin and item.announcement_type == "alert":
        admin_ids = {row.id for row in User.query.filter_by(
            organization_id=item.organization_id, role="Admin", account_status="active"
        ).all()}
        for recipient in User.query.filter(User.id.in_(admin_ids)).all():
            if recipient.id != item.created_by_id and recipient not in users:
                _send_notice(recipient, item, "escalated_alert")
        item.escalated_at = _now() if item.severity in {"high", "critical"} else item.escalated_at
    _ensure_receipts(item)
    _audit(actor, "announcement.published", item, {"title": item.title, "audience_scope": item.audience_scope})


def _process_due_schedules(org_id):
    now = _now()
    rows = Announcement.query.filter(
        Announcement.organization_id == org_id,
        Announcement.status == "scheduled",
        Announcement.scheduled_at.isnot(None),
        Announcement.scheduled_at <= now,
        Announcement.archived_at.is_(None),
    ).all()
    if not rows:
        return
    for item in rows:
        _publish(item, actor=None)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _process_escalations(org_id):
    now = _now()
    rows = Announcement.query.filter(
        Announcement.organization_id == org_id,
        Announcement.announcement_type == "alert",
        Announcement.status == "published",
        Announcement.alert_status == "open",
        Announcement.escalate_to_admin.is_(True),
        Announcement.escalated_at.is_(None),
        Announcement.archived_at.is_(None),
    ).all()
    changed = False
    admins = User.query.filter_by(organization_id=org_id, role="Admin", account_status="active").all()
    for item in rows:
        created = item.published_at or item.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        serious = item.severity in {"high", "critical"}
        elapsed = item.escalation_after_minutes is not None and created and now >= created + timedelta(minutes=item.escalation_after_minutes)
        if not serious and not elapsed:
            continue
        for admin in admins:
            if admin.id != item.created_by_id:
                _send_notice(admin, item, "escalated_alert")
        item.escalated_at = now
        _audit(None, "announcement.alert_escalated", item, {
            "severity": item.severity, "reason": "serious severity" if serious else "unresolved beyond configured threshold"
        })
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _validate_target(user, data, audience):
    team_id = data.get("team_id")
    location_id = data.get("target_location_id")
    zone_id = data.get("target_zone_id")
    team = location = zone = None
    if audience == "team":
        if isinstance(team_id, bool):
            raise ValueError("Select a valid team.")
        try:
            team_id = int(team_id)
        except (TypeError, ValueError):
            raise ValueError("A team is required for team announcements.")
        team = Team.query.filter_by(id=team_id, organization_id=user.organization_id, status="active").first()
        if not team:
            raise ValueError("That team is not available in your organization.")
        if user.role == "Supervisor" and team.id not in _team_ids(user):
            raise PermissionError("Supervisors can publish only to their own assigned teams.")
    else:
        team_id = None
        if audience == "organization" and user.role == "Supervisor":
            raise PermissionError("Supervisors may publish only to their assigned teams.")

    if location_id not in (None, ""):
        try:
            location_id = int(location_id)
        except (ValueError, TypeError):
            raise ValueError("Choose a valid location.")
        location = Location.query.filter_by(id=location_id, organization_id=user.organization_id).first()
        if location is None:
            raise ValueError("Location not found in your organization.")
    else:
        location_id = None

    if zone_id not in (None, ""):
        try:
            zone_id = int(zone_id)
        except (ValueError, TypeError):
            raise ValueError("Choose a valid zone.")
        zone = Zone.query.filter_by(id=zone_id, organization_id=user.organization_id).first()
        if zone is None:
            raise ValueError("Zone not found in your organization.")
        if location and zone.location_id != location.id:
            raise ValueError("The selected zone does not belong to the selected location.")
        if location is None:
            location_id = zone.location_id
    else:
        zone_id = None

    if team and location and location.team_id not in (None, team.id):
        raise ValueError("The selected location is not assigned to that team.")
    if team and zone and zone.team_id not in (None, team.id):
        raise ValueError("The selected zone is not assigned to that team.")
    return team_id, location_id, zone_id


def _apply_payload(item, user, data, is_create=False):
    title = data.get("title", item.title if item else "")
    message = data.get("message", item.message if item else "")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise ValueError("Title is required and must be 200 characters or fewer.")
    if not isinstance(message, str) or not message.strip() or len(message.strip()) > 12000:
        raise ValueError("Message is required and must be 12,000 characters or fewer.")
    kind = str(data.get("announcement_type", item.announcement_type if item else "announcement")).lower()
    if kind not in ALLOWED_TYPES:
        raise ValueError("Choose announcement, alert, or instruction.")
    audience = str(data.get("audience_scope", item.audience_scope if item else ("organization" if user.role == "Admin" else "team"))).lower()
    if audience not in ALLOWED_AUDIENCES:
        raise ValueError("Choose a supported target audience.")
    if user.role == "Supervisor" and audience != "team":
        raise PermissionError("Supervisors can publish only to their own teams.")
    if audience == "supervisors" and user.role != "Admin":
        raise PermissionError("Only an Admin can target all supervisors.")
    if kind != "alert":
        severity = "info"
    else:
        severity = str(data.get("severity", item.severity if item else "medium")).lower()
        if severity not in ALLOWED_SEVERITIES:
            raise ValueError("Choose a valid severity.")
    team_id, location_id, zone_id = _validate_target(user, data, audience)

    requested_status = str(data.get("status", item.status if item else "published")).lower()
    if requested_status not in ALLOWED_STATUSES:
        raise ValueError("Choose draft, publish now, or schedule.")
    scheduled_at = _parse_dt(data.get("scheduled_at", item.scheduled_at.isoformat() if item and item.scheduled_at else None), "Scheduled time")
    expires_at = _parse_dt(data.get("expires_at", item.expires_at.isoformat() if item and item.expires_at else None), "Expiry time")
    if scheduled_at and scheduled_at <= _now() and requested_status == "scheduled":
        raise ValueError("Scheduled time must be in the future.")
    if expires_at and scheduled_at and expires_at <= scheduled_at:
        raise ValueError("Expiry must be after the scheduled publish time.")
    if expires_at and not scheduled_at and requested_status == "published" and expires_at <= _now():
        raise ValueError("Expiry time must be in the future.")

    is_pinned = data.get("is_pinned", item.is_pinned if item else False)
    requires_ack = data.get("requires_acknowledgement", item.requires_acknowledgement if item else False)
    escalate = data.get("escalate_to_admin", item.escalate_to_admin if item else False)
    for label, value in (("pin", is_pinned), ("acknowledgement", requires_ack), ("escalation", escalate)):
        if not isinstance(value, bool):
            raise ValueError(f"{label.capitalize()} must be true or false.")
    escalation_after = data.get("escalation_after_minutes", item.escalation_after_minutes if item else 15)
    if escalation_after in (None, ""):
        escalation_after = None
    else:
        try:
            escalation_after = int(escalation_after)
        except (TypeError, ValueError):
            raise ValueError("Escalation delay must be a number of minutes.")
        if escalation_after < 1 or escalation_after > 10080:
            raise ValueError("Escalation delay must be between 1 minute and 7 days.")
    if escalate and kind != "alert":
        raise ValueError("Only operational alerts can be escalated.")
    if requested_status == "scheduled" and scheduled_at is None:
        raise ValueError("Choose a future date and time to schedule this announcement.")

    item.title = title.strip()
    item.message = message.strip()
    item.announcement_type = kind
    item.audience_scope = audience
    item.severity = severity
    item.team_id = team_id
    item.target_location_id = location_id
    item.target_zone_id = zone_id
    item.is_pinned = is_pinned
    item.requires_acknowledgement = requires_ack
    item.escalate_to_admin = escalate
    item.escalation_after_minutes = escalation_after if escalate else None
    item.expires_at = expires_at
    old_status = item.status if item else None
    if requested_status == "scheduled":
        item.status = "scheduled"
        item.published = False
        item.scheduled_at = scheduled_at
        item.published_at = None
    elif requested_status == "published":
        item.status = "published"
        item.published = True
        item.scheduled_at = None
        if old_status != "published" or not item.published_at:
            item.published_at = _now()
    else:
        item.status = "draft"
        item.published = False
        item.scheduled_at = None
        item.published_at = None
    return old_status


@announcements_bp.get("/options")
@roles_required("Admin", "Supervisor", "Employee")
def options():
    user = get_current_user()
    teams_query = Team.query.filter_by(organization_id=user.organization_id, status="active")
    if user.role == "Supervisor":
        teams_query = teams_query.filter(Team.id.in_(_team_ids(user)))
    if user.role == "Employee":
        teams_query = teams_query.filter(Team.id.in_(_team_ids(user)))
    teams = teams_query.order_by(Team.name.asc()).all()
    team_ids = [row.id for row in teams]
    loc_query = Location.query.filter_by(organization_id=user.organization_id, status="active")
    zone_query = Zone.query.filter_by(organization_id=user.organization_id, status="active")
    if user.role != "Admin":
        loc_query = loc_query.filter(or_(Location.team_id.is_(None), Location.team_id.in_(team_ids)))
        zone_query = zone_query.filter(or_(Zone.team_id.is_(None), Zone.team_id.in_(team_ids)))
    return jsonify({
        "teams": [row.to_dict() for row in teams],
        "locations": [row.to_dict() for row in loc_query.order_by(Location.name.asc()).all()],
        "zones": [row.to_dict() for row in zone_query.order_by(Zone.name.asc()).all()],
        "can_create": user.role in {"Admin", "Supervisor"},
        "role": user.role,
    }), 200


@announcements_bp.get("")
@roles_required("Admin", "Supervisor", "Employee")
def list_announcements():
    user = get_current_user()
    _process_due_schedules(user.organization_id)
    _process_escalations(user.organization_id)
    query = Announcement.query.filter_by(organization_id=user.organization_id).filter(Announcement.archived_at.is_(None))
    if user.role == "Admin":
        pass
    else:
        now = _now()
        published_clause = or_(
            Announcement.status == "published",
            and_(Announcement.status == "scheduled", Announcement.scheduled_at.isnot(None), Announcement.scheduled_at <= now),
        )
        team_ids = list(_team_ids(user))
        visible_scope = [Announcement.audience_scope == "organization"]
        if user.role == "Supervisor":
            visible_scope.append(Announcement.audience_scope == "supervisors")
        if team_ids:
            visible_scope.append(and_(Announcement.audience_scope == "team", Announcement.team_id.in_(team_ids)))
        own_drafts = Announcement.created_by_id == user.id if user.role == "Supervisor" else False
        query = query.filter(or_(and_(published_clause, or_(*visible_scope)), own_drafts))
        query = query.filter(or_(Announcement.expires_at.is_(None), Announcement.expires_at > now, own_drafts))
    status_filter = request.args.get("status")
    if status_filter in ALLOWED_STATUSES:
        query = query.filter(Announcement.status == status_filter)
    type_filter = request.args.get("type")
    if type_filter in ALLOWED_TYPES:
        query = query.filter(Announcement.announcement_type == type_filter)
    rows = query.order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc()).limit(250).all()
    changed = False
    result = []
    for item in rows:
        can_view = _can_view(user, item, include_drafts=(user.role == "Supervisor"))
        if not can_view:
            continue
        # Materialize real unread receipt rows for current recipients of
        # legacy and newly-active announcements; read/ack timestamps stay null
        # until the user actually takes the corresponding action.
        if item.status == "published":
            changed = _ensure_receipts(item) or changed
        is_recipient = _is_target_recipient(user, item)
        receipt = None
        if is_recipient and user.id != item.created_by_id:
            receipt = AnnouncementReceipt.query.filter_by(announcement_id=item.id, user_id=user.id).first()
            is_published = item.status == "published" or (
                item.status == "scheduled" and item.scheduled_at and
                (item.scheduled_at.replace(tzinfo=timezone.utc) if item.scheduled_at.tzinfo is None else item.scheduled_at) <= _now()
            )
            if receipt is None and is_published:
                receipt = AnnouncementReceipt(announcement_id=item.id, user_id=user.id)
                db.session.add(receipt)
                changed = True
        data = item.to_dict(include_receipts=user.role == "Admin")
        if receipt:
            state = "acknowledged" if receipt.acknowledged_at else "read" if receipt.read_at else "unread"
            data["my_receipt"] = {"read_at": receipt.read_at.isoformat() if receipt.read_at else None, "acknowledged_at": receipt.acknowledged_at.isoformat() if receipt.acknowledged_at else None, "state": state}
        else:
            data["my_receipt"] = {"state": "issued" if item.created_by_id == user.id else "oversight" if user.role == "Admin" and not is_recipient else "unread", "read_at": None, "acknowledged_at": None}
        data["can_mark_read"] = bool(is_recipient and user.id != item.created_by_id and item.status == "published")
        data["can_acknowledge"] = bool(data["can_mark_read"] and item.requires_acknowledgement)
        result.append(data)
    if changed:
        db.session.commit()
    return jsonify({"announcements": result}), 200


@announcements_bp.post("")
@roles_required("Admin", "Supervisor")
def create_announcement():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    item = Announcement(organization_id=user.organization_id, created_by_id=user.id)
    try:
        _apply_payload(item, user, data, is_create=True)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.add(item)
    try:
        db.session.flush()
        _audit(user, "announcement.created", item, {"title": item.title, "status": item.status, "audience_scope": item.audience_scope, "severity": item.severity})
        if item.status == "published":
            _ensure_receipts(item)
            for recipient in _audience_users(item):
                if recipient.id != user.id:
                    _send_notice(recipient, item, "operational_alert" if item.announcement_type == "alert" else "announcement")
            if item.escalate_to_admin and item.announcement_type == "alert" and item.severity in {"high", "critical"}:
                item.escalated_at = _now()
                for admin in User.query.filter_by(organization_id=user.organization_id, role="Admin", account_status="active").all():
                    if admin.id != user.id:
                        _send_notice(admin, item, "escalated_alert")
                _audit(user, "announcement.alert_escalated", item, {"severity": item.severity, "reason": "serious severity"})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Announcement could not be saved."}), 500
    return jsonify({"message": "Announcement saved.", "announcement": item.to_dict(include_receipts=user.role == "Admin")}), 201


@announcements_bp.patch("/<int:announcement_id>")
@roles_required("Admin", "Supervisor")
def update_announcement(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id).first()
    if item is None or item.archived_at:
        return jsonify({"error": "Announcement not found."}), 404
    if user.role == "Supervisor" and (item.created_by_id != user.id or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can edit only their own announcements for assigned teams."}), 403
    before = {"title": item.title, "status": item.status, "severity": item.severity, "is_pinned": item.is_pinned, "audience_scope": item.audience_scope}
    data = request.get_json(silent=True) or {}
    try:
        old_status = item.status
        _apply_payload(item, user, data)
        if item.status == "published" and (old_status != "published" or not item.published_at):
            item.published_at = _now()
            item.published = True
            for recipient in _audience_users(item):
                if recipient.id != user.id:
                    _send_notice(recipient, item, "operational_alert" if item.announcement_type == "alert" else "announcement")
            _ensure_receipts(item)
        _audit(user, "announcement.updated", item, {"before": before, "after": {"title": item.title, "status": item.status, "severity": item.severity, "is_pinned": item.is_pinned, "audience_scope": item.audience_scope}})
        db.session.commit()
    except PermissionError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Announcement could not be updated."}), 500
    return jsonify({"message": "Announcement updated.", "announcement": item.to_dict(include_receipts=user.role == "Admin")}), 200


@announcements_bp.delete("/<int:announcement_id>")
@roles_required("Admin", "Supervisor")
def archive_announcement(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None:
        return jsonify({"error": "Announcement not found."}), 404
    if user.role == "Supervisor" and (item.created_by_id != user.id or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can remove only their own team announcements."}), 403
    item.archived_at = _now()
    item.status = "archived"
    item.published = False
    _audit(user, "announcement.archived", item, {"title": item.title, "reason": "Removed from active announcements; retained for audit history."})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Announcement could not be removed."}), 500
    return jsonify({"message": "Announcement removed from active view."}), 200


@announcements_bp.post("/<int:announcement_id>/read")
@roles_required("Admin", "Supervisor", "Employee")
def mark_read(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None or not _can_view(user, item, include_drafts=(user.role == "Supervisor" and item.created_by_id == user.id)):
        return jsonify({"error": "Announcement not found."}), 404
    if not _is_target_recipient(user, item) or item.created_by_id == user.id:
        return jsonify({"error": "Read receipts are available only to intended recipients."}), 403
    if item.status in {"draft", "scheduled"} and item.created_by_id != user.id:
        return jsonify({"error": "This announcement has not been published yet."}), 409
    receipt = AnnouncementReceipt.query.filter_by(announcement_id=item.id, user_id=user.id).first()
    if receipt is None:
        receipt = AnnouncementReceipt(announcement_id=item.id, user_id=user.id)
        db.session.add(receipt)
    if receipt.read_at is None:
        receipt.read_at = _now()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Read state could not be saved."}), 500
    return jsonify({"receipt": receipt.to_dict()}), 200


@announcements_bp.post("/<int:announcement_id>/acknowledge")
@roles_required("Admin", "Supervisor", "Employee")
def acknowledge(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None or not _can_view(user, item):
        return jsonify({"error": "Announcement not found."}), 404
    if not _is_target_recipient(user, item) or item.created_by_id == user.id:
        return jsonify({"error": "Only intended recipients can acknowledge this announcement."}), 403
    if not item.requires_acknowledgement:
        return jsonify({"error": "Acknowledgement is not required for this announcement."}), 409
    receipt = AnnouncementReceipt.query.filter_by(announcement_id=item.id, user_id=user.id).first()
    if receipt is None:
        receipt = AnnouncementReceipt(announcement_id=item.id, user_id=user.id)
        db.session.add(receipt)
    receipt.read_at = receipt.read_at or _now()
    receipt.acknowledged_at = receipt.acknowledged_at or _now()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Acknowledgement could not be saved."}), 500
    return jsonify({"receipt": receipt.to_dict()}), 200


@announcements_bp.get("/<int:announcement_id>/receipts")
@roles_required("Admin", "Supervisor")
def list_receipts(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None:
        return jsonify({"error": "Announcement not found."}), 404
    if user.role == "Supervisor" and (item.audience_scope != "team" or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can review receipts only for announcements targeted to their assigned teams."}), 403
    rows = AnnouncementReceipt.query.filter_by(announcement_id=item.id).join(User, User.id == AnnouncementReceipt.user_id).order_by(User.full_name.asc()).all()
    return jsonify({"announcement_id": item.id, "requires_acknowledgement": item.requires_acknowledgement, "receipts": [row.to_dict() for row in rows]}), 200


@announcements_bp.post("/<int:announcement_id>/resolve")
@roles_required("Admin", "Supervisor")
def resolve_alert(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None or item.announcement_type != "alert":
        return jsonify({"error": "Operational alert not found."}), 404
    if user.role == "Supervisor" and (item.audience_scope != "team" or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can resolve alerts targeted to their assigned teams."}), 403
    was_resolved = item.alert_status == "resolved"
    item.alert_status = "resolved"
    item.resolved_at = item.resolved_at or _now()
    item.resolved_by_id = user.id
    _audit(user, "announcement.alert_resolved", item, {"title": item.title, "previous_status": "resolved" if was_resolved else "open"})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Alert could not be marked resolved."}), 500
    return jsonify({"message": "Alert marked resolved.", "announcement": item.to_dict()}), 200


@announcements_bp.post("/<int:announcement_id>/attachments")
@roles_required("Admin", "Supervisor")
def upload_attachments(announcement_id):
    user = get_current_user()
    item = Announcement.query.filter_by(id=announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None:
        return jsonify({"error": "Announcement not found."}), 404
    if user.role == "Supervisor" and (item.created_by_id != user.id or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can attach files only to their own team announcements."}), 403
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Choose at least one attachment."}), 400
    accepted = []
    total = 0
    for file in files:
        if not file or not file.filename:
            continue
        name = secure_filename(file.filename)
        ext = os.path.splitext(name)[1].lower()
        content_type = (file.mimetype or "application/octet-stream").lower()
        if ext not in ALLOWED_EXTENSIONS or content_type not in ALLOWED_EXTENSIONS[ext]:
            return jsonify({"error": f"File type is not allowed: {name}"}), 400
        data = file.read(MAX_FILE_BYTES + 1)
        if not data or len(data) > MAX_FILE_BYTES:
            return jsonify({"error": f"Each attachment must be between 1 byte and 10 MB: {name}"}), 413
        total += len(data)
        if total > MAX_TOTAL_ATTACH_BYTES:
            return jsonify({"error": "Attachments must total 20 MB or less per upload."}), 413
        audio_extensions = {".mp3", ".wav", ".ogg", ".webm", ".m4a"}
        category = "voice" if content_type.startswith("audio/") or ext in audio_extensions else "image" if content_type.startswith("image/") else "file"
        if category == "voice" and user.role == "Admin" and item.audience_scope != "supervisors":
            return jsonify({"error": "Admin voice notes are available for announcements targeted to all supervisors. Use text or a document for other audiences."}), 400
        accepted.append((name, content_type, category, data))
    if not accepted:
        return jsonify({"error": "No valid files were selected."}), 400
    created = []
    try:
        for name, content_type, category, data in accepted:
            row = AnnouncementAttachment(announcement_id=item.id, uploaded_by_id=user.id, filename=name, content_type=content_type, category=category, size=len(data), data=data)
            db.session.add(row)
            db.session.flush()
            created.append({"id": row.id, "filename": row.filename, "category": row.category, "size": row.size})
        _audit(user, "announcement.attachments_added", item, {"attachments": created})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Attachments could not be stored."}), 500
    return jsonify({"attachments": created}), 201


@announcements_bp.get("/attachments/<int:attachment_id>/download")
@roles_required("Admin", "Supervisor", "Employee")
def download_attachment(attachment_id):
    user = get_current_user()
    attachment = db.session.get(AnnouncementAttachment, attachment_id)
    if attachment is None:
        return jsonify({"error": "Attachment not found."}), 404
    item = Announcement.query.filter_by(id=attachment.announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None or not _can_view(user, item, include_drafts=(user.role == "Supervisor" and item.created_by_id == user.id)):
        return jsonify({"error": "Attachment not found."}), 404
    response = send_file(BytesIO(attachment.data), mimetype=attachment.content_type, as_attachment=True, download_name=attachment.filename, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@announcements_bp.delete("/attachments/<int:attachment_id>")
@roles_required("Admin", "Supervisor")
def delete_attachment(attachment_id):
    user = get_current_user()
    attachment = db.session.get(AnnouncementAttachment, attachment_id)
    if attachment is None:
        return jsonify({"error": "Attachment not found."}), 404
    item = Announcement.query.filter_by(id=attachment.announcement_id, organization_id=user.organization_id, archived_at=None).first()
    if item is None:
        return jsonify({"error": "Attachment not found."}), 404
    if user.role == "Supervisor" and (item.created_by_id != user.id or item.team_id not in _team_ids(user)):
        return jsonify({"error": "Supervisors can remove files only from their own team announcements."}), 403
    _audit(user, "announcement.attachment_removed", item, {"filename": attachment.filename, "attachment_id": attachment.id})
    db.session.delete(attachment)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Attachment could not be removed."}), 500
    return jsonify({"message": "Attachment removed."}), 200
