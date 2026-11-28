from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

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
    due_soon_count = 0
    closed_statuses = {"completed", "approved", "verified", "cancelled", "canceled"}
    due_soon_cutoff = now + timedelta(hours=24)
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
        # Current overdue KPI excludes work already closed; historical lateness
        # remains available in operation records and reports.
        if overdue and status not in closed_statuses:
            status_counts["overdue"] += 1
        due_at = operation.due_at
        if due_at and status not in closed_statuses:
            comparable_due = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
            comparable_now = now if comparable_due.tzinfo else now.replace(tzinfo=None)
            if comparable_now < comparable_due <= due_soon_cutoff:
                due_soon_count += 1

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
    metrics = {
        "total_operations": total,
        **status_counts,
        "completion_rate": round((finished / total) * 100, 1) if total else None,
        "due_soon": due_soon_count,
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


@supervisor_workspace_bp.get("/insights")
@roles_required("Supervisor")
def supervisor_insights():
    """Return verified, organisation- and team-scoped insight aggregates."""
    from app.models.location import Location
    from app.models.zone import Zone
    from app.models.operation_execution import OperationExecution

    user = get_current_user()
    team_ids = _team_ids(user)
    if len(team_ids) != 1:
        return jsonify({
            "error": "Your account must belong to exactly one active team before team insights can load."
        }), 409

    team_id = next(iter(team_ids))
    team = Team.query.filter_by(
        id=team_id, organization_id=user.organization_id, status="active"
    ).first()
    if team is None:
        return jsonify({"error": "Your assigned team is unavailable."}), 404

    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        return jsonify({"error": "The days parameter must be 7, 30 or 90."}), 400
    if days not in (7, 30, 90):
        return jsonify({"error": "The days parameter must be 7, 30 or 90."}), 400

    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=days - 1)
    start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    end_at = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    locations = Location.query.filter_by(
        organization_id=user.organization_id, team_id=team_id
    ).all()
    location_ids = {row.id for row in locations}
    zones = Zone.query.filter(
        Zone.organization_id == user.organization_id,
        Zone.status == "active",
    ).all()
    team_zones = [
        row for row in zones
        if row.team_id == team_id or row.location_id in location_ids
    ]
    zone_ids = {row.id for row in team_zones}
    location_names = {row.id: row.name for row in locations}

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
    staff_members = [row.user for row in memberships]

    # Reuse the established permission scope, then resolve legacy records
    # through their team, location, or zone assignment.
    scoped_operations = _scoped_operation_query(user).all()

    def as_utc(value):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def insight_timestamp(operation):
        # Count completed work when it was actually submitted/reviewed, even
        # when its operation was originally created before this reporting window.
        if str(operation.status or "").strip().lower() in {
            "completed", "complete", "approved", "verified", "done", "closed"
        }:
            return as_utc(
                operation.reviewed_at or operation.submitted_at
                or operation.updated_at or operation.created_at
            )
        return as_utc(operation.created_at)

    zone_by_id = {zone.id: zone for zone in team_zones}
    operations = []
    for operation in scoped_operations:
        resolved_team_id = operation.team_id
        if resolved_team_id is None and operation.zone_id in zone_by_id:
            zone = zone_by_id[operation.zone_id]
            resolved_team_id = zone.team_id
            if resolved_team_id is None and zone.location_id in location_ids:
                resolved_team_id = team_id
        if resolved_team_id is None and operation.location_id in location_ids:
            resolved_team_id = team_id

        activity_at = insight_timestamp(operation)
        if resolved_team_id == team_id and activity_at and start_at <= activity_at < end_at:
            operations.append(operation)

    done_statuses = {"completed", "complete", "approved", "verified", "done", "closed"}
    closed_statuses = done_statuses | {"cancelled", "canceled", "archived"}
    missed_statuses = {"missed", "failed"}
    def is_done(operation):
        return str(operation.status or "").strip().lower() in done_statuses

    def is_missed(operation):
        status = str(operation.status or "").strip().lower()
        return status in missed_statuses or (
            bool(operation.overdue) and status not in closed_statuses
        )

    total = len(operations)
    completed = sum(1 for item in operations if is_done(item))
    missed = sum(1 for item in operations if is_missed(item))
    compliance = round(completed * 100 / total, 1) if total else None

    daily = {}
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        daily[day.isoformat()] = {"period": day.isoformat(), "total": 0, "completed": 0, "missed": 0}
    for operation in operations:
        activity_at = insight_timestamp(operation)
        if activity_at is None:
            continue
        key = activity_at.date().isoformat()
        if key in daily:
            daily[key]["total"] += 1
            daily[key]["completed"] += int(is_done(operation))
            daily[key]["missed"] += int(is_missed(operation))
    trend = []
    for row in daily.values():
        row["compliance_pct"] = round(row["completed"] * 100 / row["total"], 1) if row["total"] else None
        trend.append(row)

    staff_totals = {
        person.id: {"id": person.id, "name": person.full_name, "total_tasks": 0, "completed": 0}
        for person in staff_members
    }
    for operation in operations:
        person_id = operation.assigned_to_id
        if person_id in staff_totals:
            staff_totals[person_id]["total_tasks"] += 1
            staff_totals[person_id]["completed"] += int(is_done(operation))
    staff_rows = []
    for row in staff_totals.values():
        row["compliance_pct"] = (
            round(row["completed"] * 100 / row["total_tasks"], 1)
            if row["total_tasks"] else None
        )
        staff_rows.append(row)
    staff_rows.sort(key=lambda row: (
        row["compliance_pct"] is None,
        -(row["compliance_pct"] or 0),
        -row["completed"],
        row["name"].casefold(),
    ))

    durations = []
    operation_ids = [item.id for item in operations if is_done(item)]
    if operation_ids:
        executions = OperationExecution.query.filter(
            OperationExecution.operation_id.in_(operation_ids),
            OperationExecution.submitted_at.isnot(None),
        ).all()
        for execution in executions:
            started = execution.started_at
            submitted = execution.submitted_at
            if not started or not submitted:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            minutes = (submitted - started).total_seconds() / 60
            if 0 <= minutes <= 60 * 24 * 30:
                durations.append(minutes)
    avg_duration = round(sum(durations) / len(durations)) if durations else None

    zone_misses = {zone.id: 0 for zone in team_zones}
    for operation in operations:
        if is_missed(operation) and operation.zone_id in zone_misses:
            zone_misses[operation.zone_id] += 1
    zone_rows = [{
        "id": zone.id,
        "name": zone.name,
        "location_name": location_names.get(zone.location_id) or (zone.location.name if zone.location else "Location"),
        "missed_count": zone_misses.get(zone.id, 0),
    } for zone in team_zones]
    zone_rows.sort(key=lambda row: (-row["missed_count"], row["name"].casefold()))

    return jsonify({
        "summary": {
            "staff_count": len(staff_members),
            "total_tasks": total,
            "completed": completed,
            "missed": missed,
            "compliance_pct": compliance,
            "avg_duration": avg_duration,
        },
        "trend": trend,
        "staff": staff_rows,
        "zones": zone_rows,
        "teams": [team.name],
        "period": {"start_date": start_day.isoformat(), "end_date": today.isoformat(), "days": days},
        "methodology": {
            "period_basis": "Open operations are counted by creation date; completed operations are counted by their submission/review date when available, so older work completed in this period is included.",
            "compliance": "Completed operations divided by all recorded operations in the selected period.",
            "average_duration": "Mean elapsed minutes from execution start to submission, using completed executions with timestamps. Null when no such records exist.",
            "missed": "Operations explicitly marked missed/failed or currently marked overdue while not closed.",
        },
    }), 200
