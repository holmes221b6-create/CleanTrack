from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.extensions import db
from app.models.team import Team
from app.services.permissions import get_current_user, roles_required


teams_bp = Blueprint(
    "teams",
    __name__,
    url_prefix="/api/teams",
)


@teams_bp.route("", methods=["GET", "POST"])
@roles_required("Admin")
def teams_collection():
    admin = get_current_user()

    if request.method == "GET":
        teams = (
            Team.query
            .filter_by(organization_id=admin.organization_id)
            .order_by(Team.name.asc())
            .all()
        )
        return jsonify({
            "teams": [team.to_dict() for team in teams]
        }), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    description = data.get("description", "")

    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Team name is required."}), 400

    name = name.strip()
    if len(name) > 120:
        return jsonify({
            "error": "Team name must be 120 characters or fewer."
        }), 400

    if not isinstance(description, str):
        return jsonify({
            "error": "Description must be text."
        }), 400

    description = description.strip()

    existing = Team.query.filter(
        Team.organization_id == admin.organization_id,
        func.lower(Team.name) == name.lower(),
    ).first()

    if existing:
        return jsonify({
            "error": "A team with this name already exists."
        }), 409

    team = Team(
        name=name,
        description=description or None,
        organization_id=admin.organization_id,
    )
    db.session.add(team)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "Team could not be created."
        }), 500

    return jsonify({
        "message": "Team created successfully.",
        "team": team.to_dict(),
    }), 201
