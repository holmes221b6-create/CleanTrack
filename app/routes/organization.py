from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request
from sqlalchemy import case, false, func

from app.extensions import db
from app.models.organization import Organization
from app.models.audit_log import AuditLog
from app.services.audit_retention import audit_cutoff, purge_expired_audit_logs
from app.models.user import User
from app.models.invitation import Invitation
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
    try:
        purge_expired_audit_logs()
    except Exception:
        db.session.rollback()
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
        .filter(AuditLog.organization_id == admin.organization_id, AuditLog.created_at >= audit_cutoff())
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

    if request.method == "PATCH":
        if organization is None:
            return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
        if organization.status == "archived":
            return jsonify({"error": "Restore the organization before editing its settings."}), 423

        allowed = ("name", "description", "avatar_url", "contact_email", "policy_text", "whatsapp_supervisors_group_url")
        changes = {}
        errors = []
        limits = {"name": 150, "description": 5000, "avatar_url": 500, "contact_email": 254, "policy_text": 12000, "whatsapp_supervisors_group_url": 500}
        for field in allowed:
            if field not in data:
                continue
            value = data[field]
            if value is None:
                value = ""
            if not isinstance(value, str):
                errors.append(f"{field} must be text.")
                continue
            value = value.strip()
            if len(value) > limits[field]:
                errors.append(f"{field} must be {limits[field]} characters or fewer.")
                continue
            if field == "name" and len(value) < 2:
                errors.append("Organization name must contain at least two characters.")
                continue
            if field == "whatsapp_supervisors_group_url" and value:
                parsed = urlparse(value)
                if parsed.scheme != "https" or parsed.netloc.lower() != "chat.whatsapp.com" or not parsed.path.strip("/"):
                    errors.append("Supervisor WhatsApp group links must use https://chat.whatsapp.com/ followed by the group invite code.")
                    continue
            if field == "avatar_url" and value:
                parsed = urlparse(value)
                if parsed.scheme not in ("https", "http") or not parsed.netloc:
                    errors.append("Avatar URL must be a valid http:// or https:// URL.")
                    continue
            if field == "contact_email" and value and ("@" not in value or "." not in value.rsplit("@", 1)[-1]):
                errors.append("Enter a valid organization contact email address.")
                continue
            old_value = getattr(organization, field)
            new_value = value if field == "name" else (value or None)
            if old_value != new_value:
                changes[field] = {"from": old_value, "to": new_value}

        if errors:
            return jsonify({"error": " ".join(errors)}), 400
        if not changes:
            return jsonify({
                "message": "No organization settings changed.",
                "organization": _organization_payload(organization),
            }), 200

        for field, values in changes.items():
            setattr(organization, field, values["to"])
        _record_organization_audit(admin, organization, "organization.settings_updated", {
            "changes": changes,
            "changed_fields": list(changes.keys()),
        })
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "Organization settings could not be saved."}), 500

        payload = _organization_payload(organization)
        return jsonify({
            "message": "Organization settings saved.",
            "organization": payload,
            "stats": payload["stats"],
        }), 200

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


@organization_bp.get("/team-scope")
@roles_required("Supervisor")
def supervisor_team_scope():
    supervisor = get_current_user()
    if not supervisor or not supervisor.organization_id:
        return jsonify(teams=[], locations=[], zones=[]), 200

    team_rows = (
        db.session.query(Team.id, Team.name)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .filter(
            TeamMember.user_id == supervisor.id,
            Team.organization_id == supervisor.organization_id,
            Team.status == "active",
        )
        .order_by(func.lower(Team.name))
        .all()
    )
    assigned_team_ids = [row.id for row in team_rows]
    operations = Operation.query.filter(
        Operation.organization_id == supervisor.organization_id,
        Operation.assigned_to_id == supervisor.id,
    ).all()
    location_ids = {item.location_id for item in operations if item.location_id is not None}
    zone_ids = {item.zone_id for item in operations if item.zone_id is not None}
    if assigned_team_ids:
        location_ids.update(
            row[0] for row in db.session.query(Location.id)
            .filter(
                Location.organization_id == supervisor.organization_id,
                Location.team_id.in_(assigned_team_ids),
            ).all()
        )
    locations = Location.query.filter(
        Location.organization_id == supervisor.organization_id,
        Location.id.in_(location_ids) if location_ids else false(),
    ).order_by(func.lower(Location.name)).all()
    zones = Zone.query.filter(
        Zone.organization_id == supervisor.organization_id,
        Zone.id.in_(zone_ids) if zone_ids else false(),
    ).order_by(func.lower(Zone.name)).all()

    return jsonify({
        "teams": [{"id": row.id, "name": row.name} for row in team_rows],
        "locations": [{"id": item.id, "name": item.name} for item in locations],
        "zones": [{"id": item.id, "name": item.name, "location_id": item.location_id} for item in zones],
    }), 200


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

    previous_teams = [
        {"id": row.team_id, "name": row.team.name}
        for row in TeamMember.query.filter_by(user_id=member_id).all()
    ]
    next_teams = [{"id": team.id, "name": team.name} for team in teams]

    try:
        TeamMember.query.filter_by(user_id=member_id).delete(synchronize_session=False)
        for team in teams:
            db.session.add(TeamMember(team_id=team.id, user_id=member_id))
        _record_organization_audit(admin, db.session.get(Organization, admin.organization_id),
            "organization.member_teams_updated", {
                "member_id": member_id,
                "member_name": member.full_name,
                "previous_teams": previous_teams,
                "new_teams": next_teams,
            })
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Team assignments could not be saved."}), 500

    return jsonify({
        "message": "Team assignments saved.",
        "member_id": member_id,
        "team_ids": team_ids,
    }), 200


