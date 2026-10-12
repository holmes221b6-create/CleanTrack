from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.extensions import db
from app.models.operation import Operation
from app.models.operation_log import OperationLog
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.team import Team
from app.models.location import Location
from app.models.zone import Zone
from app.services.permissions import get_current_user, roles_required
from flask import Blueprint, jsonify, request, has_request_context


operations_bp = Blueprint(
    "operations",
    __name__,
    url_prefix="/api/operations",
)


def write_operation_logs(operation, actor, action, previous_status, message=None):
    """Add operation history and audit entries to the current transaction."""
    operation_log = OperationLog(
        operation_id=operation.id,
        actor_id=actor.id,
        action=action,
        previous_status=previous_status,
        new_status=operation.status,
        message=message,
    )

  
    audit_log = AuditLog(
        actor_id=actor.id,
        organization_id=operation.organization_id,
        action=f"operation.{action}",
        resource_type="Operation",
        resource_id=str(operation.id),
        details={
            "title": operation.title,
            "previous_status": previous_status,
            "new_status": operation.status,
            "message": message,
        },
        ip_address=(
            request.remote_addr
            if has_request_context()
            else None
        ),
        user_agent=(
            request.headers.get("User-Agent", "")[:300] or None
            if has_request_context()
            else None
        ),
    )
    
    db.session.add(operation_log)
    db.session.add(audit_log)

def parse_optional_id(data, field):
    value = data.get(field)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{field} must be an integer."
    return value, None


