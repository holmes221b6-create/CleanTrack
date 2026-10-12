
from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt, verify_jwt_in_request

from app.extensions import db
from app.models.user import User


VALID_ROLES = {"Admin", "Supervisor", "Employee"}


def get_current_user():
    """Return the active database user for the current JWT."""
    verify_jwt_in_request()

    claims = get_jwt()
    user_id = claims.get("user_id")

    if not user_id:
        return None

    user = db.session.get(User, user_id)

    if user is None or user.account_status != "active":
        return None

    return user


def roles_required(*allowed_roles):
    """Restrict an endpoint to authenticated users with allowed roles."""
    invalid_roles = set(allowed_roles) - VALID_ROLES
    if invalid_roles:
        raise ValueError(
            f"Invalid role(s): {', '.join(sorted(invalid_roles))}"
        )

    def decorator(view_function):
        @wraps(view_function)
        def wrapped(*args, **kwargs):
            try:
                user = get_current_user()
            except Exception:
                return jsonify({"error": "Authentication required"}), 401

            if user is None:
                return jsonify({"error": "Account is unavailable"}), 401

            if user.role not in allowed_roles:
                return jsonify({"error": "Permission denied"}), 403

            return view_function(*args, **kwargs)

        return wrapped

    return decorator