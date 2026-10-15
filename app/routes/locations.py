from flask import Blueprint, jsonify, request
from sqlalchemy import func, or_

from app.extensions import db
from app.models.location import Location
from app.models.zone import Zone
from app.models.operation import Operation
from app.models.audit_log import AuditLog
from app.models.team import Team
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
        func.lower(Operation.status).notin_(CLOSED_STATUSES),
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
        if team is None:
            return jsonify({"error": "Team not found in your organization."}), 400
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
                    "error": "This location still has open operations. Resolve or move them before archiving.",
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
        _audit(admin, location, "location.updated", {"changes": changed})
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Location changes could not be saved."}), 500
    return jsonify({"message": "Location updated successfully.", "location": _payload(location)}), 200


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
