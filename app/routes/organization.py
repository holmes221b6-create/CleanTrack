from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request
from sqlalchemy import case, func

from app.extensions import db
from app.models.organization import Organization
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.location import Location
from app.models.zone import Zone
from app.models.operation import Operation
from app.services.permissions import get_current_user, roles_required


organization_bp = Blueprint(
    "organization",
    __name__,
    url_prefix="/api/organizations",
)


def _organization_for_admin(admin):
    if not admin or not admin.organization_id:
        return None
    return db.session.get(Organization, admin.organization_id)


def _stats_for_organization(organization_id):
    staff = User.query.filter(
        User.organization_id == organization_id,
        User.account_status == "active",
        User.role.in_(("Supervisor", "Employee")),
    ).count()
    teams = Team.query.filter_by(
        organization_id=organization_id,
        status="active",
    ).count()
    locations = Location.query.filter_by(
        organization_id=organization_id,
    ).count()
    zones = Zone.query.filter_by(
        organization_id=organization_id,
    ).count()
    return {
        "staff": staff,
        "teams": teams,
        "locations": locations,
        "zones": zones,
    }


def _organization_payload(organization):
    if organization is None:
        return None
    data = organization.to_dict()
    # The existing Admin Home uses "code"; keep organization_code for API clients too.
    data["code"] = organization.organization_code
    data["stats"] = _stats_for_organization(organization.id)
    return data

def _record_organization_audit(actor, organization, action, details):
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        organization_id=organization.id,
        action=action,
        resource_type="Organization",
        resource_id=str(organization.id),
        details=details,
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    )
    db.session.add(entry)


