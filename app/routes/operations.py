from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from io import BytesIO
from uuid import uuid4
from mimetypes import guess_type

from flask import Blueprint, current_app, jsonify, request, has_request_context, send_file
from sqlalchemy import func, or_

from app.extensions import db
from app.models.operation import Operation
from app.models.operation_execution import OperationExecution
from app.models.operation_log import OperationLog
from app.models.audit_log import AuditLog
from app.services.audit_retention import audit_cutoff, purge_expired_audit_logs
from app.models.user import User
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.location import Location
from app.models.zone import Zone
from app.models.task_evidence import TaskEvidence
from app.models.notification import Notification
from app.services.operation_scheduler import next_occurrence
from werkzeug.utils import secure_filename
from app.services.permissions import get_current_user, roles_required


operations_bp = Blueprint("operations", __name__, url_prefix="/api/operations")
VALID_PRIORITIES = {"Low", "Normal", "High", "Urgent"}
VALID_SCHEDULE_TYPES = {"manual", "one_time", "recurring"}
CLOSED_STATUSES = {"completed", "approved", "cancelled", "canceled"}


def _team_ids(user):
    return {
        row[0] for row in db.session.query(TeamMember.team_id)
        .filter(TeamMember.user_id == user.id).all()
    }


def _scoped_operation_query(user):
    query = Operation.query.filter_by(organization_id=user.organization_id)
    if user.role == "Admin":
        return query
    if user.role == "Employee":
        return query.filter(Operation.assigned_to_id == user.id)

    team_ids = _team_ids(user)
    rules = [
        Operation.assigned_to_id == user.id,
        Operation.assigned_supervisor_id == user.id,
    ]
    if team_ids:
        rules.append(Operation.team_id.in_(team_ids))
        location_ids = [
            row[0] for row in db.session.query(Location.id)
            .filter(Location.organization_id == user.organization_id, Location.team_id.in_(team_ids)).all()
        ]
        zone_ids = [
            row[0] for row in db.session.query(Zone.id)
            .filter(Zone.organization_id == user.organization_id, or_(
                Zone.team_id.in_(team_ids),
                Zone.location_id.in_(location_ids) if location_ids else Zone.id == -1,
            )).all()
        ]
        if location_ids:
            rules.append(Operation.location_id.in_(location_ids))
        if zone_ids:
            rules.append(Operation.zone_id.in_(zone_ids))
    return query.filter(or_(*rules))


def _can_manage(operation, user):
    if user.role == "Admin":
        return True
    if user.role != "Supervisor":
        return False
    if operation.assigned_supervisor_id == user.id or operation.assigned_to_id == user.id:
        return True
    team_ids = _team_ids(user)
    if operation.team_id is not None and operation.team_id in team_ids:
        return True
    if not team_ids:
        return False
    location_ids = {
        row[0] for row in db.session.query(Location.id).filter(
            Location.organization_id == user.organization_id,
            Location.team_id.in_(team_ids),
        ).all()
    }
    if operation.location_id is not None and operation.location_id in location_ids:
        return True
    zone_ids = {
        row[0] for row in db.session.query(Zone.id).filter(
            Zone.organization_id == user.organization_id,
            or_(
                Zone.team_id.in_(team_ids),
                Zone.location_id.in_(location_ids) if location_ids else Zone.id == -1,
            ),
        ).all()
    }
    return operation.zone_id is not None and operation.zone_id in zone_ids


def _visible(operation, user):
    if operation is None or operation.organization_id != user.organization_id:
        return False
    if user.role == "Admin":
        return True
    if user.role == "Employee":
        return operation.assigned_to_id == user.id
    return _can_manage(operation, user)


def _log(operation, actor, action, previous_status, message=None, details=None):
    db.session.add(OperationLog(
        operation_id=operation.id,
        actor_id=actor.id,
        action=action,
        previous_status=previous_status,
        new_status=operation.status,
        message=message,
    ))
    audit_details = {
        "title": operation.title,
        "previous_status": previous_status,
        "new_status": operation.status,
        "message": message,
    }
    if details:
        audit_details.update(details)
    db.session.add(AuditLog(
        actor_id=actor.id,
        organization_id=operation.organization_id,
        action="operation." + action,
        resource_type="Operation",
        resource_id=str(operation.id),
        details=audit_details,
        ip_address=request.remote_addr if has_request_context() else None,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None) if has_request_context() else None,
    ))


def _operation_is_overdue(operation, now=None):
    now = now or datetime.now(timezone.utc)
    due = operation.due_at
    if due is None:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    else:
        due = due.astimezone(timezone.utc)
    status = str(operation.status or "").strip().lower()
    if status in {"cancelled", "canceled"}:
        return False
    if status in {"submitted", "completed", "approved", "verified"}:
        submitted = operation.submitted_at
        if submitted is None:
            return False
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        else:
            submitted = submitted.astimezone(timezone.utc)
        return submitted > due
    if status in CLOSED_STATUSES:
        return False
    return due < now


def _parse_id(data, field):
    value = data.get(field)
    if value in (None, ""):
        return None, None
    if isinstance(value, bool):
        return None, field + " must be an integer."
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return None, field + " must be an integer."
    if parsed <= 0:
        return None, field + " must be a positive integer."
    return parsed, None


def _parse_datetime(value, field):
    if value in (None, ""):
        return None, None
    if not isinstance(value, str):
        return None, field + " must be an ISO date/time string."
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, field + " is not a valid ISO date/time."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, None


def _parse_list(data, field, max_items=40, max_length=500):
    value = data.get(field)
    if value is None:
        return None, None
    if isinstance(value, str):
        value = [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list):
        return None, field + " must be a list."
    if len(value) > max_items or any(not isinstance(item, str) or not item.strip() or len(item.strip()) > max_length for item in value):
        return None, field + " contains invalid or too many entries."
    return [item.strip() for item in value], None


