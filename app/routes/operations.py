from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from io import BytesIO
from uuid import uuid4
from mimetypes import guess_type

from flask import Blueprint, current_app, jsonify, request, has_request_context, send_file
from sqlalchemy import func, or_

from app.extensions import db
from app.models.operation import Operation
from app.models.maintenance_request import MaintenanceRequest
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
from app.models.operational_records import OperationalIncident
from app.models.profile_qualification import ProfileQualification
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
        .join(Team, Team.id == TeamMember.team_id)
        .filter(
            TeamMember.user_id == user.id,
            Team.organization_id == user.organization_id,
            Team.status == "active",
        ).all()
    }


def _scoped_operation_query(user):
    query = Operation.query.filter_by(organization_id=user.organization_id)
    if user.role == "Admin":
        return query
    if user.role == "Staff":
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
    if user.role == "Staff":
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


def _send_review_email(recipient, operation, decision, feedback):
    """Send review outcomes through the application's configured transactional email providers."""
    if recipient is None or not recipient.email:
        return "not_configured"
    from app.routes.auth import _send_mechnar_email

    approved = decision == "approve"
    resubmission = decision == "request_resubmission"
    if approved:
        subject = "Work approved: " + (operation.title or "Submitted operation")
        outcome = "approved"
        default_message = "Your work has been reviewed and approved. Thank you."
    elif resubmission:
        subject = "Resubmission requested: " + (operation.title or "Submitted operation")
        outcome = "returned for correction and resubmission"
        default_message = "Please correct the issue described below and submit the operation again."
    else:
        subject = "Work rejected: " + (operation.title or "Submitted operation")
        outcome = "rejected"
        default_message = "Your submitted work was rejected. Please review the reason below."
    message = feedback or default_message
    text_body = (
        f"Hello {recipient.full_name or 'there'},\n\n"
        f"Your submitted work has been {outcome}.\n\n"
        f"Operation: {operation.title}\n"
        f"Supervisor message: {message}\n\n"
        "You can sign in to MECHNAR to view the decision and the operation history.\n\n"
        "— MECHNAR"
    )
    return "sent" if _send_mechnar_email(recipient.email, subject, text_body) else "failed"


def _submission_validation_summary(operation, execution):
    """Return requirement-by-requirement validation for the exact submitted attempt."""
    checks = []
    checklist = operation.checklist if isinstance(operation.checklist, list) else []
    results = execution.checklist_results if execution and isinstance(execution.checklist_results, list) else []
    results_by_item = {row.get("item"): row.get("completed") is True for row in results if isinstance(row, dict)}
    for item in checklist:
        checks.append({"label": "Checklist: " + str(item), "category": "checklist", "satisfied": results_by_item.get(item, False)})

    evidence_query = TaskEvidence.query.filter(TaskEvidence.operation_id == operation.id)
    if execution:
        evidence_query = evidence_query.filter((TaskEvidence.execution_id == execution.id) | (TaskEvidence.execution_id.is_(None)))
    else:
        evidence_query = evidence_query.filter(TaskEvidence.execution_id.is_(None))
    evidence_rows = evidence_query.all()
    evidence_types = {row.evidence_type for row in evidence_rows}
    photo_stages = {row.stage for row in evidence_rows if row.evidence_type == "photo"}
    required = operation.required_evidence if isinstance(operation.required_evidence, list) else []
    evidence_checks = {
        "photo": ("At least one photo", "photo" in evidence_types),
        "before_photo": ("Before-work photo", "before" in photo_stages),
        "after_photo": ("After-work photo", "after" in photo_stages),
        "document": ("Document or attachment", "document" in evidence_types),
        "note": ("Completion note", bool(execution and (execution.completion_notes or "").strip())),
    }
    for key in required:
        if key in evidence_checks:
            label, satisfied = evidence_checks[key]
            checks.append({"label": label, "category": "evidence", "satisfied": satisfied})

    qualifications = operation.required_qualifications if isinstance(operation.required_qualifications, list) else []
    performer_id = execution.performed_by_id if execution else operation.assigned_to_id
    held = set()
    if performer_id and qualifications:
        held = {str(item.title or "").strip().casefold() for item in ProfileQualification.query.filter_by(user_id=performer_id).all()}
    for qualification in qualifications:
        checks.append({"label": "Qualification: " + str(qualification), "category": "qualification", "satisfied": str(qualification).strip().casefold() in held})

    return {"checks": checks, "total": len(checks), "satisfied": sum(1 for item in checks if item["satisfied"]), "all_satisfied": all(item["satisfied"] for item in checks)}


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


