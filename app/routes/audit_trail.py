import csv
import io
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, make_response
from sqlalchemy import cast, func, or_, String, false

from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.operation import Operation
from app.models.team_member import TeamMember
from app.models.user import User
from app.services.audit_retention import audit_cutoff, purge_expired_audit_logs
from app.services.permissions import get_current_user, roles_required


audit_trail_bp = Blueprint("audit_trail", __name__, url_prefix="/api/audit-trail")


def _parse_date(value, field_name):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a date in YYYY-MM-DD format.")


def _scoped_query(user):
    query = AuditLog.query.outerjoin(User, User.id == AuditLog.actor_id)
    query = query.filter(AuditLog.created_at >= audit_cutoff())

    if user.organization_id is not None:
        query = query.filter(AuditLog.organization_id == user.organization_id)
    else:
        query = query.filter(AuditLog.actor_id == user.id)

    role = (user.role or "").lower()
    if role == "admin":
        return query

    if role == "supervisor":
        team_ids = [
            row[0] for row in db.session.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user.id).all()
        ]
        assigned_operation_ids = db.session.query(cast(Operation.id, String)).filter(
            Operation.organization_id == user.organization_id,
            or_(
                Operation.assigned_to_id == user.id,
                Operation.team_id.in_(team_ids) if team_ids else false()
            )
        )
        return query.filter(
            or_(
                AuditLog.actor_id == user.id,
                (
                    (AuditLog.resource_type == "Operation") &
                    AuditLog.resource_id.in_(assigned_operation_ids)
                ),
                (
                    (AuditLog.resource_type == "Team") &
                    AuditLog.resource_id.in_([str(team_id) for team_id in team_ids])
                )
            ),
            or_(
                AuditLog.action.ilike("operation.%"),
                AuditLog.action.ilike("team.%"),
                AuditLog.action.ilike("security.%"),
                AuditLog.action.ilike("account.%")
            )
        )

    # Employees see their own actions and the audit events for operations
    # assigned to them, but never administrative or other users' activity.
    own_operation_ids = db.session.query(cast(Operation.id, String)).filter(
        Operation.organization_id == user.organization_id,
        Operation.assigned_to_id == user.id
    )
    return query.filter(
        or_(
            AuditLog.actor_id == user.id,
            (
                (AuditLog.resource_type == "Operation") &
                AuditLog.resource_id.in_(own_operation_ids)
            )
        ),
        or_(
            AuditLog.action.ilike("operation.%"),
            AuditLog.action.ilike("security.%"),
            AuditLog.action.ilike("account.%")
        )
    )


def _outcome(entry):
    details = entry.details if isinstance(entry.details, dict) else {}
    status = details.get("outcome") or details.get("new_status") or details.get("status")
    if status:
        return str(status)
    action = (entry.action or "").lower()
    if any(word in action for word in ("failed", "failure", "denied", "error")):
        return "Failed"
    if "rejected" in action or "declined" in action:
        return "Rejected"
    if "approved" in action:
        return "Approved"
    if any(word in action for word in (
        "created", "updated", "assigned", "archived", "restored", "deleted",
        "completed", "started", "submitted", "activated", "suspended",
        "cancelled", "canceled", "reviewed", "settings_updated"
    )):
        return "Success"
    return "Recorded"


def _display(entry, actor_name=None, actor_email=None, actor_role=None):
    details = entry.details if isinstance(entry.details, dict) else {}
    resource = entry.resource_type or "Record"
    resource_id = entry.resource_id or ""
    affected = (
        details.get("affected_record") or details.get("title") or details.get("name")
        or details.get("member_name") or details.get("team_name")
        or details.get("location_name") or details.get("zone_name")
        or (f"{resource} #{resource_id}" if resource_id else resource)
    )
    before = details.get("before")
    after = details.get("after")
    if before is None:
        field_changes = {
            key: value.get("before") for key, value in details.items()
            if isinstance(value, dict) and "before" in value
        }
        if field_changes:
            before = field_changes
    if after is None:
        field_changes = {
            key: value.get("after") for key, value in details.items()
            if isinstance(value, dict) and "after" in value
        }
        if field_changes:
            after = field_changes
    if before is None and "previous_status" in details:
        before = {"status": details.get("previous_status")}
    if after is None and "new_status" in details:
        after = {"status": details.get("new_status")}
    return {
        **entry.to_dict(),
        "actor_name": actor_name or actor_email or ("System" if entry.actor_id is None else "Unknown account"),
        "actor_email": actor_email,
        "actor_role": actor_role,
        "affected_record": str(affected),
        "outcome": _outcome(entry),
        "before": before,
        "after": after
    }