def _team_context(team_id, location_id, zone_id, user, allow_area_only=False):
    team = db.session.get(Team, team_id) if team_id else None
    location = db.session.get(Location, location_id) if location_id else None
    zone = db.session.get(Zone, zone_id) if zone_id else None
    if team_id and (team is None or team.organization_id != user.organization_id or team.status != "active"):
        return "Team not found or inactive in this organization."
    if location_id and (location is None or location.organization_id != user.organization_id or location.status != "active"):
        return "Location not found or inactive in this organization."
    if zone_id and (zone is None or zone.organization_id != user.organization_id or zone.status != "active"):
        return "Zone not found or inactive in this organization."
    if location and team_id and location.team_id not in (None, team_id):
        return "Location does not belong to the selected team."
    if zone and (location is None or zone.location_id != location.id):
        return "Zone must belong to the selected location."
    if zone and team_id and zone.team_id not in (None, team_id):
        return "Zone does not belong to the selected team."
    if user.role == "Supervisor":
        allowed = _team_ids(user)
        if team_id:
            if team_id not in allowed:
                return "Supervisors can only manage operations in their assigned teams."
        elif not allow_area_only:
            return "Supervisors must choose one of their assigned teams when creating an operation."
        else:
            allowed_locations = {
                row[0] for row in db.session.query(Location.id).filter(
                    Location.organization_id == user.organization_id,
                    Location.team_id.in_(allowed),
                ).all()
            } if allowed else set()
            allowed_zones = {
                row[0] for row in db.session.query(Zone.id).filter(
                    Zone.organization_id == user.organization_id,
                    or_(
                        Zone.team_id.in_(allowed),
                        Zone.location_id.in_(allowed_locations) if allowed_locations else Zone.id == -1,
                    ),
                ).all()
            } if allowed else set()
            if location_id not in allowed_locations and zone_id not in allowed_zones:
                return "Supervisors can only manage operations inside their assigned teams, locations, or zones."
    return None


