from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.location import Location
from app.models.location_floor_plan import LocationFloorPlan
from app.models.zone import Zone
from app.models.zone_floor_plan import ZoneFloorPlan
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.operation import Operation
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.services.permissions import get_current_user, roles_required


zones_bp = Blueprint("zones", __name__, url_prefix="/api/zones")
CLOSED_STATUSES = {"completed", "complete", "approved", "done", "closed", "cancelled", "canceled"}


def _zone_or_none(zone_id, user):
    return Zone.query.filter_by(id=zone_id, organization_id=user.organization_id).first()


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _audit(user, zone, action, details):
    db.session.add(AuditLog(
        actor_id=user.id,
        organization_id=user.organization_id,
        action=action,
        resource_type="Zone",
        resource_id=str(zone.id),
        details=_json_safe(details or {}),
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))


def _notify_admins(user, zone, title, message):
    admins = User.query.filter_by(
        organization_id=user.organization_id,
        role="Admin",
        account_status="active",
    ).all()
    for admin in admins:
        db.session.add(Notification(
            recipient_id=admin.id,
            organization_id=user.organization_id,
            title=title[:160],
            message=message[:1000],
            notification_type="zone",
            resource_type="Zone",
            resource_id=str(zone.id),
        ))


def _ops(zone, include_closed=False):
    query = Operation.query.filter_by(
        organization_id=zone.organization_id,
        zone_id=zone.id,
    )
    if not include_closed:
        query = query.filter(or_(
            func.lower(Operation.status).notin_(CLOSED_STATUSES),
            Operation.schedule_next_run_at.isnot(None),
        ))
    return query.order_by(Operation.created_at.desc(), Operation.id.desc()).all()


def _people_for_team(team_id, organization_id):
    if not team_id:
        return []
    rows = (
        db.session.query(TeamMember, User)
        .join(User, TeamMember.user_id == User.id)
        .filter(
            TeamMember.team_id == team_id,
            User.organization_id == organization_id,
            User.account_status == "active",
            User.role.in_(["Supervisor", "Employee"]),
        )
        .order_by(func.lower(User.full_name))
        .all()
    )
    return [{
        "id": user.id,
        "name": user.full_name,
        "email": user.email,
        "role": user.role,
        "team_id": team_id,
        "assigned_at": membership.assigned_at.isoformat() if membership.assigned_at else None,
    } for membership, user in rows]


def _zone_payload(zone, include_people=False, include_operations=False):
    payload = zone.to_dict()
    location = zone.location
    floor_plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    location_floor_plan = LocationFloorPlan.query.filter_by(location_id=zone.location_id).first()
    payload["has_zone_floor_plan"] = floor_plan is not None
    payload["has_floor_plan"] = floor_plan is not None or location_floor_plan is not None
    payload["floor_plan_source"] = "zone" if floor_plan is not None else "location" if location_floor_plan is not None else None
    payload["has_indoor_boundary"] = bool(zone.indoor_boundary or (floor_plan and floor_plan.boundary))
    effective_team_id = zone.team_id or (location.team_id if location else None)
    team = Team.query.filter_by(id=effective_team_id, organization_id=zone.organization_id).first() if effective_team_id else None
    people = _people_for_team(effective_team_id, zone.organization_id)
    all_operations = _ops(zone, include_closed=True)
    open_operations = [
        row for row in all_operations
        if str(row.status or "").strip().lower() not in CLOSED_STATUSES
        or row.schedule_next_run_at is not None
    ]
    now = datetime.now(timezone.utc)
    scheduled = [
        row for row in all_operations
        if row.schedule_next_run_at is not None
        and (row.schedule_next_run_at.replace(tzinfo=timezone.utc)
             if row.schedule_next_run_at.tzinfo is None else row.schedule_next_run_at) >= now
    ]
    payload.update({
        "effective_team_id": effective_team_id,
        "effective_team_name": team.name if team else None,
        "member_count": len(people),
        "employee_count": sum(1 for row in people if row["role"].lower() == "employee"),
        "supervisor_count": sum(1 for row in people if row["role"].lower() == "supervisor"),
        "operation_count": len(all_operations),
        "active_operation_count": len(open_operations),
        "scheduled_operation_count": len(scheduled),
        "assigned_people": people if include_people else [],
        "assigned_supervisor_names": [row["name"] for row in people if str(row["role"]).lower() == "supervisor"],
        "assigned_employee_names": [row["name"] for row in people if str(row["role"]).lower() == "employee"],
    })
    if include_operations:
        payload["operations"] = [{
            "id": row.id,
            "title": row.title,
            "status": row.status,
            "priority": row.priority,
            "assigned_to_id": row.assigned_to_id,
            "assigned_to_name": row.assignee.full_name if row.assignee else None,
            "assigned_supervisor_id": row.assigned_supervisor_id,
            "assigned_supervisor_name": row.assigned_supervisor.full_name if row.assigned_supervisor else None,
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "schedule_next_run_at": row.schedule_next_run_at.isoformat() if row.schedule_next_run_at else None,
        } for row in all_operations]
    return payload


