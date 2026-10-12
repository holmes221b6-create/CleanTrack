from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.extensions import db
from app.models.location import Location
from app.models.team import Team
from app.services.permissions import get_current_user, roles_required


locations_bp = Blueprint(
    "locations",
    __name__,
    url_prefix="/api/locations",
)


@locations_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def locations_collection():
    admin = get_current_user()

    if request.method == "GET":
        locations = (
            Location.query
            .filter_by(organization_id=admin.organization_id)
            .order_by(Location.name.asc())
            .all()
        )
        return jsonify({
            "locations": [location.to_dict() for location in locations]
        }), 200

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
        return jsonify({
            "error": "Location name must be 150 characters or fewer."
        }), 400

    if not isinstance(address, str) or not isinstance(description, str):
        return jsonify({
            "error": "Address and description must be text."
        }), 400

    address = address.strip()
    description = description.strip()

    if team_id is not None:
        if isinstance(team_id, bool) or not isinstance(team_id, int):
            return jsonify({"error": "Team ID must be an integer."}), 400

        team = db.session.get(Team, team_id)
        if team is None or team.organization_id != admin.organization_id:
            return jsonify({
                "error": "Team not found in your organization."
            }), 400

    for field_name, value in (
        ("latitude", latitude),
        ("longitude", longitude),
    ):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return jsonify({
                    "error": f"{field_name} must be a number."
                }), 400

    if latitude is not None and not -90 <= latitude <= 90:
        return jsonify({"error": "Latitude must be between -90 and 90."}), 400

    if longitude is not None and not -180 <= longitude <= 180:
        return jsonify({"error": "Longitude must be between -180 and 180."}), 400

    existing = Location.query.filter(
        Location.organization_id == admin.organization_id,
        func.lower(Location.name) == name.lower(),
    ).first()

    if existing:
        return jsonify({
            "error": "A location with this name already exists."
        }), 409

    location = Location(
        name=name,
        address=address or None,
        description=description or None,
        latitude=latitude,
        longitude=longitude,
        team_id=team_id,
        organization_id=admin.organization_id,
    )
    db.session.add(location)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "Location could not be created."
        }), 500

    return jsonify({
        "message": "Location created successfully.",
        "location": location.to_dict(),
    }), 201
