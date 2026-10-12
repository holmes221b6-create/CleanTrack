from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.extensions import db
from app.models.location import Location
from app.models.zone import Zone
from app.services.permissions import get_current_user, roles_required


zones_bp = Blueprint(
    "zones",
    __name__,
    url_prefix="/api/zones",
)


@zones_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def zones_collection():
    admin = get_current_user()

    if request.method == "GET":
        query = Zone.query.filter_by(
            organization_id=admin.organization_id
        )

        location_id = request.args.get("location_id")
        if location_id:
            try:
                location_id = int(location_id)
            except ValueError:
                return jsonify({
                    "error": "Location ID must be an integer."
                }), 400

            query = query.filter_by(location_id=location_id)

        zones = query.order_by(Zone.name.asc()).all()
        return jsonify({
            "zones": [zone.to_dict() for zone in zones]
        }), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    description = data.get("description", "")
    location_id = data.get("location_id")

    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Zone name is required."}), 400

    name = name.strip()
    if len(name) > 150:
        return jsonify({
            "error": "Zone name must be 150 characters or fewer."
        }), 400

    if not isinstance(description, str):
        return jsonify({
            "error": "Description must be text."
        }), 400

    if isinstance(location_id, bool) or not isinstance(location_id, int):
        return jsonify({
            "error": "A valid location_id is required."
        }), 400

    location = db.session.get(Location, location_id)
    if (
        location is None
        or location.organization_id != admin.organization_id
    ):
        return jsonify({
            "error": "Location not found in your organization."
        }), 400

    existing = Zone.query.filter(
        Zone.location_id == location_id,
        func.lower(Zone.name) == name.lower(),
    ).first()

    if existing:
        return jsonify({
            "error": "A zone with this name already exists at this location."
        }), 409

    zone = Zone(
        name=name,
        description=description.strip() or None,
        location_id=location_id,
        organization_id=admin.organization_id,
    )
    db.session.add(zone)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "Zone could not be created."
        }), 500

    return jsonify({
        "message": "Zone created successfully.",
        "zone": zone.to_dict(),
    }), 201