@organization_bp.get("/staff")
@roles_required("Admin")
def list_organization_staff():
    """Return an organization-scoped supervisor directory and true staff totals."""
    admin = get_current_user()
    if not admin or not admin.organization_id:
        return jsonify(staff=[], supervisors=[], summary={
            "total_staff": 0, "supervisors": 0, "employees": 0,
            "active": 0, "inactive": 0, "termination_pending": 0,
            "suspended": 0, "terminated": 0, "active_supervisors": 0,
            "inactive_supervisors": 0, "active_employees": 0,
            "inactive_employees": 0, "pending_invitations": 0,
        }), 200

    organization_id = admin.organization_id
    staff = User.query.filter(
        User.organization_id == organization_id,
        User.role.in_(("Supervisor", "Employee")),
        User.account_status.in_(("active", "suspended", "termination_pending", "terminated")),
    ).order_by(func.lower(User.full_name)).limit(1000).all()
    now = datetime.now(timezone.utc)
    pending_invites = Invitation.query.filter_by(
        organization_id=organization_id, status="pending",
    ).all()
    pending_invite_count = sum(
        1 for invitation in pending_invites
        if invitation.expires_at is not None
        and (invitation.expires_at.replace(tzinfo=timezone.utc)
             if invitation.expires_at.tzinfo is None
             else invitation.expires_at) > now
    )

    member_ids = [member.id for member in staff]
    team_map = {member_id: {} for member_id in member_ids}
    location_map = {member_id: {} for member_id in member_ids}
    zone_map = {member_id: {} for member_id in member_ids}
    task_map = {member_id: {"total_tasks": 0, "completed_tasks": 0,
                            "missed_tasks": 0, "current_workload": 0}
                for member_id in member_ids}

    if member_ids:
        team_rows = (
            db.session.query(TeamMember.user_id, Team.id, Team.name)
            .join(Team, Team.id == TeamMember.team_id)
            .filter(
                TeamMember.user_id.in_(member_ids),
                Team.organization_id == organization_id,
                Team.status == "active",
            ).all()
        )
        for member_id, team_id, team_name in team_rows:
            team_map[member_id][str(team_id)] = team_name

        assignment_rows = (
            db.session.query(
                Operation.assigned_to_id,
                Location.id, Location.name,
                Zone.id, Zone.name,
            )
            .outerjoin(Location, Location.id == Operation.location_id)
            .outerjoin(Zone, Zone.id == Operation.zone_id)
            .filter(
                Operation.organization_id == organization_id,
                Operation.assigned_to_id.in_(member_ids),
            ).distinct().all()
        )
        for member_id, location_id, location_name, zone_id, zone_name in assignment_rows:
            if location_id is not None and location_name:
                location_map[member_id][str(location_id)] = location_name
            if zone_id is not None and zone_name:
                zone_map[member_id][str(zone_id)] = zone_name

        period_start = now - timedelta(days=30)
        task_rows = (
            db.session.query(
                Operation.assigned_to_id.label("member_id"),
                func.count(Operation.id).label("total_tasks"),
                func.sum(case(
                    (Operation.status.in_(("Approved", "Completed")), 1),
                    else_=0,
                )).label("completed_tasks"),
                func.sum(case(
                    (Operation.overdue.is_(True) |
                     Operation.status.in_(("Rejected", "Missed", "Overdue")), 1),
                    else_=0,
                )).label("missed_tasks"),
                func.sum(case(
                    (Operation.status.notin_(("Approved", "Completed", "Cancelled", "Canceled", "Missed")), 1),
                    else_=0,
                )).label("current_workload"),
            )
            .filter(
                Operation.organization_id == organization_id,
                Operation.assigned_to_id.in_(member_ids),
                Operation.created_at >= period_start,
                Operation.created_at <= now,
            )
            .group_by(Operation.assigned_to_id).all()
        )
        for row in task_rows:
            task_map[row.member_id] = {
                "total_tasks": int(row.total_tasks or 0),
                "completed_tasks": int(row.completed_tasks or 0),
                "missed_tasks": int(row.missed_tasks or 0),
                "current_workload": int(row.current_workload or 0),
            }
            total = int(row.total_tasks or 0)
            task_map[row.member_id]["compliance_pct"] = (
                round(100.0 * int(row.completed_tasks or 0) / total, 1)
                if total else None
            )

    serialized = []
    for member in staff:
        record = member.to_dict()
        record["team_ids"] = list(team_map[member.id].keys())
        record["team_names"] = list(team_map[member.id].values())
        record["location_names"] = list(location_map[member.id].values())
        record["location_ids"] = list(location_map[member.id].keys())
        record["zone_names"] = list(zone_map[member.id].values())
        record["zone_ids"] = list(zone_map[member.id].keys())
        record.update(task_map[member.id])
        if "compliance_pct" not in record:
            record["compliance_pct"] = None
        serialized.append(record)

    active = sum(1 for member in staff if member.account_status == "active")
    inactive_statuses = {"suspended", "terminated"}
    is_inactive = lambda member: member.account_status in inactive_statuses
    supervisors = [record for record in serialized if str(record.get("role", "")).lower() == "supervisor"]
    employees = [record for record in serialized if str(record.get("role", "")).lower() == "employee"]

    return jsonify({
        "staff": serialized,
        "supervisors": supervisors,
        "summary": {
            "total_staff": len(staff),
            "supervisors": len(supervisors),
            "employees": len(employees),
            "active": active,
            "inactive": sum(1 for member in staff if is_inactive(member)),
            "termination_pending": sum(1 for member in staff if member.account_status == "termination_pending"),
            "suspended": sum(1 for member in staff if member.account_status == "suspended"),
            "terminated": sum(1 for member in staff if member.account_status == "terminated"),
            "active_supervisors": sum(1 for member in staff if member.role.lower() == "supervisor" and member.account_status == "active"),
            "inactive_supervisors": sum(1 for member in staff if member.role.lower() == "supervisor" and is_inactive(member)),
            "active_employees": sum(1 for member in staff if member.role.lower() == "employee" and member.account_status == "active"),
            "inactive_employees": sum(1 for member in staff if member.role.lower() == "employee" and is_inactive(member)),
            "pending_invitations": pending_invite_count,
        },
        "workload_period_days": 30,
    }), 200