@operations_bp.get("/capacity-preview")
@roles_required("Admin", "Supervisor")
def operation_capacity_preview():
    user=get_current_user()
    try: team_id=int(request.args.get("team_id",""))
    except (TypeError,ValueError): return jsonify({"error":"Choose a valid team."}),400
    team=Team.query.filter_by(id=team_id,organization_id=user.organization_id,status="active").first()
    if not team:return jsonify({"error":"Team not found."}),404
    if user.role=="Supervisor" and team_id not in _team_ids(user):
        return jsonify({"error":"You can only review capacity for your assigned teams."}),403
    members=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(
        TeamMember.team_id==team_id,User.organization_id==user.organization_id,
        User.account_status=="active",User.role=="Staff").all()
    member_ids=[m.id for m in members]
    open_ops=Operation.query.filter(Operation.organization_id==user.organization_id,
        Operation.team_id==team_id,func.lower(Operation.status).notin_(
            ["completed","approved","verified","closed","cancelled","canceled"])).all()
    busy_ids={op.assigned_to_id for op in open_ops if op.assigned_to_id in member_ids}
    high_ops=[op for op in open_ops if str(op.priority or "").lower() in {"high","urgent","critical"}]
    result={"team_id":team_id,"team_name":team.name,"staff_count":len(member_ids),
        "busy_staff":len(busy_ids),"available_staff":max(0,len(member_ids)-len(busy_ids)),
        "utilization_percent":round(len(busy_ids)/len(member_ids)*100) if member_ids else 0,
        "open_operations":len(open_ops),"high_priority_operations":len(high_ops),
        "high_priority_staff":len({op.assigned_to_id for op in high_ops if op.assigned_to_id in member_ids}),
        "unestimated_operations":sum(1 for op in open_ops if op.estimated_minutes is None)}
    assignee_raw=request.args.get("assigned_to_id")
    if assignee_raw:
        try: assignee_id=int(assignee_raw)
        except (TypeError,ValueError):return jsonify({"error":"Invalid assignee."}),400
        selected=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(
            TeamMember.team_id==team_id,User.id==assignee_id,
            User.organization_id==user.organization_id,User.account_status=="active",
            User.role.in_(["Staff","Supervisor"])).first()
        if selected is None:return jsonify({"error":"Selected person is not an active member of this team."}),400
        personal=[op for op in open_ops if op.assigned_to_id==assignee_id]
        result["assignee"]={"id":assignee_id,"open_operations":len(personal),
            "high_priority_operations":sum(1 for op in personal if str(op.priority or "").lower() in {"high","urgent","critical"}),
            "estimated_minutes":sum(int(op.estimated_minutes or 0) for op in personal),
            "missing_estimates":sum(1 for op in personal if op.estimated_minutes is None)}
    return jsonify({"capacity":result}),200


