from datetime import datetime, timedelta, timezone
import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, make_response
from sqlalchemy import func, or_

from app.extensions import db
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.location import Location
from app.models.zone import Zone
from app.models.operation import Operation
from app.models.operation_log import OperationLog
from app.models.audit_log import AuditLog
from app.services.audit_retention import audit_cutoff, purge_expired_audit_logs
from app.models.notification import Notification
from app.services.permissions import get_current_user, roles_required


teams_bp = Blueprint("teams", __name__, url_prefix="/api/teams")
DONE_STATUSES = {"completed", "complete", "approved", "done", "closed"}
CANCELLED_STATUSES = {"cancelled", "canceled"}
ACTIVE_STATUSES = {"assigned", "in progress", "in_progress"}
PENDING_STATUSES = {"created", "pending", "submitted", "rejected"}


def _audit(admin, team, action, details):
    db.session.add(AuditLog(
        actor_id=admin.id,
        organization_id=admin.organization_id,
        action=action,
        resource_type="Team",
        resource_id=str(team.id),
        details=details or {},
    ))


def _notify_admins(admin, team, title, message, notification_type="team"):
    admins = User.query.filter_by(
        organization_id=admin.organization_id,
        role="Admin",
        account_status="active",
    ).all()
    for recipient in admins:
        db.session.add(Notification(
            recipient_id=recipient.id,
            organization_id=admin.organization_id,
            title=title[:160],
            message=message[:1000],
            notification_type=notification_type,
            resource_type="Team",
            resource_id=str(team.id),
        ))


def _team_or_404(team_id, admin):
    return Team.query.filter_by(id=team_id, organization_id=admin.organization_id).first()