@operations_bp.route("", methods=["GET", "POST"])
@roles_required("Admin", "Supervisor", "Employee")
def operations_collection():
    user = get_current_user()
    if request.method == "GET":
        query = _scoped_operation_query(user)
        status = request.args.get("status", "").strip()
        category = request.args.get("category", "").strip()
        priority = request.args.get("priority", "").strip()
        search = request.args.get("q", "").strip()
        supervisor_filter = request.args.get("supervisor_id")
        if supervisor_filter:
            if user.role != "Admin":
                return jsonify({"error": "Only an Admin can open an organization-wide supervisor schedule."}), 403
            try:
                supervisor_id = int(supervisor_filter)
            except ValueError:
                return jsonify({"error": "supervisor_id must be an integer."}), 400
            supervisor = User.query.filter_by(
                id=supervisor_id, organization_id=user.organization_id,
                role="Supervisor", account_status="active",
            ).first()
            if supervisor is None:
                return jsonify({"error": "Supervisor not found in this organization."}), 404
            team_ids = [
                row[0] for row in db.session.query(TeamMember.team_id)
                .filter(TeamMember.user_id == supervisor.id).all()
            ]
            clauses = [
                Operation.assigned_supervisor_id == supervisor.id,
                Operation.assigned_to_id == supervisor.id,
            ]
            if team_ids:
                clauses.append(Operation.team_id.in_(team_ids))
                location_ids = [
                    row[0] for row in db.session.query(Location.id).filter(
                        Location.organization_id == user.organization_id,
                        Location.team_id.in_(team_ids),
                    ).all()
                ]
                zone_clauses = [Zone.team_id.in_(team_ids)]
                if location_ids:
                    clauses.append(Operation.location_id.in_(location_ids))
                    zone_clauses.append(Zone.location_id.in_(location_ids))
                zone_ids = [
                    row[0] for row in db.session.query(Zone.id).filter(
                        Zone.organization_id == user.organization_id, or_(*zone_clauses)
                    ).all()
                ]
                if zone_ids:
                    clauses.append(Operation.zone_id.in_(zone_ids))
            query = query.filter(or_(*clauses))
        for param, column in [
            ("team_id", Operation.team_id),
            ("location_id", Operation.location_id),
            ("zone_id", Operation.zone_id),
            ("assigned_to_id", Operation.assigned_to_id),
        ]:
            value = request.args.get(param)
            if value:
                try:
                    query = query.filter(column == int(value))
                except ValueError:
                    return jsonify({"error": param + " must be an integer."}), 400
        if status:
            query = query.filter(func.lower(Operation.status) == status.lower())
        if category:
            query = query.filter(func.lower(Operation.task_type) == category.lower())
        if priority:
            query = query.filter(func.lower(Operation.priority) == priority.lower())
        if search:
            query = query.filter(or_(
                Operation.title.ilike("%" + search[:100] + "%"),
                Operation.description.ilike("%" + search[:100] + "%"),
            ))
        now = datetime.now(timezone.utc)
        items = query.order_by(Operation.created_at.desc()).all()
        operation_ids = [item.id for item in items]
        latest_execution = {}
        if operation_ids:
            executions = OperationExecution.query.filter(
                OperationExecution.operation_id.in_(operation_ids)
            ).order_by(OperationExecution.attempt_number.desc(), OperationExecution.created_at.desc()).all()
            for execution in executions:
                latest_execution.setdefault(execution.operation_id, execution)
        operation_rows = []
        for item in items:
            item.overdue = _operation_is_overdue(item, now)
            row = item.to_dict()
            attempt = latest_execution.get(item.id)
            if attempt:
                row["started_at"] = attempt.started_at.isoformat() if attempt.started_at else None
                row["execution_submitted_at"] = attempt.submitted_at.isoformat() if attempt.submitted_at else None
                row["execution_status"] = attempt.status
                row["time_taken_minutes"] = (
                    max(0, round((attempt.submitted_at - attempt.started_at).total_seconds() / 60))
                    if attempt.started_at and attempt.submitted_at else None
                )
            operation_rows.append(row)
        return jsonify({"operations": operation_rows, "count": len(operation_rows)}), 200

    if user.role not in {"Admin", "Supervisor"}:
        return jsonify({"error": "Employees can execute assigned work but cannot create organization operations."}), 403

    data = request.get_json(silent=True) or {}
    title = data.get("title", data.get("name", ""))
    description = data.get("description", "")
    category = data.get("category", data.get("task_type", "General Operations"))
    priority = data.get("priority", "Normal")
    instructions = data.get("instructions", "")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        return jsonify({"error": "Operation name is required and must be 200 characters or fewer."}), 400
    if not isinstance(description, str) or len(description) > 10000:
        return jsonify({"error": "Description must be text of at most 10,000 characters."}), 400
    if not isinstance(category, str) or not category.strip() or len(category.strip()) > 100:
        return jsonify({"error": "A valid operation category is required."}), 400
    if not isinstance(priority, str) or priority.strip().title() not in VALID_PRIORITIES:
        return jsonify({"error": "Priority must be Low, Normal, High, or Urgent."}), 400
    if not isinstance(instructions, str) or len(instructions) > 10000:
        return jsonify({"error": "Instructions must be text of at most 10,000 characters."}), 400

    team_id, error = _parse_id(data, "team_id")
    if error:
        return jsonify({"error": error}), 400
    location_id, error = _parse_id(data, "location_id")
    if error:
        return jsonify({"error": error}), 400
    zone_id, error = _parse_id(data, "zone_id")
    if error:
        return jsonify({"error": error}), 400
    assigned_to_id, error = _parse_id(data, "assigned_to_id")
    if error:
        return jsonify({"error": error}), 400
    due_at, error = _parse_datetime(data.get("due_at"), "due_at")
    if error:
        return jsonify({"error": error}), 400

    scope_error = _team_context(team_id, location_id, zone_id, user)
    if scope_error:
        return jsonify({"error": scope_error}), 400

    assignee = db.session.get(User, assigned_to_id) if assigned_to_id else None
    if assigned_to_id and (
        assignee is None or assignee.organization_id != user.organization_id
        or assignee.account_status != "active" or assignee.role not in {"Supervisor", "Employee"}
    ):
        return jsonify({"error": "Assignee must be an active Supervisor or Employee in this organization."}), 400
    if user.role == "Supervisor" and assignee and assignee.role != "Employee" and assignee.id != user.id:
        return jsonify({"error": "Supervisors can assign work only to themselves or employees in their team."}), 403
    if assignee and team_id and assignee.id != user.id:
        membership = TeamMember.query.filter_by(team_id=team_id, user_id=assignee.id).first()
        if not membership and user.role == "Supervisor":
            return jsonify({"error": "The assignee must be a member of the selected team."}), 400

    checklist, error = _parse_list(data, "checklist", max_items=40, max_length=500)
    if error:
        return jsonify({"error": error}), 400
    required_evidence, error = _parse_list(data, "required_evidence", max_items=10, max_length=80)
    if error:
        return jsonify({"error": error}), 400
    estimated = data.get("estimated_minutes")
    if estimated in ("", None):
        estimated = None
    else:
        try:
            estimated = int(estimated)
            if estimated < 1 or estimated > 10080:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Estimated time must be between 1 minute and 7 days."}), 400
    requires_verification = data.get("requires_verification", True)
    if not isinstance(requires_verification, bool):
        return jsonify({"error": "requires_verification must be true or false."}), 400
    schedule_type = data.get("schedule_type", "one_time")
    if schedule_type not in VALID_SCHEDULE_TYPES:
        return jsonify({"error": "Schedule type must be manual, one_time, or recurring."}), 400
    schedule_timezone = data.get("schedule_timezone", "UTC")
    if not isinstance(schedule_timezone, str) or not schedule_timezone.strip() or len(schedule_timezone) > 80:
        return jsonify({"error": "A valid schedule time zone is required."}), 400
    schedule_timezone = schedule_timezone.strip()
    try:
        ZoneInfo(schedule_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": "Choose a valid IANA time zone, such as Asia/Kolkata or Europe/London."}), 400
    recurrence_rule = data.get("recurrence_rule")
    if schedule_type == "recurring":
        if not isinstance(recurrence_rule, dict) or recurrence_rule.get("frequency") not in {"daily", "weekly", "monthly"}:
            return jsonify({"error": "Recurring operations require a daily, weekly, or monthly recurrence rule."}), 400
        try:
            interval = int(recurrence_rule.get("interval", 1))
            if not 1 <= interval <= 365:
                raise ValueError
            catch_up_policy = str(recurrence_rule.get("catch_up_policy", "latest_only")).lower()
            if catch_up_policy not in {"latest_only", "skip", "limited"}:
                raise ValueError
            catch_up_window_days = int(recurrence_rule.get("catch_up_window_days", 7))
            if not 0 <= catch_up_window_days <= 365:
                raise ValueError
            recurrence_rule = {
                **recurrence_rule,
                "interval": interval,
                "catch_up_policy": catch_up_policy,
                "catch_up_window_days": catch_up_window_days,
            }
        except (TypeError, ValueError):
            return jsonify({"error": "Use a valid interval and catch-up policy (latest_only, skip, or limited, with a 0–365 day catch-up window)."}), 400
        if due_at is None:
            return jsonify({"error": "Recurring operations require the first due date and time."}), 400

    def bounded_minutes(field, default, maximum):
        value = data.get(field, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if 0 <= value <= maximum else None

    reminder_before_minutes = bounded_minutes("reminder_before_minutes", 60, 10080)
    escalate_after_minutes = bounded_minutes("escalate_after_minutes", 120, 525600)
    escalate_to_admin = data.get("escalate_to_admin", True)
    if reminder_before_minutes is None:
        return jsonify({"error": "Pre-deadline reminder must be between 0 minutes and 7 days."}), 400
    if escalate_after_minutes is None:
        return jsonify({"error": "Escalation delay must be between 0 minutes and 365 days."}), 400
    if not isinstance(escalate_to_admin, bool):
        return jsonify({"error": "escalate_to_admin must be true or false."}), 400

    schedule_next_run_at = None
    if schedule_type == "recurring":
        try:
            schedule_next_run_at = next_occurrence(due_at, recurrence_rule, schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return jsonify({"error": "The recurring schedule could not be calculated. Check its time zone and recurrence rule."}), 400

    assigned_supervisor_id = (
        assignee.id if assignee and assignee.role == "Supervisor"
        else (user.id if user.role == "Supervisor" else None)
    )
    if assigned_supervisor_id is None and assignee and assignee.role == "Employee" and team_id:
        team_supervisors = User.query.join(
            TeamMember, TeamMember.user_id == User.id
        ).filter(
            TeamMember.team_id == team_id,
            User.organization_id == user.organization_id,
            User.role == "Supervisor",
            User.account_status == "active",
        ).order_by(User.id.asc()).all()
        if len(team_supervisors) == 1:
            assigned_supervisor_id = team_supervisors[0].id

    operation = Operation(
        title=title.strip(),
        description=description.strip() or None,
        task_type=category.strip(),
        priority=priority.strip().title(),
        instructions=instructions.strip() or None,
        checklist=checklist or [],
        required_evidence=required_evidence or [],
        estimated_minutes=estimated,
        requires_verification=requires_verification,
        status="Assigned" if assigned_to_id else "Pending",
        organization_id=user.organization_id,
        team_id=team_id,
        location_id=location_id,
        zone_id=zone_id,
        created_by_id=user.id,
        assigned_to_id=assigned_to_id,
        original_assigned_to_id=assigned_to_id,
        assigned_supervisor_id=assigned_supervisor_id,
        due_at=due_at,
        original_due_at=due_at,
        schedule_type=schedule_type,
        recurrence_rule=recurrence_rule if schedule_type == "recurring" else None,
        schedule_timezone=schedule_timezone,
        schedule_next_run_at=schedule_next_run_at,
        reminder_before_minutes=reminder_before_minutes,
        escalate_after_minutes=escalate_after_minutes,
        escalate_to_admin=escalate_to_admin,
    )
    db.session.add(operation)
    try:
        db.session.flush()
        _log(operation, user, "assigned" if assigned_to_id else "created", None,
             "Operation created and assigned" if assigned_to_id else "Operation created",
             {"assigned_to_id": assigned_to_id, "original_due_at": due_at.isoformat() if due_at else None})
        db.session.commit()
    except Exception:
        current_app.logger.exception(
            "OPERATION_CREATE_FAILED: operation insert or audit record failed "
            "(organization_id=%s, actor_id=%s)",
            user.organization_id, user.id,
        )
        db.session.rollback()
        return jsonify({
            "error": "Operation could not be created. The server recorded diagnostic details; please retry after the backend deployment finishes."
        }), 500
    return jsonify({"message": "Operation created successfully.", "operation": operation.to_dict()}), 201


@operations_bp.get("/options")
@roles_required("Admin", "Supervisor", "Employee")
def operation_options():
    user = get_current_user()
    teams_query = Team.query.filter_by(organization_id=user.organization_id, status="active")
    if user.role == "Supervisor":
        ids = _team_ids(user)
        teams_query = teams_query.filter(Team.id.in_(ids)) if ids else teams_query.filter(Team.id == -1)
    elif user.role == "Employee":
        ids = _team_ids(user)
        teams_query = teams_query.filter(Team.id.in_(ids)) if ids else teams_query.filter(Team.id == -1)
    teams = teams_query.order_by(Team.name.asc()).all()
    team_ids = [item.id for item in teams]
    locations_query = Location.query.filter_by(organization_id=user.organization_id, status="active")
    zones_query = Zone.query.filter_by(organization_id=user.organization_id, status="active")
    if user.role != "Admin":
        locations_query = locations_query.filter(Location.team_id.in_(team_ids)) if team_ids else locations_query.filter(Location.id == -1)
        zones_query = zones_query.filter(or_(Zone.team_id.in_(team_ids), Zone.id == -1)) if team_ids else zones_query.filter(Zone.id == -1)
    locations = locations_query.order_by(Location.name.asc()).all()
    if user.role != "Admin":
        visible_location_ids = [row.id for row in locations]
        allowed_zone_clauses = [Zone.team_id.in_(team_ids)] if team_ids else []
        if visible_location_ids:
            allowed_zone_clauses.append(Zone.location_id.in_(visible_location_ids))
        zones_query = zones_query.filter(or_(*allowed_zone_clauses)) if allowed_zone_clauses else zones_query.filter(Zone.id == -1)
    zones = zones_query.order_by(Zone.name.asc()).all()

    members_query = User.query.filter(
        User.organization_id == user.organization_id,
        User.account_status == "active",
        User.role.in_(["Supervisor", "Employee"]),
    )
    if user.role != "Admin":
        member_ids = [row[0] for row in db.session.query(TeamMember.user_id).filter(TeamMember.team_id.in_(team_ids)).all()] if team_ids else []
        members_query = members_query.filter(User.id.in_(member_ids)) if member_ids else members_query.filter(User.id == user.id)
    members = members_query.order_by(func.lower(User.full_name)).all()
    return jsonify({
        "teams": [row.to_dict() for row in teams],
        "locations": [row.to_dict() for row in locations],
        "zones": [row.to_dict() for row in zones],
        "members": [{"id": row.id, "name": row.full_name, "email": row.email, "role": row.role} for row in members],
    }), 200


@operations_bp.get("/<int:operation_id>")
@roles_required("Admin", "Supervisor", "Employee")
def get_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or you do not have access."}), 404
    operation.overdue = _operation_is_overdue(operation)
    history = OperationLog.query.filter_by(operation_id=operation.id).order_by(OperationLog.created_at.asc()).all()
    result = operation.to_dict()
    result["history"] = [entry.to_dict() for entry in history]
    evidence = TaskEvidence.query.filter_by(operation_id=operation.id).order_by(TaskEvidence.created_at.asc()).all()
    result["evidence"] = [entry.to_dict() for entry in evidence]
    executions = OperationExecution.query.filter_by(operation_id=operation.id).order_by(
        OperationExecution.attempt_number.asc()
    ).all()
    result["executions"] = [entry.to_dict() for entry in executions]
    return jsonify({"operation": result}), 200


@operations_bp.post("/<int:operation_id>/evidence")
@roles_required("Admin", "Supervisor", "Employee")
def upload_operation_evidence(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or evidence upload is not permitted."}), 404
    if user.role == "Employee" and operation.assigned_to_id != user.id:
        return jsonify({"error": "Only the current assignee can upload execution evidence."}), 403
    if user.role == "Supervisor" and not _can_manage(operation, user) and operation.assigned_to_id != user.id:
        return jsonify({"error": "You cannot upload evidence for this operation."}), 403
    if operation.status.lower() not in {"in progress", "submitted", "completed", "approved", "verified"}:
        return jsonify({"error": "Start the work before attaching execution evidence."}), 409

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Choose a file to upload."}), 400
    filename = secure_filename(uploaded.filename)[:255]
    if not filename:
        return jsonify({"error": "The file name is not valid."}), 400
    content = uploaded.stream.read((8 * 1024 * 1024) + 1)
    if not content:
        return jsonify({"error": "The selected file is empty."}), 400
    if len(content) > 8 * 1024 * 1024:
        return jsonify({"error": "Evidence files must be 8 MB or smaller."}), 413

    file_type = (guess_type(filename)[0] or "").lower()
    allowed_types = {
        "image/jpeg", "image/png", "image/webp",
        "application/pdf", "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if file_type not in allowed_types:
        return jsonify({"error": "Use a JPG, PNG, WebP, PDF, TXT, DOC, or DOCX evidence file."}), 400
    evidence_type = "photo" if file_type.startswith("image/") else "document"
    stage = str(request.form.get("stage", "after" if evidence_type == "photo" else "execution")).strip().lower()
    if evidence_type == "photo":
        if stage not in {"before", "after", "general"}:
            return jsonify({"error": "Photo stage must be before, after, or general."}), 400
    else:
        stage = "execution"

    execution = OperationExecution.query.filter_by(
        operation_id=operation.id, performed_by_id=operation.assigned_to_id
    ).order_by(OperationExecution.attempt_number.desc()).first()
    if execution is None and operation.status.lower() == "in progress":
        latest_attempt = db.session.query(func.max(OperationExecution.attempt_number)).filter_by(
            operation_id=operation.id
        ).scalar() or 0
        execution = OperationExecution(
            operation_id=operation.id, performed_by_id=user.id,
            attempt_number=latest_attempt + 1, status="In Progress",
            verification_required=bool(operation.requires_verification),
        )
        db.session.add(execution)
        db.session.flush()
    if execution is None:
        return jsonify({"error": "Start work to open an execution record before uploading evidence."}), 409

    evidence = TaskEvidence(
        operation_id=operation.id,
        execution_id=execution.id,
        uploaded_by_id=user.id,
        evidence_type=evidence_type,
        stage=stage,
        original_filename=filename,
        storage_key="db:operation:" + str(operation.id) + ":evidence:" + uuid4().hex,
        content_type=file_type,
        file_size=len(content),
        file_data=content,
    )
    try:
        db.session.add(evidence)
        db.session.flush()
        _log(operation, user, "evidence_uploaded", operation.status,
             "Uploaded execution evidence: " + filename,
             {"evidence_id": evidence.id, "execution_id": execution.id,
              "evidence_type": evidence.evidence_type, "stage": evidence.stage, "file_size": evidence.file_size})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "The evidence file could not be stored."}), 500
    return jsonify({"message": "Execution evidence uploaded.", "evidence": evidence.to_dict()}), 201


@operations_bp.get("/<int:operation_id>/evidence/<int:evidence_id>/download")
@roles_required("Admin", "Supervisor", "Employee")
def download_operation_evidence(operation_id, evidence_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found."}), 404
    evidence = TaskEvidence.query.filter_by(id=evidence_id, operation_id=operation.id).first()
    if evidence is None:
        return jsonify({"error": "Evidence file not found."}), 404
    if not evidence.file_data:
        return jsonify({"error": "This older evidence record has no stored file bytes."}), 410
    return send_file(
        BytesIO(evidence.file_data),
        mimetype=evidence.content_type,
        as_attachment=True,
        download_name=evidence.original_filename,
        max_age=0,
    )


@operations_bp.patch("/<int:operation_id>")
@roles_required("Admin", "Supervisor")
def update_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user) or not _can_manage(operation, user):
        return jsonify({"error": "Operation not found or you do not have permission to manage it."}), 404
    if operation.status.lower() in CLOSED_STATUSES:
        return jsonify({"error": "Completed or cancelled operations cannot be edited."}), 409
    data = request.get_json(silent=True) or {}
    changes = {}
    text_fields = {"title": (200, True), "description": (10000, False), "instructions": (10000, False)}
    for key, (max_len, required) in text_fields.items():
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or (required and not value.strip()) or len(value.strip()) > max_len:
            return jsonify({"error": key + " is invalid."}), 400
        attr = "title" if key == "title" else key
        old = getattr(operation, attr)
        value = value.strip() or None
        if old != value:
            changes[key] = {"before": old, "after": value}
            setattr(operation, attr, value)
    category = data.get("category", data.get("task_type"))
    if category is not None:
        if not isinstance(category, str) or not category.strip() or len(category.strip()) > 100:
            return jsonify({"error": "Category is invalid."}), 400
        if operation.task_type != category.strip():
            changes["category"] = {"before": operation.task_type, "after": category.strip()}
            operation.task_type = category.strip()
    if "priority" in data:
        priority = data["priority"].strip().title() if isinstance(data["priority"], str) else ""
        if priority not in VALID_PRIORITIES:
            return jsonify({"error": "Priority must be Low, Normal, High, or Urgent."}), 400
        if operation.priority != priority:
            changes["priority"] = {"before": operation.priority, "after": priority}
            operation.priority = priority
    if "due_at" in data:
        due_at, error = _parse_datetime(data.get("due_at"), "due_at")
        if error:
            return jsonify({"error": error}), 400
        current = operation.due_at
        if current and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if current != due_at:
            before = current.isoformat() if current else None
            if operation.original_due_at is None:
                operation.original_due_at = current
            operation.due_at = due_at
            operation.reminder_before_sent_at = None
            operation.reminder_after_sent_at = None
            operation.escalated_at = None
            operation.overdue = False
            changes["deadline"] = {"before": before, "after": due_at.isoformat() if due_at else None}
    for field in ("checklist", "required_evidence"):
        if field in data:
            parsed, error = _parse_list(data, field, max_items=40 if field == "checklist" else 10, max_length=500 if field == "checklist" else 80)
            if error:
                return jsonify({"error": error}), 400
            old = getattr(operation, field) or []
            if old != (parsed or []):
                changes[field] = {"before": old, "after": parsed or []}
                setattr(operation, field, parsed or [])
    if "estimated_minutes" in data:
        value = data["estimated_minutes"]
        try:
            value = None if value in (None, "") else int(value)
            if value is not None and not 1 <= value <= 10080:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Estimated time must be between 1 minute and 7 days."}), 400
        if operation.estimated_minutes != value:
            changes["estimated_minutes"] = {"before": operation.estimated_minutes, "after": value}
            operation.estimated_minutes = value
    if "requires_verification" in data:
        if not isinstance(data["requires_verification"], bool):
            return jsonify({"error": "requires_verification must be true or false."}), 400
        value = data["requires_verification"]
        if operation.requires_verification != value:
            changes["requires_verification"] = {"before": operation.requires_verification, "after": value}
            operation.requires_verification = value
    if any(key in data for key in ("team_id", "location_id", "zone_id")):
        team_id = operation.team_id
        location_id = operation.location_id
        zone_id = operation.zone_id
        for key, current in (("team_id", team_id), ("location_id", location_id), ("zone_id", zone_id)):
            if key in data:
                parsed, error = _parse_id(data, key)
                if error:
                    return jsonify({"error": error}), 400
                if key == "team_id":
                    team_id = parsed
                elif key == "location_id":
                    location_id = parsed
                else:
                    zone_id = parsed
        scope_error = _team_context(team_id, location_id, zone_id, user, allow_area_only=True)
        if scope_error:
            return jsonify({"error": scope_error}), 400
        for key, attr, new_value in (
            ("team_id", "team_id", team_id),
            ("location_id", "location_id", location_id),
            ("zone_id", "zone_id", zone_id),
        ):
            old_value = getattr(operation, attr)
            if old_value != new_value:
                changes[key] = {"before": old_value, "after": new_value}
                setattr(operation, attr, new_value)
    if "schedule_type" in data:
        schedule_type = data["schedule_type"]
        if schedule_type not in VALID_SCHEDULE_TYPES:
            return jsonify({"error": "Schedule type must be manual, one_time, or recurring."}), 400
        recurrence_rule = data.get("recurrence_rule", operation.recurrence_rule)
        if schedule_type == "recurring":
            if not isinstance(recurrence_rule, dict) or recurrence_rule.get("frequency") not in {"daily", "weekly", "monthly"}:
                return jsonify({"error": "Recurring operations require a daily, weekly, or monthly recurrence rule."}), 400
            try:
                interval = int(recurrence_rule.get("interval", 1))
                if not 1 <= interval <= 365:
                    raise ValueError
                catch_up_policy = str(recurrence_rule.get("catch_up_policy", "latest_only")).lower()
                catch_up_window_days = int(recurrence_rule.get("catch_up_window_days", 7))
                if catch_up_policy not in {"latest_only", "skip", "limited"} or not 0 <= catch_up_window_days <= 365:
                    raise ValueError
                recurrence_rule = {
                    **recurrence_rule,
                    "interval": interval,
                    "catch_up_policy": catch_up_policy,
                    "catch_up_window_days": catch_up_window_days,
                }
            except (TypeError, ValueError):
                return jsonify({"error": "Use a valid interval and catch-up policy with a 0–365 day catch-up window."}), 400
        else:
            recurrence_rule = None
        if operation.schedule_type != schedule_type or operation.recurrence_rule != recurrence_rule:
            changes["schedule"] = {"before": {"type": operation.schedule_type, "rule": operation.recurrence_rule}, "after": {"type": schedule_type, "rule": recurrence_rule}}
            operation.schedule_type = schedule_type
            operation.recurrence_rule = recurrence_rule
    if "schedule_timezone" in data:
        value = data["schedule_timezone"]
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
            return jsonify({"error": "A valid schedule time zone is required."}), 400
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return jsonify({"error": "Choose a valid IANA time zone."}), 400
        if operation.schedule_timezone != value:
            changes["schedule_timezone"] = {"before": operation.schedule_timezone, "after": value}
            operation.schedule_timezone = value
    for key, default, maximum in (
        ("reminder_before_minutes", 60, 10080),
        ("escalate_after_minutes", 120, 525600),
    ):
        if key in data:
            try:
                value = int(data[key])
                if not 0 <= value <= maximum:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"error": key + " is outside the allowed range."}), 400
            old = getattr(operation, key)
            if old != value:
                changes[key] = {"before": old, "after": value}
                setattr(operation, key, value)
    if "escalate_to_admin" in data:
        value = data["escalate_to_admin"]
        if not isinstance(value, bool):
            return jsonify({"error": "escalate_to_admin must be true or false."}), 400
        if operation.escalate_to_admin != value:
            changes["escalate_to_admin"] = {"before": operation.escalate_to_admin, "after": value}
            operation.escalate_to_admin = value
    if operation.schedule_type == "recurring":
        if operation.due_at is None:
            return jsonify({"error": "Recurring operations require a first due date and time."}), 400
        if any(key in changes for key in ("deadline", "schedule", "schedule_timezone")):
            try:
                next_run = next_occurrence(operation.due_at, operation.recurrence_rule or {}, operation.schedule_timezone or "UTC")
            except (ZoneInfoNotFoundError, ValueError):
                return jsonify({"error": "Could not recalculate the recurring schedule. Check its time zone and rule."}), 400
            if operation.schedule_next_run_at != next_run:
                operation.schedule_next_run_at = next_run
                changes["next_scheduled_occurrence"] = {"after": next_run.isoformat() if next_run else None}
    else:
        operation.schedule_next_run_at = None
    if not changes:
        return jsonify({"message": "No changes to save.", "operation": operation.to_dict()}), 200
    try:
        _log(operation, user, "updated", operation.status, "Operation details updated", {"changes": changes})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Operation changes could not be saved."}), 500
    return jsonify({"message": "Operation updated.", "operation": operation.to_dict()}), 200


@operations_bp.post("/<int:operation_id>/reassign")
@roles_required("Admin", "Supervisor")
def reassign_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user) or not _can_manage(operation, user):
        return jsonify({"error": "Operation not found or you do not have permission to reassign it."}), 404
    if operation.status.lower() in CLOSED_STATUSES:
        return jsonify({"error": "Completed or cancelled operations cannot be reassigned."}), 409
    data = request.get_json(silent=True) or {}
    assignee_id, error = _parse_id(data, "assigned_to_id")
    if error or not assignee_id:
        return jsonify({"error": error or "An assignee is required."}), 400
    reason = data.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        return jsonify({"error": "A reason is required for reassignment."}), 400
    if len(reason.strip()) > 1000:
        return jsonify({"error": "Reassignment reason must be 1,000 characters or fewer."}), 400
    assignee = db.session.get(User, assignee_id)
    if not assignee or assignee.organization_id != user.organization_id or assignee.account_status != "active":
        return jsonify({"error": "Assignee must be an active member of this organization."}), 400
    if user.role == "Supervisor":
        if assignee.role != "Employee" or not operation.team_id:
            return jsonify({"error": "Supervisors can only reassign work to an Employee within a team."}), 403
        membership = TeamMember.query.filter_by(team_id=operation.team_id, user_id=assignee.id).first()
        if not membership:
            return jsonify({"error": "That employee is not a member of this operation's team."}), 400
    elif assignee.role not in {"Supervisor", "Employee"}:
        return jsonify({"error": "Operations can only be assigned to a Supervisor or Employee."}), 400
    previous_assignee = operation.assigned_to_id
    previous_status = operation.status
    if previous_assignee == assignee.id:
        return jsonify({"message": "This person is already assigned.", "operation": operation.to_dict()}), 200
    if operation.original_assigned_to_id is None:
        operation.original_assigned_to_id = previous_assignee or assignee.id
    if assignee.role == "Supervisor" and operation.assigned_supervisor_id is None:
        operation.assigned_supervisor_id = assignee.id
    operation.assigned_to_id = assignee.id
    if operation.status.lower() in {"created", "pending"}:
        operation.status = "Assigned"
    try:
        _log(operation, user, "reassigned", previous_status, reason.strip(), {
            "previous_assigned_to_id": previous_assignee,
            "new_assigned_to_id": assignee.id,
            "original_assigned_to_id": operation.original_assigned_to_id,
            "reassigned_by_id": user.id,
            "reason": reason.strip(),
        })
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Reassignment could not be saved."}), 500
    return jsonify({"message": "Operation reassigned. Original assignment and history are retained.", "operation": operation.to_dict()}), 200