@operations_bp.route("", methods=["GET", "POST"])
@roles_required("Admin", "Supervisor", "Staff")
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
                .join(Team, Team.id == TeamMember.team_id)
                .filter(
                    TeamMember.user_id == supervisor.id,
                    Team.organization_id == user.organization_id,
                ).all()
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
        admin_assigned_ids = set()
        supervisor_assigned_ids = set()
        if operation_ids:
            admin_assignment_rows = db.session.query(OperationLog.operation_id).join(
                User, User.id == OperationLog.actor_id
            ).filter(
                OperationLog.operation_id.in_(operation_ids),
                OperationLog.action.in_(("assigned", "reassigned")),
                User.role == "Admin",
                User.organization_id == user.organization_id,
            ).distinct().all()
            admin_assigned_ids = {row[0] for row in admin_assignment_rows}
            if user.role == "Supervisor":
                supervisor_assignment_rows = db.session.query(OperationLog.operation_id).filter(
                    OperationLog.operation_id.in_(operation_ids),
                    OperationLog.actor_id == user.id,
                    OperationLog.action.in_(("assigned", "reassigned")),
                ).distinct().all()
                supervisor_assigned_ids = {row[0] for row in supervisor_assignment_rows}
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
            # Explicit ownership flags prevent the Supervisor UI from
            # confusing "created/assigned by me" with "assigned to me".
            row["assigned_by_me"] = (
                user.role == "Supervisor" and item.id in supervisor_assigned_ids
                and item.assigned_to_id != user.id
            )
            row["assigned_to_me"] = item.assigned_to_id == user.id
            row["admin_assigned"] = item.id in admin_assigned_ids
            attempt = latest_execution.get(item.id)
            if attempt:
                row["started_at"] = attempt.started_at.isoformat() if attempt.started_at else None
                row["execution_submitted_at"] = attempt.submitted_at.isoformat() if attempt.submitted_at else None
                row["completed_by_id"] = attempt.performed_by_id
                row["completed_by_name"] = attempt.performer.full_name if attempt.performer else None
                row["execution_status"] = attempt.status
                row["time_taken_minutes"] = (
                    max(0, round((attempt.submitted_at - attempt.started_at).total_seconds() / 60))
                    if attempt.started_at and attempt.submitted_at else None
                )
            operation_rows.append(row)
        return jsonify({"operations": operation_rows, "count": len(operation_rows)}), 200

    if user.role not in {"Admin", "Supervisor"}:
        return jsonify({"error": "Staffs can execute assigned work but cannot create organization operations."}), 403

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
    if user.role in {"Admin", "Supervisor"} and not assigned_to_id:
        return jsonify({"error": "Select an active staff member or Supervisor before creating an operation. New operations cannot be assigned to the creator or left unassigned."}), 400
    if user.role in {"Admin", "Supervisor"} and assigned_to_id == user.id:
        return jsonify({"error": "You cannot assign a new operation to yourself. Select another active person."}), 400
    due_at, error = _parse_datetime(data.get("due_at"), "due_at")
    if error:
        return jsonify({"error": error}), 400

    scope_error = _team_context(team_id, location_id, zone_id, user)
    if scope_error:
        return jsonify({"error": scope_error}), 400

    assignee = db.session.get(User, assigned_to_id) if assigned_to_id else None
    if assigned_to_id and (
        assignee is None or assignee.organization_id != user.organization_id
        or assignee.account_status != "active" or assignee.role not in {"Supervisor", "Staff"}
    ):
        return jsonify({"error": "Assignee must be an active Supervisor or Staff in this organization."}), 400
    if user.role == "Supervisor" and assignee and assignee.role != "Staff":
        return jsonify({"error": "Supervisors can assign new operations only to Staff members in their teams."}), 403
    if user.role == "Supervisor" and assignee:
        supervisor_team_ids = _team_ids(user)
        membership_query = TeamMember.query.filter(TeamMember.user_id == assignee.id)
        if team_id:
            membership_query = membership_query.filter(TeamMember.team_id == team_id)
        else:
            membership_query = membership_query.filter(TeamMember.team_id.in_(supervisor_team_ids)) if supervisor_team_ids else membership_query.filter(TeamMember.team_id == -1)
        if membership_query.first() is None:
            return jsonify({"error": "Select a Staff member who belongs to one of your active teams."}), 400
    if user.role == "Admin" and assignee and assignee.id == user.id:
        return jsonify({"error": "Admins cannot assign a new operation to themselves. Select another active person."}), 400

    checklist, error = _parse_list(data, "checklist", max_items=40, max_length=500)
    if error:
        return jsonify({"error": error}), 400
    required_evidence, error = _parse_list(data, "required_evidence", max_items=10, max_length=80)
    if error:
        return jsonify({"error": error}), 400
    required_qualifications, error = _parse_list(data, "required_qualifications", max_items=20, max_length=180)
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

    # Keep the supervisor who created the operation distinct from the
    # person responsible for supervising an Admin-created staff assignment.
    # A Supervisor assigning work to Staff is the creator/assigner, not the
    # assignee; the team scope already grants that Supervisor management access.
    assigned_supervisor_id = (
        assignee.id if assignee and assignee.role == "Supervisor"
        else (None if user.role == "Supervisor" and assignee and assignee.role == "Staff"
              else (user.id if user.role == "Supervisor" else None))
    )
    if assigned_supervisor_id is None and assignee and assignee.role == "Staff" and team_id:
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
        required_qualifications=required_qualifications or [],
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
@roles_required("Admin", "Supervisor", "Staff")
def operation_options():
    user = get_current_user()
    teams_query = Team.query.filter_by(organization_id=user.organization_id, status="active")
    if user.role == "Supervisor":
        ids = _team_ids(user)
        teams_query = teams_query.filter(Team.id.in_(ids)) if ids else teams_query.filter(Team.id == -1)
    elif user.role == "Staff":
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
        User.role.in_(["Supervisor", "Staff"]),
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
@roles_required("Admin", "Supervisor", "Staff")
def get_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or you do not have access."}), 404
    operation.overdue = _operation_is_overdue(operation)
    history = OperationLog.query.filter_by(operation_id=operation.id).order_by(OperationLog.created_at.asc()).all()
    result = operation.to_dict()
    result["location_name"] = operation.location.name if operation.location else None
    result["zone_name"] = operation.zone.name if operation.zone else None
    result["team_name"] = operation.team.name if operation.team else None
    result["history"] = [
        {**entry.to_dict(),
         "actor_name": entry.actor.full_name if entry.actor else None,
         "actor_role": entry.actor.role if entry.actor else None}
        for entry in history
    ]
    evidence = TaskEvidence.query.filter_by(operation_id=operation.id).order_by(TaskEvidence.created_at.asc()).all()
    result["evidence"] = [entry.to_dict() for entry in evidence]
    executions = OperationExecution.query.filter_by(operation_id=operation.id).order_by(
        OperationExecution.attempt_number.asc()
    ).all()
    result["executions"] = [entry.to_dict() for entry in executions]
    latest_execution = executions[-1] if executions else None
    result["validation_summary"] = _submission_validation_summary(operation, latest_execution)
    if latest_execution:
        result["started_at"] = latest_execution.started_at.isoformat() if latest_execution.started_at else None
        result["execution_submitted_at"] = latest_execution.submitted_at.isoformat() if latest_execution.submitted_at else None
        result["time_taken_minutes"] = (
            max(0, round((latest_execution.submitted_at - latest_execution.started_at).total_seconds() / 60))
            if latest_execution.started_at and latest_execution.submitted_at else None
        )
    return jsonify({"operation": result}), 200