@organization_bp.post("/current/archive")
@roles_required("Admin")
def archive_current_organization():
    admin = get_current_user()
    organization = _organization_for_admin(admin)
    if organization is None:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    if organization.status == "archived":
        return jsonify({"error": "This organization is already archived."}), 409

    data = request.get_json(silent=True) or {}
    if str(data.get("confirm_name") or "").strip() != organization.name:
        return jsonify({"error": "Type the exact organization name to confirm archiving."}), 400

    previous_status = organization.status
    organization.status = "archived"
    _record_organization_audit(admin, organization, "organization.archived", {
        "previous_status": previous_status,
        "new_status": "archived",
        "message": "Organization archived by an Admin. Operational access is suspended; records are retained.",
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Organization could not be archived."}), 500

    return jsonify({
        "message": "Organization archived. Records have been retained and normal operational access is suspended.",
        "organization": _organization_payload(organization),
    }), 200


@organization_bp.post("/current/restore")
@roles_required("Admin")
def restore_current_organization():
    admin = get_current_user()
    organization = _organization_for_admin(admin)
    if organization is None:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    if organization.status != "archived":
        return jsonify({"error": "This organization is not archived."}), 409

    organization.status = "active"
    _record_organization_audit(admin, organization, "organization.restored", {
        "previous_status": "archived",
        "new_status": "active",
        "message": "Organization restored by an Admin.",
    })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Organization could not be restored."}), 500

    return jsonify({
        "message": "Organization restored. Normal access is available again.",
        "organization": _organization_payload(organization),
    }), 200


@organization_bp.get("/audit-log")
@roles_required("Admin")
def organization_audit_log():
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return jsonify(events=[], count=0), 200

    try:
        limit = int(request.args.get("limit", "25"))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 100))

    rows = (
        db.session.query(AuditLog, User.full_name, User.email)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .filter(AuditLog.organization_id == admin.organization_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    events = []
    for entry, actor_name, actor_email in rows:
        events.append({
            **entry.to_dict(),
            "actor_name": actor_name or actor_email or "Unknown account",
        })
    return jsonify(events=events, count=len(events)), 200


@organization_bp.route("/current", methods=["GET", "POST", "PATCH"])
@roles_required("Admin")
def current_organization():
    admin = get_current_user()
    organization = _organization_for_admin(admin)

    if request.method == "GET":
        if organization is None:
            return jsonify(
                organization=None,
                stats={"staff": 0, "teams": 0, "locations": 0, "zones": 0},
            ), 200
        payload = _organization_payload(organization)
        return jsonify(organization=payload, stats=payload["stats"]), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name") or data.get("organization_name") or ""
    if not isinstance(name, str) or len(name.strip()) < 2:
        return jsonify({"error": "Enter an organization name with at least two characters."}), 400

    # Registration normally creates the organization; this supports older active Admin accounts.
    if organization is not None:
        return jsonify({
            "error": "Your account already belongs to an organization.",
            "organization": _organization_payload(organization),
        }), 409

    organization = Organization(name=name.strip())
    admin.organization = organization
    try:
        db.session.add(organization)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Organization profile could not be created."}), 500

    payload = _organization_payload(organization)
    return jsonify({
        "message": "Organization created successfully.",
        "organization": payload,
        "stats": payload["stats"],
    }), 201


@organization_bp.get("/directory-options")
@roles_required("Admin")
def directory_options():
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return jsonify(locations=[], teams=[], zones=[]), 200

    org_id = admin.organization_id
    locations = Location.query.filter_by(organization_id=org_id).order_by(func.lower(Location.name)).all()
    teams = Team.query.filter_by(organization_id=org_id, status="active").order_by(func.lower(Team.name)).all()
    zones = Zone.query.filter_by(organization_id=org_id).order_by(func.lower(Zone.name)).all()
    location_names = {item.id: item.name for item in locations}

    return jsonify({
        "locations": [{"id": item.id, "name": item.name} for item in locations],
        "teams": [{"id": item.id, "name": item.name, "status": item.status} for item in teams],
        "zones": [{
            "id": item.id,
            "name": item.name,
            "location_id": item.location_id,
            "location_name": location_names.get(item.location_id, ""),
        } for item in zones],
    }), 200


@organization_bp.get("/members")
@roles_required("Admin")
def organization_members():
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return jsonify(members=[], count=0, ranking_period_days=30, ranked_count=0), 200

    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90, 365):
        days = 30

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    org_id = admin.organization_id
    users = (
        User.query.filter(
            User.organization_id == org_id,
            User.account_status == "active",
            User.role.in_(("Supervisor", "Employee")),
        )
        .order_by(func.lower(User.full_name))
        .limit(5000)
        .all()
    )
    user_ids = [user.id for user in users]
    if not user_ids:
        return jsonify(members=[], count=0, ranking_period_days=days, ranked_count=0), 200

    task_stats = {}
    stats_query = (
        db.session.query(
            Operation.assigned_to_id.label("member_id"),
            func.count(Operation.id).label("total_tasks"),
            func.sum(case(
                (Operation.status.in_(("Approved", "Completed")), 1),
                else_=0,
            )).label("completed_tasks"),
            func.sum(case(
                (
                    Operation.overdue.is_(True) |
                    Operation.status.in_(("Rejected", "Missed", "Overdue")),
                    1,
                ),
                else_=0,
            )).label("missed_tasks"),
        )
        .filter(
            Operation.organization_id == org_id,
            Operation.assigned_to_id.in_(user_ids),
            Operation.created_at >= start,
            Operation.created_at <= now,
        )
        .group_by(Operation.assigned_to_id)
        .all()
    )
    for row in stats_query:
        total = int(row.total_tasks or 0)
        completed = int(row.completed_tasks or 0)
        missed = int(row.missed_tasks or 0)
        task_stats[row.member_id] = {
            "total_tasks": total,
            "completed_tasks": completed,
            "missed_tasks": missed,
            "compliance_pct": round(100.0 * completed / total, 1) if total else None,
        }

    # Location and zone filters reflect actual operations assigned to each member.
    location_map = {user_id: {} for user_id in user_ids}
    zone_map = {user_id: {} for user_id in user_ids}
    assignment_rows = (
        db.session.query(
            Operation.assigned_to_id,
            Location.id.label("location_id"),
            Location.name.label("location_name"),
            Zone.id.label("zone_id"),
            Zone.name.label("zone_name"),
        )
        .outerjoin(Location, Operation.location_id == Location.id)
        .outerjoin(Zone, Operation.zone_id == Zone.id)
        .filter(
            Operation.organization_id == org_id,
            Operation.assigned_to_id.in_(user_ids),
        )
        .distinct()
        .all()
    )
    for member_id, location_id, location_name, zone_id, zone_name in assignment_rows:
        if location_id is not None and location_name:
            location_map[member_id][str(location_id)] = location_name
        if zone_id is not None and zone_name:
            zone_map[member_id][str(zone_id)] = zone_name

    teams_map = {user_id: {} for user_id in user_ids}
    team_rows = (
        db.session.query(TeamMember.user_id, Team.id, Team.name)
        .join(Team, Team.id == TeamMember.team_id)
        .filter(
            TeamMember.user_id.in_(user_ids),
            Team.organization_id == org_id,
            Team.status == "active",
        )
        .all()
    )
    for member_id, team_id, team_name in team_rows:
        teams_map[member_id][str(team_id)] = team_name

    members = []
    for user in users:
        locations_for_user = location_map[user.id]
        zones_for_user = zone_map[user.id]
        teams_for_user = teams_map[user.id]
        task_data = task_stats.get(user.id, {
            "total_tasks": 0,
            "completed_tasks": 0,
            "missed_tasks": 0,
            "compliance_pct": None,
        })
        members.append({
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "role": user.role.lower(),
            "is_active": user.account_status == "active",
            "membership_status": user.account_status,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "location_id": next(iter(locations_for_user.keys()), None),
            "location_ids": list(locations_for_user.keys()),
            "location_name": next(iter(locations_for_user.values()), None),
            "location_names": list(locations_for_user.values()),
            "team_ids": list(teams_for_user.keys()),
            "team_names": list(teams_for_user.values()),
            "zone_ids": list(zones_for_user.keys()),
            "zone_names": list(zones_for_user.values()),
            **task_data,
            "rank": None,
            "ranking_period_days": days,
        })

    ranked = [
        member for member in members
        if member["role"] in ("employee", "staff")
        and member["total_tasks"] > 0
        and member["compliance_pct"] is not None
    ]
    ranked.sort(key=lambda member: (
        -float(member["compliance_pct"]),
        -int(member["completed_tasks"]),
        int(member["missed_tasks"]),
        str(member["name"] or "").casefold(),
    ))
    for position, member in enumerate(ranked, 1):
        member["rank"] = position

    return jsonify({
        "members": members,
        "count": len(members),
        "ranking_period_days": days,
        "ranked_count": len(ranked),
    }), 200


@organization_bp.post("/members/<int:member_id>/teams")
@roles_required("Admin")
def set_member_teams(member_id):
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409

    data = request.get_json(silent=True) or {}
    team_ids = data.get("team_ids", [])
    if not isinstance(team_ids, list):
        return jsonify({"error": "team_ids must be a list."}), 400
    if any(isinstance(value, bool) or not str(value).isdigit() for value in team_ids):
        return jsonify({"error": "Every team ID must be a valid integer."}), 400
    team_ids = list(dict.fromkeys(int(value) for value in team_ids))

    member = User.query.filter_by(
        id=member_id,
        organization_id=admin.organization_id,
        account_status="active",
    ).first()
    if member is None or member.role not in {"Supervisor", "Employee"}:
        return jsonify({"error": "Member not found in your organization."}), 404

    if team_ids:
        teams = Team.query.filter(
            Team.organization_id == admin.organization_id,
            Team.status == "active",
            Team.id.in_(team_ids),
        ).all()
    else:
        teams = []
    if len(teams) != len(team_ids):
        return jsonify({"error": "One or more teams do not belong to your organization."}), 400

    try:
        TeamMember.query.filter_by(user_id=member_id).delete(synchronize_session=False)
        for team in teams:
            db.session.add(TeamMember(team_id=team.id, user_id=member_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team assignments could not be saved."}), 500

    return jsonify({
        "message": "Team assignments saved.",
        "member_id": member_id,
        "team_ids": team_ids,
    }), 200