def _filtered_rows(user):
    try:
        from_date = _parse_date(request.args.get("from"), "from")
        to_date = _parse_date(request.args.get("to"), "to")
    except ValueError as exc:
        return None, (jsonify(error=str(exc)), 400)

    if from_date and to_date and from_date > to_date:
        return None, (jsonify(error="The start date must be on or before the end date."), 400)

    query = _scoped_query(user)
    if from_date:
        query = query.filter(AuditLog.created_at >= max(from_date, audit_cutoff()))
    if to_date:
        query = query.filter(AuditLog.created_at < to_date + timedelta(days=1))

    category = (request.args.get("category") or "").strip().lower()
    category_prefixes = {
        "organization": ("organization.",),
        "membership": ("organization.membership_", "membership."),
        "team": ("team.",),
        "location": ("location.",),
        "zone": ("zone.",),
        "operation": ("operation.",),
        "security": ("security.", "account.")
    }
    if category in category_prefixes:
        prefixes = category_prefixes[category]
        query = query.filter(or_(*(AuditLog.action.ilike(prefix + "%") for prefix in prefixes)))
    elif category == "other":
        known = tuple(prefix for values in category_prefixes.values() for prefix in values)
        query = query.filter(~or_(*(AuditLog.action.ilike(prefix + "%") for prefix in known)))

    outcome = (request.args.get("outcome") or "").strip().lower()
    if outcome == "rejected":
        query = query.filter(or_(AuditLog.action.ilike("%rejected%"), AuditLog.action.ilike("%declined%")))
    elif outcome == "approved":
        query = query.filter(AuditLog.action.ilike("%approved%"))
    elif outcome == "failed":
        query = query.filter(or_(AuditLog.action.ilike("%failed%"), AuditLog.action.ilike("%failure%"), AuditLog.action.ilike("%denied%"), AuditLog.action.ilike("%error%")))
    elif outcome == "success":
        query = query.filter(or_(*(AuditLog.action.ilike("%" + word + "%") for word in (
            "created", "updated", "assigned", "archived", "restored", "deleted",
            "completed", "started", "submitted", "activated", "suspended",
            "cancelled", "canceled", "settings_updated"
        ))))

    actor_id = request.args.get("actor_id", type=int)
    if actor_id:
        query = query.filter(AuditLog.actor_id == actor_id)

    resource_type = (request.args.get("resource_type") or "").strip()
    if resource_type:
        query = query.filter(AuditLog.resource_type.ilike(resource_type))

    search = (request.args.get("q") or "").strip()
    if search:
        pattern = "%" + search[:120] + "%"
        query = query.filter(or_(
            AuditLog.action.ilike(pattern),
            AuditLog.resource_type.ilike(pattern),
            AuditLog.resource_id.ilike(pattern),
            func.coalesce(User.full_name, "").ilike(pattern),
            func.coalesce(User.email, "").ilike(pattern),
            cast(AuditLog.details, String).ilike(pattern)
        ))

    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()), None


def _rows_for_query(query, limit=None, offset=0):
    if limit is not None:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)
    rows = query.with_entities(AuditLog, User.full_name, User.email, User.role).all()
    return [_display(entry, actor_name, actor_email, actor_role)
            for entry, actor_name, actor_email, actor_role in rows]


@audit_trail_bp.get("")
@roles_required("Admin", "Supervisor", "Employee")
def audit_trail():
    user = get_current_user()
    try:
        purge_expired_audit_logs()
    except Exception:
        db.session.rollback()

    query, error = _filtered_rows(user)
    if error:
        return error
    try:
        total = query.order_by(None).count()
        limit = max(1, min(request.args.get("limit", default=50, type=int), 200))
        offset = max(0, min(request.args.get("offset", default=0, type=int), 1000000))
        rows = _rows_for_query(query, limit=limit, offset=offset)
        actor_rows = (
            query.order_by(None)
            .with_entities(AuditLog.actor_id, User.full_name, User.email, User.role)
            .distinct()
            .order_by(User.full_name.asc())
            .limit(500)
            .all()
        )
        actors = [
            {"id": actor_id, "label": name or email or "Unknown account", "role": actor_role}
            for actor_id, name, email, actor_role in actor_rows if actor_id is not None
        ]
        return jsonify(
            entries=rows, count=len(rows), total=total, limit=limit, offset=offset,
            actors=actors, retention_days=7, cutoff=audit_cutoff().isoformat()
        ), 200
    except Exception:
        db.session.rollback()
        return jsonify(error="Audit records could not be loaded."), 500


@audit_trail_bp.get("/export")
@roles_required("Admin", "Supervisor", "Employee")
def export_audit_trail():
    user = get_current_user()
    try:
        purge_expired_audit_logs()
    except Exception:
        db.session.rollback()
    query, error = _filtered_rows(user)
    if error:
        return error

    try:
        rows = _rows_for_query(query)
        export_format = (request.args.get("format") or "csv").lower()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if export_format == "json":
            response = make_response(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "retention_days": 7,
                "count": len(rows),
                "entries": rows
            }, ensure_ascii=False, indent=2, default=str))
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-audit-{stamp}.json"'
            return response
        if export_format != "csv":
            return jsonify(error="Export format must be csv or json."), 400
        buffer = io.StringIO()
        columns = ["id", "created_at", "actor_name", "actor_email", "actor_role", "action",
                   "resource_type", "resource_id", "affected_record", "outcome", "before", "after", "details"]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serial = dict(row)
            for key in ("before", "after", "details"):
                serial[key] = json.dumps(serial.get(key), ensure_ascii=False, default=str)
            writer.writerow(serial)
        response = make_response(buffer.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-audit-{stamp}.csv"'
        return response
    except Exception:
        db.session.rollback()
        return jsonify(error="Audit records could not be exported."), 500