@operations_bp.post("/<int:operation_id>/evidence")
@roles_required("Admin", "Supervisor", "Staff")
def upload_operation_evidence(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or evidence upload is not permitted."}), 404
    if user.role == "Staff" and operation.assigned_to_id != user.id:
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
        operation_id=operation.id, performed_by_id=user.id
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
@roles_required("Admin", "Supervisor", "Staff")
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
    for field in ("checklist", "required_evidence", "required_qualifications"):
        if field in data:
            parsed, error = _parse_list(data, field, max_items=40 if field == "checklist" else (20 if field == "required_qualifications" else 10), max_length=500 if field == "checklist" else (180 if field == "required_qualifications" else 80))
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
    if "assigned_to_id" in data:
        assigned_to_id, error = _parse_id(data, "assigned_to_id")
        if error:
            return jsonify({"error": error}), 400
        assignee = db.session.get(User, assigned_to_id) if assigned_to_id else None
        if assigned_to_id and (
            assignee is None or assignee.organization_id != user.organization_id
            or assignee.account_status != "active"
            or assignee.role not in {"Supervisor", "Staff"}
        ):
            return jsonify({"error": "Choose an active staff member or supervisor in this organization."}), 400
        if user.role == "Supervisor" and assignee and assignee.role != "Staff" and assignee.id != user.id:
            return jsonify({"error": "Supervisors can assign work only to themselves or Staff in their authorized team."}), 403
        if assignee and assignee.id != user.id:
            team_id = operation.team_id
            if team_id is None:
                return jsonify({"error": "Select a team before assigning this operation to a staff member."}), 400
            membership = TeamMember.query.filter_by(team_id=team_id, user_id=assignee.id).first()
            if not membership:
                return jsonify({"error": "The selected assignee must belong to the operation's team."}), 400
        old_assignee_id = operation.assigned_to_id
        if old_assignee_id != assigned_to_id:
            changes["assigned_to_id"] = {"before": old_assignee_id, "after": assigned_to_id}
            operation.assigned_to_id = assigned_to_id
            if assigned_to_id and operation.status.lower() in {"pending", "created"}:
                operation.status = "Assigned"
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
        if assignee.role != "Staff" or not operation.team_id:
            return jsonify({"error": "Supervisors can only reassign work to an Staff within a team."}), 403
        membership = TeamMember.query.filter_by(team_id=operation.team_id, user_id=assignee.id).first()
        if not membership:
            return jsonify({"error": "That employee is not a member of this operation's team."}), 400
    elif assignee.role not in {"Supervisor", "Staff"}:
        return jsonify({"error": "Operations can only be assigned to a Supervisor or Staff."}), 400
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
@roles_required("Supervisor", "Staff")
def start_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or not assigned to you."}), 404
    if user.role == "Staff":
        if operation.assigned_to_id != user.id:
            return jsonify({"error": "Only the current assignee can start this operation."}), 403
    elif user.role == "Supervisor":
        if operation.assigned_to_id != user.id and not _can_manage(operation, user):
            return jsonify({"error": "You can only start work for an operation assigned to you or within your authorized team."}), 403
    if operation.status.lower() not in {"assigned", "pending", "resubmission required"}:
        return jsonify({"error": "Only pending, assigned, or work returned for resubmission can be started."}), 409
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
@roles_required("Supervisor", "Staff")
def submit_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user):
        return jsonify({"error": "Operation not found or not assigned to you."}), 404
    if user.role == "Staff":
        if operation.assigned_to_id != user.id:
            return jsonify({"error": "Only the current assignee can submit this operation."}), 403
    elif user.role == "Supervisor":
        if operation.assigned_to_id != user.id and not _can_manage(operation, user):
            return jsonify({"error": "You can only submit work for an operation assigned to you or within your authorized team."}), 403
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
    required_qualifications = operation.required_qualifications if isinstance(operation.required_qualifications, list) else []
    if required_qualifications:
        held = {str(item.title or "").strip().casefold() for item in ProfileQualification.query.filter_by(user_id=user.id).all()}
        missing_qualifications = [name for name in required_qualifications if str(name).strip().casefold() not in held]
        if missing_qualifications:
            return jsonify({"error": "Required qualifications are not recorded on your profile: " + ", ".join(missing_qualifications[:8]) + ". Contact your Supervisor."}), 400
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


