from io import BytesIO

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.location import Location
from app.models.location_floor_plan import LocationFloorPlan
from app.models.zone import Zone
from app.models.operation import Operation
from app.models.audit_log import AuditLog
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.notification import Notification
from app.models.user import User
from app.services.permissions import get_current_user, roles_required

locations_bp = Blueprint("locations", __name__, url_prefix="/api/locations")
CLOSED_STATUSES = {"completed", "complete", "approved", "done", "closed", "cancelled", "canceled"}


def _location_or_none(location_id, admin):
    return Location.query.filter_by(
        id=location_id, organization_id=admin.organization_id
    ).first()


def _payload(location):
    data = location.to_dict()
    data["team_name"] = location.team.name if location.team else None
    floor_plan = LocationFloorPlan.query.filter_by(location_id=location.id).first()
    data["has_floor_plan"] = floor_plan is not None
    data["floor_plan"] = _floor_plan_metadata(floor_plan) if floor_plan else None
    data["zone_count"] = Zone.query.filter_by(
        organization_id=location.organization_id, location_id=location.id
    ).count()
    zone_ids = [row[0] for row in db.session.query(Zone.id).filter_by(
        organization_id=location.organization_id, location_id=location.id
    ).all()]
    operation_filter = [Operation.location_id == location.id]
    if zone_ids:
        operation_filter.append(Operation.zone_id.in_(zone_ids))
    data["operation_count"] = Operation.query.filter(
        Operation.organization_id == location.organization_id,
        or_(*operation_filter),
    ).count()
    data["active_operation_count"] = Operation.query.filter(
        Operation.organization_id == location.organization_id,
        or_(*operation_filter),
        or_(
            func.lower(Operation.status).notin_(CLOSED_STATUSES),
            Operation.schedule_next_run_at.isnot(None),
        ),
    ).count()
    return data


def _audit(admin, location, action, details):
    db.session.add(AuditLog(
        actor_id=admin.id,
        organization_id=admin.organization_id,
        action=action,
        resource_type="Location",
        resource_id=str(location.id),
        details=details or {},
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))


def _notify_admins(admin, location, title, message):
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
            notification_type="location",
            resource_type="Location",
            resource_id=str(location.id),
        ))