@operations_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def operations_collection():
    admin = get_current_user()

    if request.method == "GET":
        query = Operation.query.filter_by(
            organization_id=admin.organization_id
        )
        status = request.args.get("status")
        if status:
            query = query.filter_by(status=status)

        operations = query.order_by(Operation.created_at.desc()).all()
        return jsonify({
            "operations": [item.to_dict() for item in operations]
        }), 200

    data = request.get_json(silent=True) or {}
    title = data.get("title", "")
    description = data.get("description", "")
    task_type = data.get("task_type", "General")

    if not isinstance(title, str) or not title.strip():
        return jsonify({"error": "Operation title is required."}), 400
    title = title.strip()

    if len(title) > 200:
        return jsonify({"error": "Title must be 200 characters or fewer."}), 400

    if not isinstance(description, str) or not isinstance(task_type, str):
        return jsonify({"error": "Description and task type must be text."}), 400
    if not task_type.strip() or len(task_type.strip()) > 100:
        return jsonify({"error": "Task type must contain 1 to 100 characters."}), 400

    team_id, error = parse_optional_id(data, "team_id")
    if error:
        return jsonify({"error": error}), 400
    location_id, error = parse_optional_id(data, "location_id")
    if error:
        return jsonify({"error": error}), 400
    zone_id, error = parse_optional_id(data, "zone_id")
    if error:
        return jsonify({"error": error}), 400
    assigned_to_id, error = parse_optional_id(data, "assigned_to_id")
    if error:
        return jsonify({"error": error}), 400

    if team_id is not None:
        team = db.session.get(Team, team_id)
        if team is None or team.organization_id != admin.organization_id:
            return jsonify({"error": "Team not found in your organization."}), 400

    if location_id is not None:
        location = db.session.get(Location, location_id)
        if location is None or location.organization_id != admin.organization_id:
            return jsonify({"error": "Location not found in your organization."}), 400
        if team_id is not None and location.team_id not in (None, team_id):
            return jsonify({"error": "Location does not belong to the selected team."}), 400

    if zone_id is not None:
        zone = db.session.get(Zone, zone_id)
        if zone is None or zone.organization_id != admin.organization_id:
            return jsonify({"error": "Zone not found in your organization."}), 400
        if location_id is None or zone.location_id != location_id:
            return jsonify({"error": "Zone must belong to the selected location."}), 400

    if assigned_to_id is not None:
        assignee = db.session.get(User, assigned_to_id)
        if (
            assignee is None
            or assignee.organization_id != admin.organization_id
            or assignee.account_status != "active"
            or assignee.role not in {"Supervisor", "Employee"}
        ):
            return jsonify({
                "error": "Assignee must be an active Supervisor or Employee in your organization."
            }), 400

    due_at = None
    due_value = data.get("due_at")
    if due_value:
        if not isinstance(due_value, str):
            return jsonify({"error": "due_at must be an ISO date/time string."}), 400
        try:
            due_at = datetime.fromisoformat(due_value.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "due_at is not a valid ISO date/time."}), 400

    operation = Operation(
        title=title,
        description=description.strip() or None,
        task_type=task_type.strip(),
        status="Assigned" if assigned_to_id is not None else "Created",
        organization_id=admin.organization_id,
        team_id=team_id,
        location_id=location_id,
        zone_id=zone_id,
        created_by_id=admin.id,
        assigned_to_id=assigned_to_id,
        due_at=due_at,
    )

    db.session.add(operation)
    try:
        # Save first so the operation has an ID for its history records.
        db.session.flush()
        write_operation_logs(
            operation,
            admin,
            "assigned" if assigned_to_id is not None else "created",
            None,
            "Operation created" if assigned_to_id is None else "Operation created and assigned",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Operation could not be created."}), 500

    return jsonify({
        "message": "Operation created successfully.",
        "operation": operation.to_dict(),
    }), 201


@operations_bp.post("/<int:operation_id>/start")
@roles_required("Supervisor", "Employee")
def start_operation(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)

    if operation is None or operation.organization_id != user.organization_id:
        return jsonify({"error": "Operation not found."}), 404
    if operation.assigned_to_id != user.id:
        return jsonify({"error": "This operation is not assigned to you."}), 403
    if operation.status not in {"Assigned", "Rejected"}:
        return jsonify({"error": "Only assigned or rejected operations can be started."}), 409

    previous_status = operation.status
    operation.status = "In Progress"
    operation.review_feedback = None

    try:
        write_operation_logs(operation, user, "started", previous_status)
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

    if operation is None or operation.organization_id != user.organization_id:
        return jsonify({"error": "Operation not found."}), 404
    if operation.assigned_to_id != user.id:
        return jsonify({"error": "This operation is not assigned to you."}), 403
    if operation.status != "In Progress":
        return jsonify({"error": "Only operations in progress can be submitted."}), 409

    previous_status = operation.status
    operation.status = "Submitted"
    operation.submitted_at = datetime.now(timezone.utc)

    try:
        write_operation_logs(operation, user, "submitted", previous_status)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not submit operation."}), 500

    return jsonify({
        "message": "Operation submitted for review.",
        "operation": operation.to_dict(),
    }), 200


@operations_bp.post("/<int:operation_id>/review")
@roles_required("Admin")
def review_operation(operation_id):
    admin = get_current_user()
    operation = db.session.get(Operation, operation_id)

    if operation is None or operation.organization_id != admin.organization_id:
        return jsonify({"error": "Operation not found."}), 404
    if operation.status != "Submitted":
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
        return jsonify({"error": "Feedback is required when rejecting work."}), 400

    previous_status = operation.status
    operation.status = "Approved" if decision == "approve" else "Rejected"
    operation.review_feedback = feedback or None
    operation.reviewed_at = datetime.now(timezone.utc)

    try:
        write_operation_logs(
            operation,
            admin,
            "approved" if decision == "approve" else "rejected",
            previous_status,
            feedback or None,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not save operation review."}), 500

    return jsonify({
        "message": "Operation approved." if decision == "approve"
        else "Operation rejected. The assignee can correct and resubmit it.",
        "operation": operation.to_dict(),
    }), 200


@operations_bp.get("/<int:operation_id>/history")
@roles_required("Admin", "Supervisor", "Employee")
def operation_history(operation_id):
    user = get_current_user()
    operation = db.session.get(Operation, operation_id)

    if operation is None or operation.organization_id != user.organization_id:
        return jsonify({"error": "Operation not found."}), 404

    if user.role != "Admin" and operation.assigned_to_id != user.id:
        return jsonify({"error": "Permission denied."}), 403

    history = (
        OperationLog.query
        .filter_by(operation_id=operation.id)
        .order_by(OperationLog.created_at.asc())
        .all()
    )

    return jsonify({
        "operation_id": operation.id,
        "history": [entry.to_dict() for entry in history],
    }), 200


@operations_bp.get("/audit-history")
@roles_required("Admin")
def organization_audit_history():
    admin = get_current_user()

    logs = (
        AuditLog.query
        .filter_by(organization_id=admin.organization_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )

    return jsonify({
        "audit_logs": [log.to_dict() for log in logs],
        "count": len(logs),
    }), 200