@organization_bp.post("/members/<int:member_id>/termination")
@roles_required("Admin")
def schedule_member_termination(member_id):
    admin = get_current_user()
    member = db.session.get(User, member_id)
    if (
        member is None
        or member.organization_id != admin.organization_id
        or member.role not in {"Supervisor", "Employee"}
    ):
        return jsonify({"error": "Organization member not found."}), 404
    if member.account_status == "termination_pending":
        return jsonify({"error": "Termination is already scheduled for this member."}), 409
    if member.account_status != "active":
        return jsonify({"error": "Only active members can be scheduled for termination."}), 409

    requested_at = datetime.now(timezone.utc)
    effective_at = requested_at + timedelta(days=7)
    member.account_status = "termination_pending"
    member.termination_requested_at = requested_at
    member.termination_effective_at = effective_at
    _record_organization_audit(admin, db.session.get(Organization, admin.organization_id),
        "organization.member_termination_scheduled", {
            "member_id": member.id,
            "member_name": member.full_name,
            "member_email": member.email,
            "requested_at": requested_at.isoformat(),
            "effective_at": effective_at.isoformat(),
            "grace_period_days": 7,
            "message": "Member remains active during the seven-day termination grace period.",
        })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Termination could not be scheduled."}), 500
    return jsonify({
        "message": "Termination scheduled. The member keeps access during the seven-day grace period.",
        "staff_member": member.to_dict(),
    }), 200