def _dt_utc(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _team_data(team, admin, include_directory=False):
    member_rows = (
        db.session.query(TeamMember, User)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team.id, User.organization_id == admin.organization_id)
        .order_by(func.lower(User.full_name))
        .all()
    )
    members = [{
        "id": user.id, "name": user.full_name, "email": user.email,
        "role": user.role, "account_status": user.account_status,
        "assigned_at": membership.assigned_at.isoformat() if membership.assigned_at else None,
    } for membership, user in member_rows]

    locations = Location.query.filter_by(
        organization_id=admin.organization_id, team_id=team.id
    ).order_by(Location.name.asc()).all()
    location_ids = [row.id for row in locations]
    zone_filter = [Zone.team_id == team.id]
    if location_ids:
        zone_filter.append(Zone.location_id.in_(location_ids))
    zones = Zone.query.filter(
        Zone.organization_id == admin.organization_id, or_(*zone_filter)
    ).order_by(Zone.name.asc()).all()
    zone_ids = [row.id for row in zones]

    operation_filter = [Operation.team_id == team.id]
    if location_ids:
        operation_filter.append(Operation.location_id.in_(location_ids))
    if zone_ids:
        operation_filter.append(Operation.zone_id.in_(zone_ids))
    operations = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        or_(*operation_filter),
    ).order_by(Operation.created_at.desc(), Operation.id.desc()).all()

    now = datetime.now(timezone.utc)
    normalized = []
    for operation in operations:
        status = str(operation.status or "").strip().lower()
        completed = status in DONE_STATUSES
        due = _dt_utc(operation.due_at)
        past_due = bool(operation.overdue) or (
            due is not None and due < now and not completed and status not in CANCELLED_STATUSES
        )
        normalized.append((operation, status, completed, past_due))

    total = len(normalized)
    completed_total = sum(1 for _, _, done, _ in normalized if done)
    active_total = sum(1 for _, status, done, _ in normalized if not done and status in ACTIVE_STATUSES)
    pending_total = sum(1 for _, status, done, _ in normalized if not done and status in PENDING_STATUSES)
    overdue_total = sum(1 for _, _, done, past_due in normalized if not done and past_due)
    people_count = len(members)
    supervisors = sum(1 for row in members if str(row["role"]).lower() == "supervisor")
    employees = sum(1 for row in members if str(row["role"]).lower() == "employee")
    completion_rate = round((completed_total / total) * 100, 1) if total else None
    capacity_status = None
    if team.capacity is not None:
        capacity_status = "over_capacity" if people_count > team.capacity else "within_capacity"

    serialized_operations = []
    for operation, status, completed, past_due in normalized:
        serialized_operations.append({
            "id": operation.id, "title": operation.title, "description": operation.description,
            "task_type": operation.task_type, "status": operation.status,
            "team_id": operation.team_id, "location_id": operation.location_id,
            "zone_id": operation.zone_id, "assigned_to_id": operation.assigned_to_id,
            "due_at": operation.due_at.isoformat() if operation.due_at else None,
            "created_at": operation.created_at.isoformat() if operation.created_at else None,
            "updated_at": operation.updated_at.isoformat() if operation.updated_at else None,
            "completed": completed, "overdue_calculated": past_due,
        })

    trend = []
    week_start = (now - timedelta(weeks=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start -= timedelta(days=week_start.weekday())
    for index in range(8):
        start = week_start + timedelta(weeks=index)
        end = start + timedelta(weeks=1)
        created = [
            op for op, _, _, _ in normalized
            if _dt_utc(op.created_at) and start <= _dt_utc(op.created_at) < end
        ]
        completed_in_week = [
            op for op, _, done, _ in normalized
            if done and _dt_utc(op.reviewed_at or op.updated_at or op.created_at)
            and start <= _dt_utc(op.reviewed_at or op.updated_at or op.created_at) < end
        ]
        due_completed = []
        for op in completed_in_week:
            due = _dt_utc(op.due_at)
            if due is not None:
                completed_at = _dt_utc(op.reviewed_at or op.updated_at or op.created_at)
                due_completed.append((due, completed_at))
        on_time = sum(1 for due, completed_at in due_completed if completed_at and completed_at <= due)
        trend.append({
            "week_start": start.date().isoformat(),
            "tasks_created": len(created),
            "tasks_completed": len(completed_in_week),
            "compliance_rate": round(on_time / len(due_completed) * 100, 1) if due_completed else None,
            "due_dated_completions": len(due_completed),
        })

    team_summary = {
        **team.to_dict(), "supervisor_count": supervisors, "employee_count": employees,
        "member_count": people_count, "location_count": len(locations), "zone_count": len(zones),
        "task_count": total, "completed_task_count": completed_total, "active_task_count": active_total,
        "pending_task_count": pending_total, "overdue_task_count": overdue_total,
        "completion_rate": completion_rate, "capacity_status": capacity_status,
    }
    try:
        purge_expired_audit_logs()
    except Exception:
        db.session.rollback()
    audit_rows = AuditLog.query.filter(
        AuditLog.organization_id == admin.organization_id,
        AuditLog.resource_type == "Team",
        AuditLog.resource_id == str(team.id),
        AuditLog.created_at >= audit_cutoff(),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(100).all()
    activity = [{
        **row.to_dict(), "actor_name": row.actor.full_name if row.actor else "System",
        "event_source": "team",
    } for row in audit_rows]
    operation_ids = [item["id"] for item in serialized_operations]
    if operation_ids:
        operation_history = (
            OperationLog.query.filter(OperationLog.operation_id.in_(operation_ids))
            .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
            .limit(100).all()
        )
        for row in operation_history:
            activity.append({
                "id": row.id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "actor_name": row.actor.full_name if row.actor else "System",
                "action": "task." + str(row.action),
                "resource_type": "Operation",
                "resource_id": str(row.operation_id),
                "event_source": "task",
                "details": {
                    "task_title": row.operation.title if row.operation else "Task",
                    "operation_id": row.operation_id,
                    "previous_status": row.previous_status,
                    "new_status": row.new_status,
                    "message": row.message,
                },
            })
    activity.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    activity = activity[:100]

    payload = {
        "team": team_summary, "members": members,
        "locations": [row.to_dict() for row in locations],
        "zones": [row.to_dict() for row in zones],
        "operations": serialized_operations, "activity": activity, "performance": trend,
    }
    if include_directory:
        users = User.query.filter(
            User.organization_id == admin.organization_id, User.account_status == "active",
            User.role.in_(["Supervisor", "Employee"]),
        ).order_by(func.lower(User.full_name)).all()
        assignment_by_user = {}
        assignment_rows = (
            TeamMember.query.join(Team, TeamMember.team_id == Team.id)
            .filter(Team.organization_id == admin.organization_id).all()
        )
        for membership in assignment_rows:
            assignment_by_user.setdefault(membership.user_id, []).append(membership.team_id)
        payload["available_members"] = [{
            "id": user.id, "name": user.full_name, "email": user.email, "role": user.role,
            "assigned_team_ids": assignment_by_user.get(user.id, []),
        } for user in users]
        payload["available_locations"] = [
            row.to_dict() for row in Location.query.filter_by(
                organization_id=admin.organization_id
            ).order_by(Location.name.asc()).all()
        ]
        payload["available_zones"] = [
            row.to_dict() for row in Zone.query.filter_by(
                organization_id=admin.organization_id
            ).order_by(Zone.name.asc()).all()
        ]
    return payload


@teams_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def teams_collection():
    admin = get_current_user()
    if request.method == "GET":
        organization_teams = Team.query.filter_by(
            organization_id=admin.organization_id
        ).order_by(Team.name.asc()).all()
        # Keep the summary collection backwards-compatible while exposing
        # real activity, workload and eight-week trend data for the Admin overview.
        insights = [_team_data(team, admin) for team in organization_teams]
        return jsonify({
            "teams": [item["team"] for item in insights],
            "insights": [{
                "team_id": item["team"]["id"],
                "activity": item["activity"][:12],
                "performance": item["performance"],
                "operations": item["operations"],
            } for item in insights],
        }), 200

    data = request.get_json(silent=True) or {}
    name, description, capacity = data.get("name"), data.get("description", ""), data.get("capacity")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Team name is required."}), 400
    name = name.strip()
    if len(name) > 120:
        return jsonify({"error": "Team name must be 120 characters or fewer."}), 400
    if not isinstance(description, str):
        return jsonify({"error": "Description must be text."}), 400
    if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1):
        return jsonify({"error": "Capacity must be a positive whole number or omitted."}), 400
    description = description.strip()
    duplicate = Team.query.filter(
        Team.organization_id == admin.organization_id, func.lower(Team.name) == name.lower()
    ).first()
    if duplicate:
        return jsonify({"error": "A team with this name already exists."}), 409
    team = Team(name=name, description=description or None, organization_id=admin.organization_id, capacity=capacity)
    try:
        db.session.add(team)
        db.session.flush()
        _audit(admin, team, "team.created", {
            "name": name, "description": description,
            "capacity": capacity, "status": "active",
        })
        # Notifications are helpful but must never block team creation.
        # Isolate them in a savepoint so a notification issue rolls back only
        # the notification writes, not the team or its audit record.
        try:
            with db.session.begin_nested():
                _notify_admins(
                    admin, team, "Team created",
                    f'{admin.full_name} created the team "{team.name}".',
                )
        except Exception:
            from flask import current_app
            current_app.logger.exception("Could not create team notifications for team %s", team.id)
        team_payload = team.to_dict()
        db.session.commit()
    except Exception:
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception("Team creation failed for organization %s", admin.organization_id)
        return jsonify({"error": "Team could not be created. Check the team details and try again."}), 500
    return jsonify({"message": "Team created successfully.", "team": team_payload}), 201


@teams_bp.route("/<int:team_id>", methods=["GET", "PATCH"])
@roles_required("Admin")
def team_detail(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if request.method == "GET":
        return jsonify(_team_data(team, admin, include_directory=True)), 200
    if team.status == "archived":
        return jsonify({"error": "Restore this team before editing it."}), 423
    data = request.get_json(silent=True) or {}
    changes = {}
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            return jsonify({"error": "Team name must contain 1–120 characters."}), 400
        name = name.strip()
        duplicate = Team.query.filter(
            Team.organization_id == admin.organization_id, Team.id != team.id,
            func.lower(Team.name) == name.lower(),
        ).first()
        if duplicate:
            return jsonify({"error": "A team with this name already exists."}), 409
        if name != team.name:
            changes["name"] = {"before": team.name, "after": name}
            team.name = name
    if "description" in data:
        description = data["description"]
        if not isinstance(description, str):
            return jsonify({"error": "Description must be text."}), 400
        description = description.strip() or None
        if description != team.description:
            changes["description"] = {"before": team.description, "after": description}
            team.description = description
    if "capacity" in data:
        capacity = data["capacity"]
        if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1):
            return jsonify({"error": "Capacity must be a positive whole number or null."}), 400
        if capacity != team.capacity:
            changes["capacity"] = {"before": team.capacity, "after": capacity}
            team.capacity = capacity
    if "whatsapp_group_url" in data:
        raw_url = data["whatsapp_group_url"]
        if raw_url is not None and not isinstance(raw_url, str):
            return jsonify({"error": "WhatsApp group invite URL must be text."}), 400
        group_url = (raw_url or "").strip() or None
        if group_url:
            parsed = urlparse(group_url)
            if (
                len(group_url) > 500
                or parsed.scheme != "https"
                or parsed.hostname != "chat.whatsapp.com"
                or not parsed.path.strip("/")
                or parsed.username
                or parsed.password
            ):
                return jsonify({"error": "Use a valid invite URL beginning with https://chat.whatsapp.com/."}), 400
        if group_url != team.whatsapp_group_url:
            changes["whatsapp_group_url"] = {"before": team.whatsapp_group_url, "after": group_url}
            team.whatsapp_group_url = group_url
    if changes:
        _audit(admin, team, "team.updated", changes)
        _notify_admins(admin, team, "Team updated", f'{admin.full_name} updated "{team.name}".')
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "Team could not be updated."}), 500
    return jsonify({"message": "Team updated.", **_team_data(team, admin)}), 200