@operations_bp.post("/<int:operation_id>/start")
@roles_required("Supervisor", "Employee")
def start_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or not assigned to you."}), 404
    if operation.assigned_to_id != user.id:
        return jsonify({"error": "Only the current assignee can start this operation."}), 403
    if operation.status.lower() not in {"assigned", "pending", "rejected"}:
        return jsonify({"error": "Only pending, assigned, or rejected work can be started."}), 409
    previous = operation.status
    operation.status = "In Progress"
    latest_attempt = db.session.query(func.max(OperationExecution.attempt_number)).filter_by(
        operation_id=operation.id
    ).scalar() or 0
    execution = OperationExecution(
        operation_id=operation.id,
        performed_by_id=user.id,
        attempt_number=latest_attempt + 1,
        status="In Progress",
        verification_required=bool(operation.requires_verification),
    )
    try:
        db.session.add(execution)
        _log(operation, user, "started", previous, "Work started; execution attempt " + str(latest_attempt + 1) + " opened.",
             {"execution_attempt": latest_attempt + 1})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not start operation."}), 500
    return jsonify({"message": "Operation started.", "operation": operation.to_dict()}), 200


@operations_bp.post("/<int:operation_id>/submit")
@roles_required("Supervisor", "Employee")
def submit_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or not assigned to you."}), 404
    if operation.assigned_to_id != user.id:
        return jsonify({"error": "Only the current assignee can submit this operation."}), 403
    if operation.status.lower() != "in progress":
        return jsonify({"error": "Only operations in progress can be submitted."}), 409

    data = request.get_json(silent=True) or {}
    notes = data.get("completion_notes", "")
    checklist_results = data.get("checklist_results", [])
    if not isinstance(notes, str) or len(notes) > 10000:
        return jsonify({"error": "Completion notes must be text of at most 10,000 characters."}), 400
    if not isinstance(checklist_results, list):
        return jsonify({"error": "Checklist results must be a list."}), 400

    expected_items = operation.checklist if isinstance(operation.checklist, list) else []
    results_by_item = {}
    for row in checklist_results:
        if not isinstance(row, dict) or not isinstance(row.get("item"), str) or not isinstance(row.get("completed"), bool):
            return jsonify({"error": "Each checklist result needs an item and a true/false completed value."}), 400
        item = row["item"].strip()
        if item not in expected_items or item in results_by_item:
            return jsonify({"error": "Checklist results do not match the operation checklist."}), 400
        results_by_item[item] = row["completed"]
    missing = [item for item in expected_items if results_by_item.get(item) is not True]
    if missing:
        return jsonify({"error": "Complete every checklist item before submission: " + ", ".join(missing[:8])}), 400

    execution = OperationExecution.query.filter_by(
        operation_id=operation.id, performed_by_id=user.id, status="In Progress"
    ).order_by(OperationExecution.attempt_number.desc()).first()
    if execution is None:
        # Older in-progress work can predate execution records. Preserve it by
        # opening a first persisted execution record when it is submitted.
        latest_attempt = db.session.query(func.max(OperationExecution.attempt_number)).filter_by(
            operation_id=operation.id
        ).scalar() or 0
        execution = OperationExecution(
            operation_id=operation.id,
            performed_by_id=user.id,
            attempt_number=latest_attempt + 1,
            status="In Progress",
            verification_required=bool(operation.requires_verification),
        )
        db.session.add(execution)
        db.session.flush()

    evidence_rows = TaskEvidence.query.filter(
        TaskEvidence.operation_id == operation.id,
        (TaskEvidence.execution_id == execution.id) | (TaskEvidence.execution_id.is_(None)),
    ).all()
    evidence_types = {row.evidence_type for row in evidence_rows}
    photo_stages = {row.stage for row in evidence_rows if row.evidence_type == "photo"}
    required = operation.required_evidence if isinstance(operation.required_evidence, list) else []
    missing_evidence = []
    if "photo" in required and "photo" not in evidence_types:
        missing_evidence.append("at least one photo")
    if "before_photo" in required and "before" not in photo_stages:
        missing_evidence.append("a before-work photo")
    if "after_photo" in required and "after" not in photo_stages:
        missing_evidence.append("an after-work photo")
    if "document" in required and "document" not in evidence_types:
        missing_evidence.append("a document or attachment")
    if "note" in required and not notes.strip():
        missing_evidence.append("a completion note")
    if missing_evidence:
        return jsonify({"error": "Required execution evidence is missing: " + ", ".join(missing_evidence) + "."}), 400

    previous = operation.status
    operation.submitted_at = datetime.now(timezone.utc)
    operation.status = "Submitted" if operation.requires_verification else "Completed"
    execution.submitted_at = operation.submitted_at
    execution.status = "Submitted" if operation.requires_verification else "Completed"
    execution.completion_notes = notes.strip() or None
    execution.checklist_results = [
        {"item": item, "completed": results_by_item.get(item, False)}
        for item in expected_items
    ]
    try:
        _log(operation, user, "submitted" if operation.requires_verification else "completed", previous,
             "Submitted for verification." if operation.requires_verification else "Completed without verification.",
             {"execution_id": execution.id, "completion_notes": execution.completion_notes,
              "checklist_results": execution.checklist_results})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not submit operation."}), 500
    return jsonify({
        "message": "Operation submitted for verification." if operation.requires_verification else "Operation completed.",
        "operation": operation.to_dict(),
        "execution": execution.to_dict(),
    }), 200