@locations_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def locations_collection():
    admin = get_current_user()

    if request.method == "GET":
        rows = Location.query.filter_by(
            organization_id=admin.organization_id
        ).order_by(Location.name.asc()).all()
        return jsonify({"locations": [_payload(row) for row in rows]}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    address = data.get("address", "")
    description = data.get("description", "")
    team_id = data.get("team_id")
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Location name is required."}), 400
    name = name.strip()
    if len(name) > 150:
        return jsonify({"error": "Location name must be 150 characters or fewer."}), 400
    if not isinstance(address, str) or not isinstance(description, str):
        return jsonify({"error": "Address and description must be text."}), 400
    address, description = address.strip(), description.strip()

    if team_id not in (None, ""):
        try:
            if isinstance(team_id, bool):
                raise ValueError()
            team_id = int(team_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Team ID must be an integer."}), 400
        team = Team.query.filter_by(id=team_id, organization_id=admin.organization_id).first()
        if team is None or team.status != "active":
            return jsonify({"error": "Team not found or inactive in your organization."}), 400
    else:
        team_id = None

    for field_name, value in (("latitude", latitude), ("longitude", longitude)):
        if value not in (None, "") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return jsonify({"error": f"{field_name} must be a number."}), 400
    try:
        latitude = None if latitude in (None, "") else float(latitude)
        longitude = None if longitude in (None, "") else float(longitude)
    except (ValueError, TypeError):
        return jsonify({"error": "Latitude and longitude must be valid numbers."}), 400
    if latitude is not None and not -90 <= latitude <= 90:
        return jsonify({"error": "Latitude must be between -90 and 90."}), 400
    if longitude is not None and not -180 <= longitude <= 180:
        return jsonify({"error": "Longitude must be between -180 and 180."}), 400
    if (latitude is None) != (longitude is None):
        return jsonify({"error": "Provide both latitude and longitude, or leave both blank."}), 400

    existing = Location.query.filter(
        Location.organization_id == admin.organization_id,
        func.lower(Location.name) == name.lower(),
    ).first()
    if existing:
        return jsonify({"error": "A location with this name already exists."}), 409

    location = Location(
        name=name, address=address or None, description=description or None,
        latitude=latitude, longitude=longitude, team_id=team_id,
        organization_id=admin.organization_id, status="active",
    )
    db.session.add(location)
    try:
        db.session.flush()
        _audit(admin, location, "location.created", {
            "name": location.name, "address": location.address,
            "description": location.description, "team_id": location.team_id,
            "latitude": location.latitude, "longitude": location.longitude,
            "status": location.status,
        })
        _notify_admins(admin, location, "Location created", f'{admin.full_name} created location "{location.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Location could not be created."}), 500

    return jsonify({
        "message": "Location created successfully.",
        "location": _payload(location),
    }), 201


@locations_bp.route("/<int:location_id>", methods=["GET", "PATCH"])
@roles_required("Admin")
def location_detail(location_id):
    admin = get_current_user()
    location = _location_or_none(location_id, admin)
    if location is None:
        return jsonify({"error": "Location not found in your organization."}), 404

    if request.method == "GET":
        return jsonify({"location": _payload(location)}), 200

    data = request.get_json(silent=True) or {}
    allowed = {"name", "address", "description", "team_id", "latitude", "longitude", "status"}
    unknown = set(data) - allowed
    if unknown:
        return jsonify({"error": "Unsupported field(s): " + ", ".join(sorted(unknown))}), 400

    before = {key: getattr(location, key) for key in allowed}
    if "name" in data:
        value = data["name"]
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 150:
            return jsonify({"error": "Location name is required and must be 150 characters or fewer."}), 400
        name = value.strip()
        duplicate = Location.query.filter(
            Location.organization_id == admin.organization_id,
            func.lower(Location.name) == name.lower(),
            Location.id != location.id,
        ).first()
        if duplicate:
            return jsonify({"error": "A location with this name already exists."}), 409
        location.name = name
    for key, max_len in (("address", 300), ("description", None)):
        if key in data:
            value = data[key]
            if value is not None and not isinstance(value, str):
                return jsonify({"error": f"{key.capitalize()} must be text."}), 400
            value = (value or "").strip()
            if max_len and len(value) > max_len:
                return jsonify({"error": f"{key.capitalize()} must be {max_len} characters or fewer."}), 400
            setattr(location, key, value or None)

    if "team_id" in data:
        value = data["team_id"]
        if value in (None, ""):
            location.team_id = None
        else:
            try:
                if isinstance(value, bool):
                    raise ValueError()
                team_id = int(value)
            except (TypeError, ValueError):
                return jsonify({"error": "Team ID must be an integer."}), 400
            team = Team.query.filter_by(id=team_id, organization_id=admin.organization_id).first()
            if team is None:
                return jsonify({"error": "Team not found in your organization."}), 400
            if team.status != "active" and team_id != location.team_id:
                return jsonify({"error": "New assignments can only use active teams."}), 400
            location.team_id = team_id

    if "latitude" in data or "longitude" in data:
        raw_lat = data.get("latitude", location.latitude)
        raw_lon = data.get("longitude", location.longitude)
        try:
            lat = None if raw_lat in (None, "") else float(raw_lat)
            lon = None if raw_lon in (None, "") else float(raw_lon)
        except (ValueError, TypeError):
            return jsonify({"error": "Latitude and longitude must be valid numbers."}), 400
        if (lat is None) != (lon is None):
            return jsonify({"error": "Provide both latitude and longitude, or leave both blank."}), 400
        if lat is not None and not -90 <= lat <= 90:
            return jsonify({"error": "Latitude must be between -90 and 90."}), 400
        if lon is not None and not -180 <= lon <= 180:
            return jsonify({"error": "Longitude must be between -180 and 180."}), 400
        location.latitude, location.longitude = lat, lon

    if "status" in data:
        status = data["status"]
        if status not in {"active", "inactive", "archived"}:
            return jsonify({"error": "Status must be active, inactive, or archived."}), 400
        if status == "archived" and location.status != "archived":
            payload = _payload(location)
            if payload["active_operation_count"]:
                return jsonify({
                    "error": "This location still has open operations or scheduled work. Resolve or move them before archiving.",
                    "active_operation_count": payload["active_operation_count"],
                    "operation_count": payload["operation_count"],
                    "zone_count": payload["zone_count"],
                }), 409
        location.status = status

    changed = {key: {"before": before[key], "after": getattr(location, key)}
               for key in allowed if before[key] != getattr(location, key)}
    if not changed:
        return jsonify({"message": "No location changes were needed.", "location": _payload(location)}), 200
    try:
        action = "location.updated"
        if "status" in changed:
            old_status, new_status = changed["status"]["before"], changed["status"]["after"]
            action = "location.archived" if new_status == "archived" else "location.restored" if old_status == "archived" else "location.status_changed"
        _audit(admin, location, action, {"changes": changed})
        notification_title = "Location archived" if action == "location.archived" else "Location restored" if action == "location.restored" else "Location updated"
        _notify_admins(admin, location, notification_title, f'{admin.full_name} updated location "{location.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Location changes could not be saved."}), 500
    return jsonify({"message": "Location updated successfully.", "location": _payload(location)}), 200


LOCATION_FLOOR_PLAN_TYPES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".webp": ("image/webp", b"RIFF"),
}
LOCATION_FLOOR_PLAN_MAX_BYTES = 10 * 1024 * 1024


def _floor_plan_metadata(plan):
    return {
        "filename": plan.filename,
        "content_type": plan.content_type,
        "size": plan.size,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


def _can_access_location_floor_plan(user, location):
    if user.role == "Admin":
        return True
    team_ids = {
        row[0] for row in db.session.query(TeamMember.team_id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id).all()
    }
    if location.team_id and location.team_id in team_ids:
        return True
    zone_query = Zone.query.filter_by(
        organization_id=user.organization_id, location_id=location.id
    )
    zone_ids = [row.id for row in zone_query.all()]
    if any(row.supervisor_id == user.id or (row.team_id or location.team_id) in team_ids for row in zone_query):
        return True
    operation_scope = [Operation.location_id == location.id]
    if zone_ids:
        operation_scope.append(Operation.zone_id.in_(zone_ids))
    return Operation.query.filter(
        Operation.organization_id == user.organization_id,
        or_(*operation_scope),
        or_(Operation.assigned_to_id == user.id, Operation.assigned_supervisor_id == user.id),
    ).first() is not None


@locations_bp.post("/<int:location_id>/floor-plan")
@roles_required("Admin")
def upload_location_floor_plan(location_id):
    admin = get_current_user()
    location = _location_or_none(location_id, admin)
    if location is None:
        return jsonify({"error": "Location not found in your organization."}), 404
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "Choose a PNG, JPEG, or WebP floor-plan image."}), 400
    filename = secure_filename(file.filename)
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = LOCATION_FLOOR_PLAN_TYPES.get(extension)
    if expected is None:
        return jsonify({"error": "Floor plans must be PNG, JPEG, or WebP images."}), 400
    data = file.read(LOCATION_FLOOR_PLAN_MAX_BYTES + 1)
    if not data or len(data) > LOCATION_FLOOR_PLAN_MAX_BYTES:
        return jsonify({"error": "Floor-plan images must be between 1 byte and 10 MB."}), 413
    content_type, signature = expected
    if not data.startswith(signature) or (extension == ".webp" and (len(data) < 12 or data[8:12] != b"WEBP")):
        return jsonify({"error": "The uploaded file does not match its image format."}), 400
    plan = LocationFloorPlan.query.filter_by(location_id=location.id).first()
    replacing = plan is not None
    if plan is None:
        plan = LocationFloorPlan(location_id=location.id, uploaded_by_id=admin.id)
        db.session.add(plan)
    plan.filename = filename[:255]
    plan.content_type = content_type
    plan.size = len(data)
    plan.data = data
    plan.uploaded_by_id = admin.id
    try:
        db.session.flush()
        _audit(admin, location, "location.floor_plan_replaced" if replacing else "location.floor_plan_uploaded", {
            "filename": plan.filename, "size": plan.size, "content_type": plan.content_type,
        })
        _notify_admins(admin, location, "Location floor plan updated", f'{admin.full_name} {"replaced" if replacing else "uploaded"} the shared floor plan for location "{location.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Location floor plan could not be saved."}), 500
    return jsonify({"message": "Shared location floor plan saved securely. Zones at this location can draw boundaries on it.", "floor_plan": _floor_plan_metadata(plan)}), 200 if replacing else 201


@locations_bp.get("/<int:location_id>/floor-plan")
@roles_required("Admin", "Supervisor", "Employee")
def get_location_floor_plan(location_id):
    user = get_current_user()
    location = _location_or_none(location_id, user)
    if location is None:
        return jsonify({"error": "Location not found in your organization."}), 404
    if not _can_access_location_floor_plan(user, location):
        return jsonify({"error": "Your account is not assigned to this location or its zones. The private floor plan is not available."}), 403
    plan = LocationFloorPlan.query.filter_by(location_id=location.id).first()
    if plan is None:
        return jsonify({"error": "No shared floor plan has been uploaded for this location."}), 404
    response = send_file(
        BytesIO(plan.data), mimetype=plan.content_type, as_attachment=False,
        download_name=plan.filename, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@locations_bp.delete("/<int:location_id>/floor-plan")
@roles_required("Admin")
def delete_location_floor_plan(location_id):
    admin = get_current_user()
    location = _location_or_none(location_id, admin)
    if location is None:
        return jsonify({"error": "Location not found in your organization."}), 404
    plan = LocationFloorPlan.query.filter_by(location_id=location.id).first()
    if plan is None:
        return jsonify({"error": "No shared floor plan is saved for this location."}), 404
    try:
        _audit(admin, location, "location.floor_plan_removed", {"filename": plan.filename, "size": plan.size})
        db.session.delete(plan)
        _notify_admins(admin, location, "Location floor plan removed", f'{admin.full_name} removed the shared floor plan for location "{location.name}".')
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Location floor plan could not be removed."}), 500
    return jsonify({"message": "Shared floor plan removed. Zone boundary and location history records are retained."}), 200


@locations_bp.get("/<int:location_id>/history")
@roles_required("Admin")
def location_history(location_id):
    admin = get_current_user()
    location = _location_or_none(location_id, admin)
    if location is None:
        return jsonify({"error": "Location not found in your organization."}), 404
    rows = AuditLog.query.filter_by(
        organization_id=admin.organization_id,
        resource_type="Location",
        resource_id=str(location.id),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(100).all()
    return jsonify({"history": [row.to_dict() for row in rows]}), 200