@organization_bp.post("/members/<int:member_id>/termination/cancel")
@roles_required("Admin")
def cancel_member_termination(member_id):
    admin = get_current_user()
    member = db.session.get(User, member_id)
    if (
        member is None
        or member.organization_id != admin.organization_id
        or member.role not in {"Supervisor", "Employee"}
    ):
        return jsonify({"error": "Organization member not found."}), 404
    if member.account_status != "termination_pending":
        return jsonify({"error": "This member does not have a pending termination."}), 409

    previous_effective_at = member.termination_effective_at.isoformat() if member.termination_effective_at else None
    member.account_status = "active"
    member.termination_requested_at = None
    member.termination_effective_at = None
    _record_organization_audit(admin, db.session.get(Organization, admin.organization_id),
        "organization.member_termination_cancelled", {
            "member_id": member.id,
            "member_name": member.full_name,
            "member_email": member.email,
            "previous_effective_at": previous_effective_at,
        })
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Pending termination could not be cancelled."}), 500
    return jsonify({
        "message": "Pending termination cancelled. The member remains active.",
        "staff_member": member.to_dict(),
    }), 200


@organization_bp.post("/members/<int:member_id>/suspend")
@roles_required("Admin")
def suspend_organization_member():
    admin = get_current_user()
    member = User.query.filter_by(
        id=member_id,
        organization_id=admin.organization_id,
    ).first()
    if member is None or member.role not in {"Supervisor", "Employee"}:
        return jsonify({"error": "Organization member not found."}), 404
    if member.account_status == "suspended":
        return jsonify({"error": "This member is already suspended."}), 409
    if member.account_status not in {"active", "termination_pending"}:
        return jsonify({"error": "Only active or termination-pending members can be suspended."}), 409

    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        return jsonify({"error": "Enter a documented reason with at least 10 characters for immediate suspension."}), 400
    reason = reason.strip()
    if len(reason) > 1000:
        return jsonify({"error": "Suspension reason must be 1000 characters or fewer."}), 400

    previous_status = member.account_status
    member.account_status = "suspended"
    _record_organization_audit(
        admin, db.session.get(Organization, admin.organization_id),
        "organization.member_suspended", {
            "member_id": member.id,
            "member_name": member.full_name,
            "member_email": member.email,
            "reason": reason,
            "previous_status": previous_status,
            "new_status": "suspended",
            "scope": "Immediate account/API access suspension",
            "termination_effective_at": member.termination_effective_at.isoformat() if member.termination_effective_at else None,
            "message": "Member access suspended immediately by an Admin with a documented reason.",
        },
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Member could not be suspended."}), 500
    return jsonify({
        "message": "Member access has been suspended immediately. The action and reason were recorded in the organization audit trail.",
        "staff_member": member.to_dict(),
    }), 200


@organization_bp.post("/members/<int:member_id>/suspend/restore")
@roles_required("Admin")
def restore_suspended_organization_member():
    admin = get_current_user()
    member = User.query.filter_by(
        id=member_id,
        organization_id=admin.organization_id,
    ).first()
    if member is None or member.role not in {"Supervisor", "Employee"}:
        return jsonify({"error": "Organization member not found."}), 404
    if member.account_status != "suspended":
        return jsonify({"error": "This member is not suspended."}), 409

    old_status = member.account_status
    deadline = member.termination_effective_at
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    pending_termination = (
        member.termination_requested_at is not None
        and deadline is not None
        and deadline > datetime.now(timezone.utc)
    )
    member.account_status = "termination_pending" if pending_termination else "active"
    _record_organization_audit(
        admin, db.session.get(Organization, admin.organization_id),
        "organization.member_suspension_lifted", {
            "member_id": member.id,
            "member_name": member.full_name,
            "member_email": member.email,
            "previous_status": old_status,
            "new_status": member.account_status,
            "termination_effective_at": member.termination_effective_at.isoformat() if member.termination_effective_at else None,
            "message": "Admin restored member access after a recorded suspension.",
        },
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Member access could not be restored."}), 500
    return jsonify({
        "message": "Member access restored. " + ("The scheduled termination remains in effect." if pending_termination else "The member is active."),
        "staff_member": member.to_dict(),
    }), 200