@operations_bp.post("/<int:operation_id>/review")
@roles_required("Admin", "Supervisor")
def review_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user) or not _can_manage(operation, user):
        return jsonify({"error": "Operation not found or not permitted for review."}), 404
    if user.id == operation.assigned_to_id:
        return jsonify({"error": "An assignee cannot verify their own work."}), 403
    if operation.status.lower() != "submitted":
        return jsonify({"error": "Only submitted operations can be reviewed."}), 409
    data = request.get_json(silent=True) or {}
    decision = data.get("decision", "")
    feedback = data.get("feedback", "")
    if not isinstance(decision, str) or decision.strip().lower() not in {"approve", "reject"}:
        return jsonify({"error": "Decision must be approve or reject."}), 400
    if not isinstance(feedback, str):
        return jsonify({"error": "Feedback must be text."}), 400
    decision = decision.strip().lower()
    feedback = feedback.strip()
    if decision == "reject" and not feedback:
        return jsonify({"error": "A rejection reason is required."}), 400
    previous = operation.status
    operation.status = "Completed" if decision == "approve" else "Rejected"
    operation.review_feedback = feedback or None
    operation.reviewed_at = datetime.now(timezone.utc)
    execution = OperationExecution.query.filter_by(
        operation_id=operation.id, status="Submitted"
    ).order_by(OperationExecution.attempt_number.desc()).first()
    if execution is None and operation.assigned_to_id:
        # Backward compatibility for operations submitted before execution
        # records were introduced.
        latest_attempt = db.session.query(func.max(OperationExecution.attempt_number)).filter_by(
            operation_id=operation.id
        ).scalar() or 0
        execution = OperationExecution(
            operation_id=operation.id,
            performed_by_id=operation.assigned_to_id,
            attempt_number=latest_attempt + 1,
            status="Submitted",
            started_at=operation.created_at or datetime.now(timezone.utc),
            submitted_at=operation.submitted_at or datetime.now(timezone.utc),
            verification_required=True,
        )
        db.session.add(execution)
    if execution is None:
        db.session.rollback()
        return jsonify({"error": "No execution record could be associated with this submission. The review was not saved."}), 409
    execution.status = "Completed" if decision == "approve" else "Rejected"
    execution.verified_by_id = user.id
    execution.verified_at = operation.reviewed_at
    execution.review_feedback = feedback or None
    try:
        _log(operation, user, "verified" if decision == "approve" else "rejected", previous, feedback or None, {
            "reviewed_by_id": user.id,
            "reviewed_at": operation.reviewed_at.isoformat(),
            "decision": decision,
            "execution_id": execution.id,
            "execution_attempt": execution.attempt_number,
        })
        if operation.assigned_to_id:
            db.session.add(Notification(
                recipient_id=operation.assigned_to_id,
                organization_id=operation.organization_id,
                title="Work approved" if decision == "approve" else "Work needs correction",
                message=(
                    feedback or "Your work has been approved. Your task has been reviewed and approved. Great work, thank you!"
                ) if decision == "approve" else "Your submitted work was rejected. Reason: " + feedback,
                notification_type="operation_review",
                resource_type="operation",
                resource_id=str(operation.id),
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not save operation review."}), 500
    return jsonify({
        "message": "Operation verified and completed." if decision == "approve" else "Operation rejected. The assignee can correct the work and resubmit it.",
        "operation": operation.to_dict(),
        "execution": execution.to_dict(),
        "employee_notification": "created" if operation.assigned_to_id else "not_available",
        "email_delivery": "not_configured",
    }), 200


@operations_bp.post("/<int:operation_id>/cancel")
@roles_required("Admin", "Supervisor")
def cancel_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user) or not _can_manage(operation, user):
        return jsonify({"error": "Operation not found or you do not have permission to cancel it."}), 404
    if operation.status.lower() in CLOSED_STATUSES or operation.status.lower() == "rejected":
        return jsonify({"error": "This operation is already closed or requires corrective action."}), 409
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        return jsonify({"error": "Cancellation reason must be text."}), 400
    previous = operation.status
    operation.status = "Cancelled"
    if operation.schedule_type == "recurring" and operation.schedule_series_id is None:
        operation.schedule_next_run_at = None
    try:
        _log(operation, user, "cancelled", previous, reason.strip() or "Cancelled by authorized manager.")
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not cancel operation."}), 500
    return jsonify({"message": "Operation cancelled.", "operation": operation.to_dict()}), 200


@operations_bp.get("/<int:operation_id>/history")
@roles_required("Admin", "Supervisor", "Employee")
def operation_history(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or history is not available to this account."}), 404
    history = OperationLog.query.filter_by(operation_id=operation.id).order_by(OperationLog.created_at.asc()).all()
    return jsonify({"operation_id": operation.id, "history": [entry.to_dict() for entry in history]}), 200


@operations_bp.get("/audit-history")
@roles_required("Admin")
def organization_audit_history():
    admin = get_current_user()
    try:
        purge_expired_audit_logs()
    except Exception:
        db.session.rollback()
    logs = AuditLog.query.filter(
        AuditLog.organization_id == admin.organization_id,
        AuditLog.created_at >= audit_cutoff(),
    ).order_by(AuditLog.created_at.desc()).limit(100).all()
    return jsonify({"audit_logs": [log.to_dict() for log in logs], "count": len(logs)}), 200