def _validate_boundary(value):
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise ValueError("Boundary must be a GeoJSON Polygon.")
    coords = value.get("coordinates")
    if not isinstance(coords, list) or not coords or not isinstance(coords[0], list):
        raise ValueError("Boundary polygon coordinates are invalid.")
    ring = coords[0]
    if len(ring) < 4:
        raise ValueError("Add at least three boundary points and close the polygon.")
    normalized = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("Every boundary point must contain longitude and latitude.")
        try:
            lon, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise ValueError("Boundary points must be numeric.")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("A boundary point is outside valid longitude/latitude ranges.")
        normalized.append([lon, lat])
    if normalized[0] != normalized[-1]:
        raise ValueError("The boundary polygon must be closed (last point matches first point).")
    return {"type": "Polygon", "coordinates": [normalized]}


@zones_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def zones_collection():
    admin = get_current_user()

    if request.method == "GET":
        query = Zone.query.filter_by(organization_id=admin.organization_id)
        location_id = request.args.get("location_id")
        status = request.args.get("status")
        if location_id:
            try:
                location_id = int(location_id)
            except ValueError:
                return jsonify({"error": "Location ID must be an integer."}), 400
            query = query.filter_by(location_id=location_id)
        if status and status != "all":
            if status not in {"active", "inactive", "archived"}:
                return jsonify({"error": "Status must be active, inactive, archived, or all."}), 400
            query = query.filter_by(status=status)
        zones = query.order_by(Zone.name.asc(), Zone.id.asc()).all()
        return jsonify({"zones": [_zone_payload(zone) for zone in zones]}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    description = data.get("description", "")
    location_id = data.get("location_id")
    team_id = data.get("team_id")
    supervisor_id = data.get("supervisor_id")
    floor_label = data.get("floor_label", "")
    try:
        boundary = _validate_boundary(data.get("boundary"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Zone name is required."}), 400
    name = name.strip()
    if len(name) > 150:
        return jsonify({"error": "Zone name must be 150 characters or fewer."}), 400
    if not isinstance(description, str) or not isinstance(floor_label, str):
        return jsonify({"error": "Description and floor/area label must be text."}), 400
    if len(floor_label.strip()) > 120:
        return jsonify({"error": "Floor/area label must be 120 characters or fewer."}), 400
    if isinstance(location_id, bool):
        return jsonify({"error": "A valid location_id is required."}), 400
    try:
        location_id = int(location_id)
    except (TypeError, ValueError):
        return jsonify({"error": "A valid location_id is required."}), 400
    location = Location.query.filter_by(
        id=location_id, organization_id=admin.organization_id
    ).first()
    if location is None or location.status != "active":
        return jsonify({"error": "Choose an active location in your organization."}), 400

    team = None
    if team_id not in (None, ""):
        try:
            if isinstance(team_id, bool):
                raise ValueError()
            team_id = int(team_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Team ID must be an integer."}), 400
        team = Team.query.filter_by(id=team_id, organization_id=admin.organization_id).first()
        if team is None or team.status != "active":
            return jsonify({"error": "Choose an active team in your organization."}), 400
        if location.team_id not in (None, team.id):
            return jsonify({"error": "The parent location belongs to another team. Reassign the location first."}), 409
    else:
        team_id = None

    if supervisor_id in (None, ""):
        supervisor_id = None
    else:
        try:
            if isinstance(supervisor_id, bool):
                raise ValueError()
            supervisor_id = int(supervisor_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Supervisor ID must be an integer."}), 400
        supervisor = User.query.filter_by(
            id=supervisor_id,
            organization_id=admin.organization_id,
            role="Supervisor",
            account_status="active",
        ).first()
        if supervisor is None:
            return jsonify({"error": "Choose an active Supervisor in your organization."}), 400

    duplicate = Zone.query.filter(
        Zone.organization_id == admin.organization_id,
        Zone.location_id == location_id,
        func.lower(Zone.name) == name.lower(),
    ).first()
    if duplicate:
        return jsonify({"error": "A zone with this name already exists at this location."}), 409

    zone = Zone(
        name=name,
        description=description.strip() or None,
        floor_label=floor_label.strip() or None,
        boundary=boundary,
        location_id=location.id,
        team_id=team_id,
        supervisor_id=supervisor_id,
        qr_token=uuid4().hex,
        organization_id=admin.organization_id,
        status="active",
    )
    db.session.add(zone)
    try:
        db.session.flush()
        _audit(admin, zone, "zone.created", _zone_payload(zone))
        _notify_admins(admin, zone, "Zone created", f'{admin.full_name} created zone "{zone.name}" at "{location.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Zone could not be created."}), 500

    return jsonify({"message": "Zone created successfully.", "zone": _zone_payload(zone, include_people=True)}), 201


@zones_bp.route("/<int:zone_id>", methods=["GET", "PATCH"])
@roles_required("Admin")
def zone_detail(zone_id):
    admin = get_current_user()
    zone = _zone_or_none(zone_id, admin)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    if request.method == "GET":
        return jsonify({"zone": _zone_payload(zone, include_people=True, include_operations=True)}), 200

    data = request.get_json(silent=True) or {}
    allowed = {"name", "description", "location_id", "team_id", "supervisor_id", "status", "floor_label", "boundary", "qr_enabled", "qr_expires_at", "regenerate_qr"}
    unknown = set(data) - allowed
    if unknown:
        return jsonify({"error": "Unsupported field(s): " + ", ".join(sorted(unknown))}), 400

    before = {
        key: getattr(zone, key)
        for key in allowed
        if key not in {"location_id", "team_id", "boundary", "regenerate_qr"}
    }
    before.update({
        "location_id": zone.location_id,
        "team_id": zone.team_id,
        "boundary": zone.boundary,
    })

    if "name" in data:
        value = data["name"]
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 150:
            return jsonify({"error": "Zone name is required and must be 150 characters or fewer."}), 400
        name = value.strip()
        duplicate = Zone.query.filter(
            Zone.organization_id == admin.organization_id,
            Zone.location_id == zone.location_id,
            func.lower(Zone.name) == name.lower(),
            Zone.id != zone.id,
        ).first()
        if duplicate:
            return jsonify({"error": "A zone with this name already exists at this location."}), 409
        zone.name = name

    if "description" in data:
        value = data["description"]
        if value is not None and not isinstance(value, str):
            return jsonify({"error": "Description must be text."}), 400
        zone.description = (value or "").strip() or None

    if "floor_label" in data:
        value = data["floor_label"]
        if value is not None and not isinstance(value, str):
            return jsonify({"error": "Floor/area label must be text."}), 400
        value = (value or "").strip()
        if len(value) > 120:
            return jsonify({"error": "Floor/area label must be 120 characters or fewer."}), 400
        zone.floor_label = value or None

    if "location_id" in data:
        value = data["location_id"]
        try:
            if isinstance(value, bool):
                raise ValueError()
            location_id = int(value)
        except (TypeError, ValueError):
            return jsonify({"error": "A valid parent location is required."}), 400
        location = Location.query.filter_by(
            id=location_id, organization_id=admin.organization_id
        ).first()
        if location is None or (location.status != "active" and location_id != zone.location_id):
            return jsonify({"error": "Choose an active location for a new parent assignment."}), 400
        duplicate = Zone.query.filter(
            Zone.organization_id == admin.organization_id,
            Zone.location_id == location_id,
            func.lower(Zone.name) == zone.name.lower(),
            Zone.id != zone.id,
        ).first()
        if duplicate:
            return jsonify({"error": "A zone with this name already exists at the selected location."}), 409
        if zone.team_id and location.team_id not in (None, zone.team_id):
            return jsonify({"error": "The selected location is assigned to another team. Resolve the team assignment first."}), 409
        zone.location_id = location_id

    if "team_id" in data:
        value = data["team_id"]
        if value in (None, ""):
            zone.team_id = None
        else:
            try:
                if isinstance(value, bool):
                    raise ValueError()
                team_id = int(value)
            except (TypeError, ValueError):
                return jsonify({"error": "Team ID must be an integer."}), 400
            team = Team.query.filter_by(id=team_id, organization_id=admin.organization_id).first()
            if team is None or (team.status != "active" and team_id != zone.team_id):
                return jsonify({"error": "New assignments require an active team in your organization."}), 400
            if zone.status != "active" and team_id != zone.team_id:
                return jsonify({"error": "Activate or restore this zone before assigning a team."}), 409
            location = zone.location
            if location and location.team_id not in (None, team.id):
                return jsonify({"error": "The parent location belongs to another team. Reassign the location first."}), 409
            zone.team_id = team_id

    if "supervisor_id" in data:
        value = data["supervisor_id"]
        if value in (None, ""):
            zone.supervisor_id = None
        else:
            try:
                if isinstance(value, bool):
                    raise ValueError()
                supervisor_id = int(value)
            except (TypeError, ValueError):
                return jsonify({"error": "Supervisor ID must be an integer."}), 400
            supervisor = User.query.filter_by(
                id=supervisor_id,
                organization_id=admin.organization_id,
                role="Supervisor",
            ).first()
            if supervisor is None or (supervisor.account_status != "active" and supervisor_id != zone.supervisor_id):
                return jsonify({"error": "New zone assignments require an active Supervisor in your organization."}), 400
            zone.supervisor_id = supervisor_id

    if "boundary" in data:
        try:
            zone.boundary = _validate_boundary(data.get("boundary"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    if "qr_enabled" in data:
        if not isinstance(data["qr_enabled"], bool):
            return jsonify({"error": "qr_enabled must be true or false."}), 400
        zone.qr_enabled = data["qr_enabled"]

    if "qr_expires_at" in data:
        raw_expiry = data["qr_expires_at"]
        if raw_expiry in (None, ""):
            zone.qr_expires_at = None
        elif not isinstance(raw_expiry, str):
            return jsonify({"error": "QR expiry must be a valid ISO date/time or blank."}), 400
        else:
            try:
                expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                zone.qr_expires_at = expiry.astimezone(timezone.utc)
            except ValueError:
                return jsonify({"error": "QR expiry must be a valid ISO date/time or blank."}), 400

    if "regenerate_qr" in data:
        if not isinstance(data["regenerate_qr"], bool):
            return jsonify({"error": "regenerate_qr must be true or false."}), 400
        if data["regenerate_qr"]:
            before["qr_token"] = zone.qr_token
            zone.qr_token = uuid4().hex

    if "status" in data:
        status = data["status"]
        if status not in {"active", "inactive", "archived"}:
            return jsonify({"error": "Status must be active, inactive, or archived."}), 400
        if status == "active" and zone.location is not None and zone.location.status != "active":
            return jsonify({"error": "Activate or restore the parent location before activating this zone."}), 409
        if status == "archived" and zone.status != "archived":
            payload = _zone_payload(zone, include_people=True, include_operations=True)
            blockers = [
                op for op in payload["operations"]
                if str(op["status"] or "").strip().lower() not in CLOSED_STATUSES
                or op["schedule_next_run_at"] is not None
            ]
            if blockers:
                return jsonify({
                    "error": "This zone still has active operations or scheduled work. Reassign or resolve them before archiving. No records were moved or deleted.",
                    "active_operation_count": len(blockers),
                    "assigned_people_count": payload["member_count"],
                    "scheduled_operation_count": payload["scheduled_operation_count"],
                    "dependencies": {
                        "active_operations": len(blockers),
                        "assigned_people": payload["member_count"],
                        "scheduled_operations": payload["scheduled_operation_count"],
                    },
                }), 409
        zone.status = status

    changed_keys = (allowed - {"regenerate_qr"}) | ({"qr_token"} if "qr_token" in before else set())
    changed = {key: {"before": before.get(key), "after": getattr(zone, key)}
               for key in changed_keys if before.get(key) != getattr(zone, key)}
    if not changed:
        return jsonify({"message": "No zone changes were needed.", "zone": _zone_payload(zone, include_people=True)}), 200

    old_status = before.get("status")
    new_status = zone.status
    if "qr_token" in changed:
        action = "zone.qr_regenerated"
    elif "qr_enabled" in changed:
        action = "zone.qr_enabled" if zone.qr_enabled else "zone.qr_disabled"
    elif "qr_expires_at" in changed:
        action = "zone.qr_expiry_changed"
    elif "status" in changed:
        action = "zone.archived" if new_status == "archived" else "zone.restored" if old_status == "archived" and new_status == "active" else "zone.status_changed"
    elif "boundary" in changed:
        action = "zone.boundary_updated"
    elif "team_id" in changed:
        action = "zone.team_assignment_changed"
    elif "supervisor_id" in changed:
        action = "zone.supervisor_assignment_changed"
    elif "location_id" in changed:
        action = "zone.location_changed"
    else:
        action = "zone.updated"
    try:
        _audit(admin, zone, action, {"changes": changed})
        _notify_admins(admin, zone, "Zone updated", f'{admin.full_name} updated zone "{zone.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Zone changes could not be saved."}), 500

    return jsonify({"message": "Zone updated successfully.", "zone": _zone_payload(zone, include_people=True)}), 200


@zones_bp.get("/<int:zone_id>/history")
@roles_required("Admin")
def zone_history(zone_id):
    admin = get_current_user()
    zone = _zone_or_none(zone_id, admin)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    rows = AuditLog.query.filter_by(
        organization_id=admin.organization_id,
        resource_type="Zone",
        resource_id=str(zone.id),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(100).all()
    return jsonify({"history": [{
        **row.to_dict(),
        "actor_name": row.actor.full_name if row.actor else "System",
    } for row in rows]}), 200


FLOOR_PLAN_TYPES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".webp": ("image/webp", b"RIFF"),
}
FLOOR_PLAN_MAX_BYTES = 10 * 1024 * 1024


def _can_access_floor_plan(user, zone):
    if user.role == "Admin":
        return True
    effective_team_id = zone.team_id or (zone.location.team_id if zone.location else None)
    member_team_ids = {
        row[0] for row in db.session.query(TeamMember.team_id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(
            TeamMember.user_id == user.id,
            Team.organization_id == user.organization_id,
        ).all()
    }
    if zone.supervisor_id == user.id or (effective_team_id and effective_team_id in member_team_ids):
        return True
    return Operation.query.filter(
        Operation.organization_id == user.organization_id,
        Operation.zone_id == zone.id,
        or_(Operation.assigned_to_id == user.id, Operation.assigned_supervisor_id == user.id),
    ).first() is not None


def _floor_plan_metadata(plan):
    return {
        "filename": plan.filename,
        "content_type": plan.content_type,
        "size": plan.size,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


@zones_bp.post("/<int:zone_id>/floor-plan")
@roles_required("Admin")
def upload_zone_floor_plan(zone_id):
    admin = get_current_user()
    zone = _zone_or_none(zone_id, admin)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "Choose a PNG, JPEG, or WebP floor-plan image."}), 400
    filename = secure_filename(file.filename)
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = FLOOR_PLAN_TYPES.get(extension)
    if expected is None:
        return jsonify({"error": "Floor plans must be PNG, JPEG, or WebP images."}), 400
    data = file.read(FLOOR_PLAN_MAX_BYTES + 1)
    if not data or len(data) > FLOOR_PLAN_MAX_BYTES:
        return jsonify({"error": "Floor-plan images must be between 1 byte and 10 MB."}), 413
    content_type, signature = expected
    if not data.startswith(signature) or (extension == ".webp" and (len(data) < 12 or data[8:12] != b"WEBP")):
        return jsonify({"error": "The uploaded file does not match its image format."}), 400

    plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    replacing = plan is not None
    if plan is None:
        plan = ZoneFloorPlan(zone_id=zone.id, uploaded_by_id=admin.id)
        db.session.add(plan)
    plan.filename = filename[:255]
    plan.content_type = content_type
    plan.size = len(data)
    plan.data = data
    plan.uploaded_by_id = admin.id
    try:
        db.session.flush()
        _audit(admin, zone, "zone.floor_plan_replaced" if replacing else "zone.floor_plan_uploaded", {
            "filename": plan.filename, "size": plan.size, "content_type": plan.content_type,
        })
        _notify_admins(admin, zone, "Zone floor plan updated", f'{admin.full_name} {"replaced" if replacing else "uploaded"} the floor plan for zone "{zone.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Floor plan could not be saved to the database."}), 500
    return jsonify({"message": "Floor plan saved securely to CleanTrack.", "floor_plan": _floor_plan_metadata(plan)}), 201 if not replacing else 200


@zones_bp.get("/<int:zone_id>/floor-plan")
@roles_required("Admin", "Supervisor", "Employee")
def get_zone_floor_plan(zone_id):
    user = get_current_user()
    zone = _zone_or_none(zone_id, user)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    if not _can_access_floor_plan(user, zone):
        return jsonify({"error": "You are not assigned to this zone. The private floor plan is not available to your account."}), 403
    plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    if plan is None:
        return jsonify({"error": "No floor plan has been uploaded for this zone."}), 404
    response = send_file(
        BytesIO(plan.data), mimetype=plan.content_type, as_attachment=False,
        download_name=plan.filename, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@zones_bp.get("/<int:zone_id>/floor-plan/boundary")
@roles_required("Admin", "Supervisor", "Employee")
def get_zone_floor_plan_boundary(zone_id):
    user = get_current_user()
    zone = _zone_or_none(zone_id, user)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    if not _can_access_floor_plan(user, zone):
        return jsonify({"error": "You are not assigned to this zone. The private floor plan is not available to your account."}), 403
    plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    shared_plan = LocationFloorPlan.query.filter_by(location_id=zone.location_id).first()
    boundary = zone.indoor_boundary or (plan.boundary if plan else None)
    metadata = _floor_plan_metadata(plan) if plan else _floor_plan_metadata(shared_plan) if shared_plan else None
    return jsonify({"floor_plan": metadata, "source": "zone" if plan else "location" if shared_plan else None, "boundary": boundary}), 200


@zones_bp.patch("/<int:zone_id>/floor-plan/boundary")
@roles_required("Admin")
def save_zone_floor_plan_boundary(zone_id):
    admin = get_current_user()
    zone = _zone_or_none(zone_id, admin)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    shared_plan = LocationFloorPlan.query.filter_by(location_id=zone.location_id).first()
    if plan is None and shared_plan is None:
        return jsonify({"error": "Upload a floor plan for this zone or its parent location before drawing an indoor boundary."}), 404
    data = request.get_json(silent=True) or {}
    points = data.get("points")
    if points is None:
        zone.indoor_boundary = None
        if plan is not None:
            plan.boundary = None
    else:
        if not isinstance(points, list) or len(points) < 3 or len(points) > 500:
            return jsonify({"error": "An indoor boundary needs between 3 and 500 points."}), 400
        checked = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                return jsonify({"error": "Each boundary point must contain normalized x and y coordinates."}), 400
            x, y = point
            if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return jsonify({"error": "Boundary coordinates must be numbers."}), 400
            if not 0 <= x <= 1 or not 0 <= y <= 1:
                return jsonify({"error": "Indoor boundary coordinates must be between 0 and 1."}), 400
            checked.append([round(float(x), 6), round(float(y), 6)])
        if checked[0] == checked[-1]:
            checked.pop()
        if len(checked) < 3 or len(set((p[0], p[1]) for p in checked)) < 3:
            return jsonify({"error": "An indoor boundary needs at least three distinct points."}), 400
        zone.indoor_boundary = {"type": "IndoorPolygon", "points": checked}
        if plan is not None:
            plan.boundary = zone.indoor_boundary
    try:
        _audit(admin, zone, "zone.indoor_boundary_updated", {
            "floor_plan": plan.filename if plan else shared_plan.filename,
            "floor_plan_source": "zone" if plan else "location",
            "boundary": zone.indoor_boundary,
        })
        _notify_admins(admin, zone, "Indoor zone boundary updated", f'{admin.full_name} updated the indoor boundary for zone "{zone.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Indoor boundary could not be saved."}), 500
    return jsonify({"message": "Indoor boundary saved and audited.", "boundary": zone.indoor_boundary}), 200


@zones_bp.delete("/<int:zone_id>/floor-plan")
@roles_required("Admin")
def delete_zone_floor_plan(zone_id):
    admin = get_current_user()
    zone = _zone_or_none(zone_id, admin)
    if zone is None:
        return jsonify({"error": "Zone not found in your organization."}), 404
    plan = ZoneFloorPlan.query.filter_by(zone_id=zone.id).first()
    if plan is None:
        return jsonify({"error": "No floor plan is saved for this zone."}), 404
    details = {"filename": plan.filename, "size": plan.size}
    try:
        _audit(admin, zone, "zone.floor_plan_removed", details)
        db.session.delete(plan)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Floor plan could not be removed."}), 500
    return jsonify({"message": "Floor plan removed. Zone history has been retained."}), 200


@zones_bp.get("/resolve/<string:qr_token>")
@roles_required("Admin", "Supervisor", "Employee")
def resolve_zone_qr(qr_token):
    user = get_current_user()
    zone = Zone.query.filter_by(
        qr_token=qr_token,
        organization_id=user.organization_id,
    ).first()
    if zone is None:
        return jsonify({"error": "This QR code is invalid or does not belong to your organization.", "code_status": "invalid"}), 404

    location = zone.location
    if not zone.qr_enabled:
        return jsonify({"code_status": "disabled", "message": "This QR code has been disabled by an Admin. Ask for a current code."}), 410
    if zone.qr_expires_at is not None:
        expiry = zone.qr_expires_at.replace(tzinfo=timezone.utc) if zone.qr_expires_at.tzinfo is None else zone.qr_expires_at
        if expiry <= datetime.now(timezone.utc):
            return jsonify({"code_status": "expired", "message": "This QR code has expired. Ask an Admin for a current code."}), 410
    if zone.status == "archived" or (location and location.status == "archived"):
        return jsonify({"code_status": "archived", "message": "This zone is no longer active. Contact your organization Admin. The QR code does not grant access."}), 410
    if zone.status != "active" or (location and location.status != "active"):
        return jsonify({"code_status": "inactive", "message": "This zone or its parent location is inactive. Contact your organization Admin."}), 410

    team_ids = [
        row[0] for row in db.session.query(TeamMember.team_id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id).all()
    ]
    assigned_ops = Operation.query.filter_by(
        organization_id=user.organization_id,
        zone_id=zone.id,
    ).all()
    is_assigned = user.role == "Admin" or zone.supervisor_id == user.id or bool(
        (zone.team_id or (location.team_id if location else None)) in team_ids
    ) or any(op.assigned_to_id == user.id or op.assigned_supervisor_id == user.id for op in assigned_ops)
    base = {
        "code_status": "active",
        "zone": {
            "id": zone.id, "name": zone.name,
            "description": zone.description,
            "floor_label": zone.floor_label,
            "location_id": zone.location_id,
            "location_name": location.name if location else None,
            "team_id": zone.team_id or (location.team_id if location else None),
            "team_name": zone.team.name if zone.team else (location.team.name if location and location.team else None),
            "supervisor_id": zone.supervisor_id,
            "supervisor_name": zone.supervisor.full_name if zone.supervisor else None,
        },
        "is_assigned": is_assigned,
    }
    if not is_assigned:
        return jsonify({
            **base,
            "message": "This code identifies the work area, but your account is not assigned to this zone. Scanning the code does not grant access to private operations.",
            "operations": [],
        }), 200

    if user.role == "Admin":
        visible = assigned_ops
    elif user.role == "Supervisor":
        effective_team_id = zone.team_id or (location.team_id if location else None)
        visible = assigned_ops if zone.supervisor_id == user.id or effective_team_id in team_ids else [
            op for op in assigned_ops
            if op.team_id in team_ids or op.assigned_supervisor_id == user.id or op.assigned_to_id == user.id
        ]
    else:
        visible = [op for op in assigned_ops if op.assigned_to_id == user.id]
    base["operations"] = [{
        "id": op.id, "title": op.title, "status": op.status,
        "priority": op.priority, "due_at": op.due_at.isoformat() if op.due_at else None,
    } for op in visible]
    base["message"] = "Zone identified. Only operations available to your role and assignments are shown."
    return jsonify(base), 200