@operations_bp.get("/review-queue")
@roles_required("Supervisor")
def supervisor_review_queue():
    user = get_current_user()
    team_ids = _team_ids(user)
    if not team_ids:
        return jsonify({"operations": [], "count": 0, "message": "No active team is assigned to this Supervisor."}), 200
    items = Operation.query.filter(
        Operation.organization_id == user.organization_id,
        Operation.team_id.in_(team_ids),
        func.lower(Operation.status).in_(("submitted", "completed", "rejected", "resubmission required")),
        Operation.requires_verification.is_(True),
    ).order_by(Operation.submitted_at.asc(), Operation.created_at.asc()).limit(500).all()
    operation_ids = [item.id for item in items]
    latest_execution = {}
    if operation_ids:
        executions = OperationExecution.query.filter(
            OperationExecution.operation_id.in_(operation_ids),
            func.lower(OperationExecution.status).in_(("submitted", "completed", "rejected", "resubmission required")),
        ).order_by(OperationExecution.attempt_number.desc(), OperationExecution.created_at.desc()).all()
        for execution in executions:
            latest_execution.setdefault(execution.operation_id, execution)
    result = []
    for item in items:
        # A Supervisor may not review work they personally performed.
        if item.assigned_to_id == user.id:
            continue
        row = item.to_dict()
        row["location_name"] = item.location.name if item.location else None
        row["zone_name"] = item.zone.name if item.zone else None
        row["team_name"] = item.team.name if item.team else None
        execution = latest_execution.get(item.id)
        row["execution"] = execution.to_dict() if execution else None
        submitted_for_review = execution.submitted_at if execution and execution.submitted_at else item.submitted_at
        if submitted_for_review and submitted_for_review.tzinfo is None:
            submitted_for_review = submitted_for_review.replace(tzinfo=timezone.utc)
        wait_seconds = max(0, (datetime.now(timezone.utc) - submitted_for_review).total_seconds()) if submitted_for_review and item.status.lower() == "submitted" else 0
        wait_hours = int(wait_seconds // 3600)
        row["review_wait_hours"] = wait_hours
        row["review_overdue"] = item.status.lower() == "submitted" and wait_seconds >= 86400
        row["started_at"] = execution.started_at.isoformat() if execution and execution.started_at else None
        row["execution_submitted_at"] = execution.submitted_at.isoformat() if execution and execution.submitted_at else row.get("submitted_at")
        row["time_taken_minutes"] = (
            max(0, round((execution.submitted_at - execution.started_at).total_seconds() / 60))
            if execution and execution.started_at and execution.submitted_at else None
        )
        result.append(row)
    return jsonify({"operations": result, "count": len(result)}), 200


@operations_bp.post("/bulk-review")
@roles_required("Supervisor")
def bulk_review_operations():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    operation_ids = data.get("operation_ids")
    feedback = data.get("feedback", "")
    if not isinstance(operation_ids, list) or not operation_ids or len(operation_ids) > 50:
        return jsonify({"error": "Select between 1 and 50 submissions for bulk approval."}), 400
    if any(isinstance(value, bool) for value in operation_ids):
        return jsonify({"error": "Every selected operation ID must be an integer."}), 400
    try:
        ids = [int(value) for value in operation_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "Every selected operation ID must be an integer."}), 400
    if any(value <= 0 for value in ids) or len(set(ids)) != len(ids):
        return jsonify({"error": "Selected operation IDs must be unique positive integers."}), 400
    if not isinstance(feedback, str) or len(feedback) > 1000:
        return jsonify({"error": "The approval message must be text of at most 1,000 characters."}), 400
    feedback = feedback.strip()
    team_ids = _team_ids(user)
    if not team_ids:
        return jsonify({"error": "You do not have an active team to review."}), 403

    operations = Operation.query.filter(
        Operation.id.in_(ids),
        Operation.organization_id == user.organization_id,
    ).all()
    by_id = {item.id: item for item in operations}
    if len(by_id) != len(ids):
        return jsonify({"error": "One or more selected submissions are unavailable in your team."}), 404

    executions_by_operation = {}
    evidence_by_execution = {}
    for operation_id in ids:
        operation = by_id[operation_id]
        if not _visible(operation, user) or not _can_manage(operation, user) or operation.team_id not in team_ids:
            return jsonify({"error": "Bulk review is restricted to submissions assigned to your own team."}), 403
        if operation.assigned_to_id == user.id:
            return jsonify({"error": "You cannot approve your own submitted work."}), 403
        if operation.status.lower() != "submitted" or not operation.requires_verification:
            return jsonify({"error": "Bulk approval only accepts submissions currently awaiting required verification."}), 409
        execution = OperationExecution.query.filter_by(
            operation_id=operation.id, status="Submitted"
        ).order_by(OperationExecution.attempt_number.desc()).first()
        if execution is None:
            return jsonify({"error": "A selected submission has no execution record. Review it individually after checking its history."}), 409
        validation = _submission_validation_summary(operation, execution)
        if not validation["all_satisfied"]:
            missing_labels = [item["label"] for item in validation["checks"] if not item["satisfied"]]
            return jsonify({"error": "Bulk approval stopped. Outstanding requirements: " + ", ".join(missing_labels[:8]) + ". Review the submission individually."}), 409
        expected = operation.checklist if isinstance(operation.checklist, list) else []
        results = execution.checklist_results if isinstance(execution.checklist_results, list) else []
        completed_items = {row.get("item") for row in results if isinstance(row, dict) and row.get("completed") is True}
        if any(item not in completed_items for item in expected):
            return jsonify({"error": "Bulk approval stopped: at least one submission has an incomplete required checklist. Review it individually."}), 409
        evidence = TaskEvidence.query.filter(
            TaskEvidence.operation_id == operation.id,
            (TaskEvidence.execution_id == execution.id) | (TaskEvidence.execution_id.is_(None)),
        ).all()
        types = {row.evidence_type for row in evidence}
        stages = {row.stage for row in evidence if row.evidence_type == "photo"}
        required = operation.required_evidence if isinstance(operation.required_evidence, list) else []
        missing = (
            ("photo" in required and "photo" not in types)
            or ("before_photo" in required and "before" not in stages)
            or ("after_photo" in required and "after" not in stages)
            or ("document" in required and "document" not in types)
            or ("note" in required and not (execution.completion_notes or "").strip())
        )
        if missing:
            return jsonify({"error": "Bulk approval stopped: required evidence is missing from at least one selected submission. Review it individually."}), 409
        executions_by_operation[operation.id] = execution

    now = datetime.now(timezone.utc)
    try:
        for operation_id in ids:
            operation = by_id[operation_id]
            execution = executions_by_operation[operation_id]
            previous = operation.status
            operation.status = "Completed"
            operation.review_feedback = feedback or None
            operation.reviewed_at = now
            execution.status = "Completed"
            execution.verified_by_id = user.id
            execution.verified_at = now
            execution.review_feedback = feedback or None
            _log(operation, user, "verified", previous, feedback or "Approved through controlled bulk review.", {
                "reviewed_by_id": user.id,
                "reviewed_at": now.isoformat(),
                "decision": "approve",
                "execution_id": execution.id,
                "execution_attempt": execution.attempt_number,
                "bulk_review": True,
            })
            if operation.assigned_to_id:
                db.session.add(Notification(
                    recipient_id=operation.assigned_to_id,
                    organization_id=operation.organization_id,
                    title="Work approved",
                    message=feedback or "Your submitted work has been approved.",
                    notification_type="operation_review",
                    resource_type="operation",
                    resource_id=str(operation.id),
                ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Bulk operation review failed.")
        return jsonify({"error": "The bulk review could not be saved. No selected submission was approved."}), 500

    email_results = []
    for operation_id in ids:
        operation = by_id[operation_id]
        recipient = db.session.get(User, operation.assigned_to_id) if operation.assigned_to_id else None
        email_results.append(_send_review_email(recipient, operation, "approve", feedback))
    sent = email_results.count("sent")
    return jsonify({
        "message": f"{len(ids)} submissions approved.",
        "approved_ids": ids,
        "email_delivery": "sent" if sent == len(ids) else ("not_configured" if all(x == "not_configured" for x in email_results) else "partial_or_failed"),
        "email_sent_count": sent,
        "email_total": len(ids),
    }), 200


@operations_bp.post("/<int:operation_id>/review")
@roles_required("Admin", "Supervisor")
def review_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)
    if not _visible(operation, user) or not _can_manage(operation, user):
        return jsonify({"error": "Operation not found or not permitted for review."}), 404
    if user.role == "Supervisor" and operation.team_id not in _team_ids(user):
        return jsonify({"error": "Supervisors can review submissions belonging only to their own team."}), 403
    if not operation.requires_verification:
        return jsonify({"error": "This operation was not configured to require Supervisor verification."}), 409
    if user.id == operation.assigned_to_id:
        return jsonify({"error": "An assignee cannot verify their own work."}), 403
    if operation.status.lower() != "submitted":
        return jsonify({"error": "Only submitted operations can be reviewed."}), 409
    data = request.get_json(silent=True) or {}
    decision = data.get("decision", "")
    feedback = data.get("feedback", "")
    if not isinstance(decision, str) or decision.strip().lower() not in {"approve", "reject", "request_resubmission"}:
        return jsonify({"error": "Decision must be approve, reject, or request_resubmission."}), 400
    if not isinstance(feedback, str) or len(feedback) > 1000:
        return jsonify({"error": "Feedback must be text of at most 1,000 characters."}), 400
    decision = decision.strip().lower()
    feedback = feedback.strip()
    if decision in {"reject", "request_resubmission"} and not feedback:
        return jsonify({"error": "A reason or correction instruction is required for this decision."}), 400
    previous = operation.status
    operation.status = "Completed" if decision == "approve" else ("Resubmission Required" if decision == "request_resubmission" else "Rejected")
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
    validation_summary = _submission_validation_summary(operation, execution)
    if decision == "approve" and not validation_summary["all_satisfied"]:
        missing = [item["label"] for item in validation_summary["checks"] if not item["satisfied"]]
        return jsonify({"error": "Approval blocked. Outstanding requirements: " + ", ".join(missing[:8])}), 409
    execution.status = "Completed" if decision == "approve" else ("Resubmission Required" if decision == "request_resubmission" else "Rejected")
    execution.verified_by_id = user.id
    execution.verified_at = operation.reviewed_at
    execution.review_feedback = feedback or None
    try:
        review_action = "verified" if decision == "approve" else ("resubmission_requested" if decision == "request_resubmission" else "rejected")
        _log(operation, user, review_action, previous, feedback or None, {
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
                title=("Work approved" if decision == "approve" else ("Resubmission requested" if decision == "request_resubmission" else "Work rejected")),
                message=(
                    feedback or "Your work has been reviewed and approved. Thank you."
                ) if decision == "approve" else (("Please correct and resubmit: " if decision == "request_resubmission" else "Your submitted work was rejected. Reason: ") + feedback),
                notification_type="operation_review",
                resource_type="operation",
                resource_id=str(operation.id),
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not save operation review."}), 500
    recipient = db.session.get(User, operation.assigned_to_id) if operation.assigned_to_id else None
    email_delivery = _send_review_email(recipient, operation, decision, feedback)
    return jsonify({
        "message": ("Operation verified and completed." if decision == "approve" else ("Corrections requested. The assignee can start a new attempt and resubmit." if decision == "request_resubmission" else "Operation rejected. This submission is closed.")),
        "operation": operation.to_dict(),
        "execution": execution.to_dict(),
        "employee_notification": "created" if operation.assigned_to_id else "not_available",
        "email_delivery": email_delivery,
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
@roles_required("Admin", "Supervisor", "Staff")
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


# Supervisor-only blocker handling is exposed through Operations so managers can
# resolve operational issues without mixing them into the Admin/Supervisor work queue.


@operations_bp.route("/maintenance-requests", methods=["GET", "POST"])
@roles_required("Admin", "Supervisor")
def maintenance_requests_collection():
    user = get_current_user()
    if request.method == "GET":
        query = MaintenanceRequest.query.filter_by(organization_id=user.organization_id)
        if user.role == "Supervisor":
            team_ids = _team_ids(user)
            if not team_ids:
                return jsonify({"items": []})
            query = query.filter(MaintenanceRequest.team_id.in_(team_ids))
        items = query.order_by(MaintenanceRequest.created_at.desc()).limit(300).all()
        return jsonify({"items": [item.to_dict() for item in items]})

    if user.role != "Supervisor":
        return jsonify({"error": "Maintenance requests must be raised by a Supervisor."}), 403
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    issue_type = str(data.get("issue_type") or "General repair").strip()
    priority = str(data.get("priority") or "Normal").strip().title()
    asset_reference = str(data.get("asset_reference") or "").strip()
    if not title or len(title) > 180:
        return jsonify({"error": "Enter an issue title (maximum 180 characters)."}), 400
    if not description or len(description) > 5000:
        return jsonify({"error": "Describe the problem (maximum 5,000 characters)."}), 400
    if issue_type not in {"Electrical", "Plumbing", "HVAC", "Furniture", "Building / facility", "Equipment", "IT / network", "General repair", "Other"}:
        return jsonify({"error": "Choose a valid issue type."}), 400
    if priority not in {"Low", "Normal", "High", "Urgent"}:
        return jsonify({"error": "Choose a valid priority."}), 400
    team_id, error = _parse_id(data, "team_id")
    if error or not team_id:
        return jsonify({"error": error or "Select the responsible team."}), 400
    location_id, error = _parse_id(data, "location_id")
    if error:
        return jsonify({"error": error}), 400
    zone_id, error = _parse_id(data, "zone_id")
    if error:
        return jsonify({"error": error}), 400
    scope_error = _team_context(team_id, location_id, zone_id, user)
    if scope_error:
        return jsonify({"error": scope_error}), 403
    item = MaintenanceRequest(
        organization_id=user.organization_id,
        team_id=team_id,
        location_id=location_id,
        zone_id=zone_id,
        requested_by_id=user.id,
        title=title,
        issue_type=issue_type,
        description=description,
        priority=priority,
        asset_reference=asset_reference[:160] or None,
        safety_impact=bool(data.get("safety_impact")),
        status="Awaiting Admin review",
    )
    db.session.add(item)
    db.session.flush()
    admins = User.query.filter_by(
        organization_id=user.organization_id, role="Admin", account_status="active"
    ).all()
    for admin in admins:
        db.session.add(Notification(
            recipient_id=admin.id,
            organization_id=user.organization_id,
            title="New maintenance request",
            message=f"{user.full_name} reported: {item.title} ({item.priority} priority).",
            notification_type="maintenance_request",
            resource_type="MaintenanceRequest",
            resource_id=str(item.id),
        ))
    db.session.add(AuditLog(
        actor_id=user.id,
        organization_id=user.organization_id,
        action="maintenance.requested",
        resource_type="MaintenanceRequest",
        resource_id=str(item.id),
        details={"title": item.title, "priority": item.priority, "team_id": item.team_id},
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))
    db.session.commit()
    return jsonify({"message": "Maintenance request sent to the organization Admin.", "request": item.to_dict()}), 201


@operations_bp.patch("/maintenance-requests/<int:request_id>")
@roles_required("Admin")
def review_maintenance_request(request_id):
    user = get_current_user()
    item = MaintenanceRequest.query.filter_by(
        id=request_id, organization_id=user.organization_id
    ).first()
    if item is None:
        return jsonify({"error": "Maintenance request not found."}), 404
    data = request.get_json(silent=True) or {}
    decision = str(data.get("decision") or "").strip().lower()
    note = str(data.get("admin_note") or "").strip()
    allowed = {
        "approve": "Approved — arranging repair",
        "reject": "Rejected",
        "in_progress": "Repair in progress",
        "complete": "Completed",
    }
    if decision not in allowed:
        return jsonify({"error": "Choose a valid Admin decision."}), 400
    if decision == "reject" and not note:
        return jsonify({"error": "Add a reason before rejecting this request."}), 400
    if item.status == "Rejected" or item.status == "Completed":
        return jsonify({"error": "This maintenance request is already closed."}), 409
    previous = item.status
    item.status = allowed[decision]
    item.admin_note = note[:3000] or item.admin_note
    item.reviewed_by_id = user.id
    item.reviewed_at = datetime.now(timezone.utc)
    db.session.add(AuditLog(
        actor_id=user.id,
        organization_id=user.organization_id,
        action="maintenance." + decision,
        resource_type="MaintenanceRequest",
        resource_id=str(item.id),
        details={"title": item.title, "previous_status": previous, "new_status": item.status, "admin_note": item.admin_note},
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))
    db.session.commit()
    return jsonify({"message": "Maintenance request updated.", "request": item.to_dict()})


@operations_bp.route("/blockers", methods=["GET", "POST"])
@roles_required("Supervisor")
def supervisor_operation_blockers():
    user = get_current_user()
    team_ids = _team_ids(user)
    if request.method == "GET":
        if not team_ids:
            return jsonify({"items": [], "count": 0, "can_create": False, "message": "No active team is assigned to this Supervisor. Ask an organization Admin to add you to a team."}), 200

        items = OperationalIncident.query.filter(
            OperationalIncident.organization_id == user.organization_id,
            OperationalIncident.team_id.in_(team_ids),
        ).order_by(OperationalIncident.created_at.desc()).limit(300).all()
        return jsonify({"items": [item.to_dict() for item in items], "count": len(items)}), 200

    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    if not title or len(title) > 180 or not description or len(description) > 10000:
        return jsonify({"error": "A title (up to 180 characters) and description (up to 10,000 characters) are required."}), 400
    category = str(data.get("category") or "other").strip().lower()
    severity = str(data.get("severity") or "medium").strip().lower()
    if category not in {"equipment", "safety", "staffing", "resource", "facility", "other"}:
        return jsonify({"error": "Choose a valid blocker category."}), 400
    if severity not in {"low", "medium", "high", "critical"}:
        return jsonify({"error": "Choose a valid severity."}), 400

    team_id, error = _parse_id(data, "team_id")
    if error:
        return jsonify({"error": error}), 400
    if team_id is None or team_id not in team_ids:
        return jsonify({"error": "Choose one of your assigned teams."}), 403
    operation_id, error = _parse_id(data, "operation_id")
    if error:
        return jsonify({"error": error}), 400
    operation = db.session.get(Operation, operation_id) if operation_id else None
    if operation_id and (not _visible(operation, user) or not _can_manage(operation, user)):
        return jsonify({"error": "The selected operation is outside your authorized scope."}), 404
    if operation and operation.team_id not in (None, team_id):
        return jsonify({"error": "The selected operation does not belong to this team."}), 400

    location_id, error = _parse_id(data, "location_id")
    if error:
        return jsonify({"error": error}), 400
    zone_id, error = _parse_id(data, "zone_id")
    if error:
        return jsonify({"error": error}), 400
    scope_error = _team_context(team_id, location_id, zone_id, user, allow_area_only=True)
    if scope_error:
        return jsonify({"error": scope_error}), 400
    if operation:
        location_id = location_id or operation.location_id
        zone_id = zone_id or operation.zone_id

    item = OperationalIncident(
        organization_id=user.organization_id,
        team_id=team_id,
        reported_by_id=user.id,
        operation_id=operation.id if operation else None,
        location_id=location_id,
        zone_id=zone_id,
        title=title,
        description=description,
        category=category,
        severity=severity,
        status="open",
    )
    db.session.add(item)
    try:
        db.session.flush()
        if operation:
            _log(operation, user, "blocker_reported", operation.status,
                 "A blocker was recorded: " + title,
                 {"blocker_id": item.id, "severity": severity, "category": category})
        db.session.add(AuditLog(
            actor_id=user.id,
            organization_id=user.organization_id,
            action="operation.blocker_reported",
            resource_type="OperationalIncident",
            resource_id=str(item.id),
            details={"title": title, "severity": severity, "category": category,
                     "team_id": team_id, "operation_id": operation.id if operation else None},
            ip_address=request.remote_addr,
            user_agent=(request.headers.get("User-Agent", "")[:300] or None),
        ))
        if severity in {"high", "critical"}:
            admins = User.query.filter_by(
                organization_id=user.organization_id, role="Admin", account_status="active"
            ).all()
            for admin in admins:
                db.session.add(Notification(
                    recipient_id=admin.id, organization_id=user.organization_id,
                    title="High-priority operational blocker",
                    message=title[:1000] + " — " + severity.upper() + " severity. Review the affected team and operation.",
                    notification_type="operation_blocker",
                    resource_type="OperationalIncident", resource_id=str(item.id),
                ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save a Supervisor operational blocker.")
        return jsonify({"error": "The blocker could not be saved. Please retry."}), 500
    return jsonify({"message": "Blocker recorded.", "item": item.to_dict()}), 201


@operations_bp.patch("/blockers/<int:blocker_id>")
@roles_required("Supervisor")
def update_supervisor_operation_blocker(blocker_id):
    user = get_current_user()
    team_ids = _team_ids(user)
    item = OperationalIncident.query.filter(
        OperationalIncident.id == blocker_id,
        OperationalIncident.organization_id == user.organization_id,
        OperationalIncident.team_id.in_(team_ids or {-1}),
    ).first()
    if item is None:
        return jsonify({"error": "Blocker not found in your authorized teams."}), 404
    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or item.status).strip().lower()
    if status not in {"open", "in_progress", "resolved", "closed"}:
        return jsonify({"error": "Choose a valid blocker status."}), 400
    resolution = str(data.get("resolution") or "").strip()
    if status in {"resolved", "closed"} and not resolution and not item.resolution:
        return jsonify({"error": "Add a resolution note before closing this blocker."}), 400
    previous_status = item.status
    item.status = status
    item.resolution = resolution[:4000] or item.resolution
    item.resolved_at = datetime.now(timezone.utc) if status in {"resolved", "closed"} else None
    try:
        db.session.add(AuditLog(
            actor_id=user.id,
            organization_id=user.organization_id,
            action="operation.blocker_" + status,
            resource_type="OperationalIncident",
            resource_id=str(item.id),
            details={"title": item.title, "previous_status": previous_status,
                     "new_status": status, "resolution": item.resolution},
            ip_address=request.remote_addr,
            user_agent=(request.headers.get("User-Agent", "")[:300] or None),
        ))
        if item.operation:
            _log(item.operation, user, "blocker_" + status, item.operation.status,
                 "Blocker '" + item.title + "' marked " + status + ".",
                 {"blocker_id": item.id, "resolution": item.resolution})
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not update a Supervisor operational blocker.")
        return jsonify({"error": "The blocker update could not be saved. Please retry."}), 500
    return jsonify({"message": "Blocker updated.", "item": item.to_dict()}), 200
