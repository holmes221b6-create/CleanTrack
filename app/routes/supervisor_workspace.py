from datetime import datetime, timezone

from flask import Blueprint, jsonify

from app.extensions import db
from app.models.operation import Operation
from app.models.operation_log import OperationLog
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.services.permissions import get_current_user, roles_required
from app.routes.operations import _operation_is_overdue, _team_ids, _scoped_operation_query

supervisor_workspace_bp = Blueprint(
    "supervisor_workspace", __name__, url_prefix="/api/supervisor"
)


@supervisor_workspace_bp.get("/workspace")
@roles_required("Supervisor")
def supervisor_workspace():
    """Return a live, team-scoped workspace assembled only from persisted records."""
    user = get_current_user()
    team_ids = _team_ids(user)

    if len(team_ids) != 1:
        return jsonify({
            "error": (
                "Your account must belong to exactly one active team before "
                "the Supervisor workspace can load."
            ),
            "team": None,
            "members": [],
            "operations": [],
            "activity": [],
            "metrics": None,
        }), 409

    team_id = next(iter(team_ids))
    team = Team.query.filter_by(
        id=team_id, organization_id=user.organization_id, status="active"
    ).first()
    if team is None:
        return jsonify({"error": "Your assigned team is unavailable."}), 404

    memberships = (
        TeamMember.query.join(User, User.id == TeamMember.user_id)
        .filter(
            TeamMember.team_id == team_id,
            User.organization_id == user.organization_id,
            User.role == "Staff",
            User.account_status == "active",
        )
        .order_by(User.full_name.asc())
        .all()
    )
    staff = [row.user for row in memberships]
    # _scoped_operation_query already applies organization, team, assigned-supervisor,
    # assigned-staff, location and zone boundaries. Do not narrow it a second time:
    # legacy operations may be linked to the team through a location or zone.
    operations = (
        _scoped_operation_query(user)
        .order_by(Operation.due_at.asc().nullslast(), Operation.created_at.desc())
        .all()
    )

    now = datetime.now(timezone.utc)
    operation_rows = []
    status_counts = {
        "pending": 0,
        "in_progress": 0,
        "submitted": 0,
        "completed": 0,
        "overdue": 0,
    }
    workload = {person.id: 0 for person in staff}
    for operation in operations:
        status = str(operation.status or "").strip().lower().replace(" ", "_")
        if status in {"pending", "assigned", "scheduled"}:
            status_counts["pending"] += 1
        elif status in {"in_progress", "in progress", "started"}:
            status_counts["in_progress"] += 1
        elif status in {"submitted", "awaiting_review", "pending_review"}:
            status_counts["submitted"] += 1
        elif status in {"completed", "approved", "verified"}:
            status_counts["completed"] += 1

        overdue = _operation_is_overdue(operation, now)
        if overdue:
            status_counts["overdue"] += 1

        if operation.assigned_to_id in workload and status not in {
            "completed", "approved", "verified", "cancelled", "canceled"
        }:
            workload[operation.assigned_to_id] += 1

        row = operation.to_dict()
        row["location_name"] = operation.location.name if operation.location else None
        row["zone_name"] = operation.zone.name if operation.zone else None
        row["team_name"] = operation.team.name if operation.team else None
        row["created_by_role"] = operation.creator.role if operation.creator else None
        row["overdue"] = overdue
        row["calendar_at"] = (
            operation.due_at.isoformat() if operation.due_at else
            operation.created_at.isoformat() if operation.created_at else None
        )
        operation_rows.append(row)

    member_rows = []
    for person in staff:
        member_rows.append({
            "id": person.id,
            "name": person.full_name,
            "role": person.role,
            "avatar_url": person.avatar_url,
            "workload": workload.get(person.id, 0),
            "account_status": person.account_status,
            # Attendance/shift availability is not currently recorded in the data model.
            "availability": "not_recorded",
        })

    operation_ids = [item.id for item in operations]
    activity_rows = []
    if operation_ids:
        logs = (
            OperationLog.query.join(Operation, Operation.id == OperationLog.operation_id)
            .filter(
                Operation.organization_id == user.organization_id,
                Operation.id.in_(operation_ids),
            )
            .order_by(OperationLog.created_at.desc())
            .limit(12)
            .all()
        )
        for log in logs:
            actor = log.actor
            activity_rows.append({
                "id": log.id,
                "operation_id": log.operation_id,
                "operation_title": log.operation.title if log.operation else "Operation",
                "action": log.action,
                "previous_status": log.previous_status,
                "new_status": log.new_status,
                "message": log.message,
                "actor_id": log.actor_id,
                "actor_name": actor.full_name if actor else "Unknown member",
                "actor_role": actor.role if actor else None,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            })

    total = len(operations) - sum(1 for item in operations if str(item.status or "").strip().lower() in {"cancelled", "canceled"})
    finished = status_counts["completed"]
    submitted_for_review = status_counts["submitted"]
    metrics = {
        "total_operations": total,
        **status_counts,
        "completion_rate": round((finished / total) * 100, 1) if total else None,
        "staff_count": len(staff),
        "active_staff_count": len(staff),
        "workload_total": sum(workload.values()),
        "data_as_of": now.isoformat(),
    }

    return jsonify({
        "team": {
            "id": team.id,
            "name": team.name,
            "description": team.description,
            "organization_id": team.organization_id,
        },
        "supervisor": {
            "id": user.id,
            "name": user.full_name,
            "organization_id": user.organization_id,
        },
        "members": member_rows,
        "operations": operation_rows,
        "activity": activity_rows,
        "metrics": metrics,
    }), 200