@teams_bp.post("/<int:team_id>/archive")
@roles_required("Admin")
def archive_team(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if team.status == "archived":
        return jsonify({"message": "Team is already archived.", "team": team.to_dict()}), 200
    team.status = "archived"
    _audit(admin, team, "team.archived", {"name": team.name, "message": "Archived; historical records were retained."})
    _notify_admins(admin, team, "Team archived", f'{admin.full_name} archived "{team.name}". Historical records were retained.')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team could not be archived."}), 500
    return jsonify({"message": "Team archived. Historical records were retained.", "team": team.to_dict()}), 200


@teams_bp.post("/<int:team_id>/restore")
@roles_required("Admin")
def restore_team(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if team.status == "active":
        return jsonify({"message": "Team is already active.", "team": team.to_dict()}), 200
    team.status = "active"
    _audit(admin, team, "team.restored", {"name": team.name})
    _notify_admins(admin, team, "Team restored", f'{admin.full_name} restored "{team.name}".')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team could not be restored."}), 500
    return jsonify({"message": "Team restored.", "team": team.to_dict()}), 200


@teams_bp.put("/<int:team_id>/members")
@roles_required("Admin")
def assign_members(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if team.status == "archived":
        return jsonify({"error": "Restore this team before assigning people."}), 423
    data = request.get_json(silent=True) or {}
    member_ids = data.get("member_ids")
    if not isinstance(member_ids, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in member_ids):
        return jsonify({"error": "member_ids must be a list of user IDs."}), 400
    if len(set(member_ids)) != len(member_ids):
        return jsonify({"error": "A user can only appear once in the assignment list."}), 400
    users = User.query.filter(
        User.id.in_(member_ids) if member_ids else User.id == -1,
        User.organization_id == admin.organization_id, User.account_status == "active",
        User.role.in_(["Supervisor", "Employee"]),
    ).all()
    if len(users) != len(member_ids):
        return jsonify({"error": "Every selected person must be an active Supervisor or Employee in your organization."}), 400
    previous = {row.user_id: row for row in TeamMember.query.filter_by(team_id=team.id).all()}
    selected = set(member_ids)
    for user in users:
        if user.role == "Supervisor":
            elsewhere = TeamMember.query.join(Team, TeamMember.team_id == Team.id).filter(
                TeamMember.user_id == user.id, TeamMember.team_id != team.id,
                Team.organization_id == admin.organization_id,
            ).first()
            if elsewhere:
                other_team = db.session.get(Team, elsewhere.team_id)
                return jsonify({"error": f'{user.full_name} is already assigned to "{other_team.name if other_team else "another team"}". Supervisors can only belong to one team.'}), 409
    current_ids = set(previous)
    to_add = [user for user in users if user.id not in current_ids]
    to_remove = [previous[user_id] for user_id in current_ids - selected]
    try:
        for membership in to_remove:
            user = db.session.get(User, membership.user_id)
            db.session.delete(membership)
            _audit(admin, team, "team.member_removed", {
                "member_id": membership.user_id, "member_name": user.full_name if user else "Unknown",
                "role": user.role if user else None,
            })
            _notify_admins(admin, team, "Team member removed", f'{user.full_name if user else "A member"} was removed from team "{team.name}".')
        for user in to_add:
            db.session.add(TeamMember(team_id=team.id, user_id=user.id))
            _audit(admin, team, "team.member_assigned", {
                "member_id": user.id, "member_name": user.full_name, "role": user.role, "team_name": team.name,
            })
            _notify_admins(admin, team, "Team member assigned", f'{user.role} {user.full_name} was added to team "{team.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team membership could not be updated."}), 500
    return jsonify({"message": "Team membership updated.", **_team_data(team, admin, include_directory=True)}), 200


@teams_bp.put("/<int:team_id>/locations")
@roles_required("Admin")
def assign_locations(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if team.status == "archived":
        return jsonify({"error": "Restore this team before assigning locations."}), 423
    data = request.get_json(silent=True) or {}
    ids = data.get("location_ids")
    if not isinstance(ids, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in ids):
        return jsonify({"error": "location_ids must be a list of location IDs."}), 400
    if len(ids) != len(set(ids)):
        return jsonify({"error": "Locations cannot be repeated."}), 400
    locations = Location.query.filter(
        Location.organization_id == admin.organization_id,
        Location.id.in_(ids) if ids else Location.id == -1,
    ).all()
    if len(locations) != len(ids):
        return jsonify({"error": "One or more selected locations are outside your organization or unavailable."}), 400
    currently = Location.query.filter_by(team_id=team.id, organization_id=admin.organization_id).all()
    target = set(ids)
    changes = []
    for row in currently:
        if row.id not in target:
            row.team_id = None
            changes.append({"location_id": row.id, "location_name": row.name, "before_team_id": team.id, "after_team_id": None})
    for row in locations:
        if row.team_id != team.id:
            old_id = row.team_id
            row.team_id = team.id
            changes.append({"location_id": row.id, "location_name": row.name, "before_team_id": old_id, "after_team_id": team.id})
            if old_id:
                old_team = _team_or_404(old_id, admin)
                if old_team:
                    _audit(admin, old_team, "team.location_reassigned", {"location_id": row.id, "location_name": row.name, "before_team_id": old_id, "after_team_id": team.id})
            # A zone explicitly assigned to the previous team follows its
            # location when an Admin moves that location to another team.
            # Zone records and operation history remain intact.
            for zone in Zone.query.filter_by(
                organization_id=admin.organization_id, location_id=row.id
            ).all():
                if zone.team_id and zone.team_id != team.id:
                    previous_zone_team_id = zone.team_id
                    zone.team_id = team.id
                    zone_change = {
                        "zone_id": zone.id, "zone_name": zone.name,
                        "before_team_id": previous_zone_team_id, "after_team_id": team.id,
                        "reason": "parent location reassigned",
                    }
                    changes.append(zone_change)
                    previous_zone_team = _team_or_404(previous_zone_team_id, admin)
                    if previous_zone_team:
                        _audit(admin, previous_zone_team, "team.zone_reassigned", zone_change)
    if changes:
        _audit(admin, team, "team.locations_updated", {"changes": changes})
        _notify_admins(admin, team, "Team locations updated", f'{admin.full_name} updated locations assigned to "{team.name}".')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team locations could not be updated."}), 500
    return jsonify({"message": "Team locations updated.", **_team_data(team, admin, include_directory=True)}), 200


@teams_bp.put("/<int:team_id>/zones")
@roles_required("Admin")
def assign_zones(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    if team.status == "archived":
        return jsonify({"error": "Restore this team before assigning zones."}), 423
    data = request.get_json(silent=True) or {}
    ids = data.get("zone_ids")
    if not isinstance(ids, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in ids):
        return jsonify({"error": "zone_ids must be a list of zone IDs."}), 400
    if len(ids) != len(set(ids)):
        return jsonify({"error": "Zones cannot be repeated."}), 400
    zones = Zone.query.filter(
        Zone.organization_id == admin.organization_id,
        Zone.id.in_(ids) if ids else Zone.id == -1,
    ).all()
    if len(zones) != len(ids):
        return jsonify({"error": "One or more selected zones are outside your organization or unavailable."}), 400
    locations = {row.id: row for row in Location.query.filter_by(organization_id=admin.organization_id).all()}
    for zone in zones:
        loc = locations.get(zone.location_id)
        if loc and loc.team_id not in (None, team.id):
            return jsonify({"error": f'Zone "{zone.name}" belongs to a location assigned to another team. Reassign that location first.'}), 409
    currently = Zone.query.filter_by(team_id=team.id, organization_id=admin.organization_id).all()
    target = set(ids)
    changes = []
    for row in currently:
        if row.id not in target:
            row.team_id = None
            changes.append({"zone_id": row.id, "zone_name": row.name, "before_team_id": team.id, "after_team_id": None})
    for row in zones:
        if row.team_id != team.id:
            old_id = row.team_id
            row.team_id = team.id
            changes.append({"zone_id": row.id, "zone_name": row.name, "before_team_id": old_id, "after_team_id": team.id})
            if old_id:
                old_team = _team_or_404(old_id, admin)
                if old_team:
                    _audit(admin, old_team, "team.zone_reassigned", {"zone_id": row.id, "zone_name": row.name, "before_team_id": old_id, "after_team_id": team.id})
    if changes:
        _audit(admin, team, "team.zones_updated", {"changes": changes})
        _notify_admins(admin, team, "Team zones updated", f'{admin.full_name} updated zones assigned to "{team.name}".')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team zones could not be updated."}), 500
    return jsonify({"message": "Team zones updated.", **_team_data(team, admin, include_directory=True)}), 200


def _export_rows(payload):
    team = payload["team"]
    rows = [["Section", "Field", "Value"]]
    summary_fields = (
        "id", "name", "description", "status", "capacity", "member_count", "supervisor_count",
        "employee_count", "location_count", "zone_count", "task_count", "completed_task_count",
        "active_task_count", "pending_task_count", "overdue_task_count", "completion_rate",
        "capacity_status", "created_at",
    )
    for field in summary_fields:
        rows.append(["Team summary", field.replace("_", " ").title(), team.get(field)])
    sections = (
        ("People", payload["members"], ("name", "email", "role", "account_status", "assigned_at")),
        ("Locations", payload["locations"], ("name", "address", "status", "created_at")),
        ("Zones", payload["zones"], ("name", "location_id", "team_id", "status", "created_at")),
        ("Tasks", payload["operations"], ("id", "title", "task_type", "status", "assigned_to_id", "location_id", "zone_id", "due_at", "created_at", "updated_at", "overdue_calculated")),
        ("Performance", payload["performance"], ("week_start", "tasks_created", "tasks_completed", "compliance_rate", "due_dated_completions")),
        ("Activity history", payload["activity"], ("created_at", "actor_name", "action", "details")),
    )
    for section, entries, keys in sections:
        rows.append([section, "", ""])
        for entry in entries:
            rows.append([section, " | ".join(str(entry.get(key) or "") for key in keys), ""])
    return [["" if value is None else str(value) for value in row] for row in rows]


def _xlsx_bytes(rows):
    def col_name(number):
        name = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            name = chr(65 + remainder) + name
        return name
    sheet_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(row, 1):
            cells.append(f'<c r="{col_name(column_index)}{row_index}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>'
    content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    root_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Team report" sheetId="1" r:id="rId1"/></sheets></workbook>'
    workbook_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_rels)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def _pdf_bytes(lines):
    pages = [lines[index:index + 48] for index in range(0, max(len(lines), 1), 48)]
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids = [4 + index * 2 for index in range(len(pages))]
    objects[2] = ("<< /Type /Pages /Kids [" + " ".join(f"{page_id} 0 R" for page_id in page_ids) + f"] /Count {len(pages)} >>").encode("ascii")
    for index, page_lines in enumerate(pages):
        page_id, content_id = 4 + index * 2, 5 + index * 2
        commands = ["BT", "/F1 10 Tf", "50 760 Td", "14 TL"]
        for line in page_lines:
            safe = str(line).encode("latin-1", "replace").decode("latin-1")
            safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.extend([f"({safe[:170]}) Tj", "T*"])
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        objects[page_id] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id] + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii"))
    return bytes(output)


@teams_bp.get("/<int:team_id>/export")
@roles_required("Admin")
def export_team(team_id):
    admin = get_current_user()
    team = _team_or_404(team_id, admin)
    if team is None:
        return jsonify({"error": "Team not found in your organization."}), 404
    file_format = (request.args.get("format") or "csv").lower()
    if file_format not in {"csv", "xlsx", "pdf"}:
        return jsonify({"error": "Choose CSV, XLSX, or PDF."}), 400
    rows = _export_rows(_team_data(team, admin))
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", team.name).strip("-") or f"team-{team.id}"
    if file_format == "csv":
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        filename = f"{slug}-team-report.csv"
    elif file_format == "xlsx":
        response = make_response(_xlsx_bytes(rows))
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{slug}-team-report.xlsx"
    else:
        lines = [f"CleanTrack | Team report: {team.name}", f"Generated: {datetime.now(timezone.utc).isoformat()}", ""] + [" | ".join(row) for row in rows]
        response = make_response(_pdf_bytes(lines))
        response.headers["Content-Type"] = "application/pdf"
        filename = f"{slug}-team-report.pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response, 200
