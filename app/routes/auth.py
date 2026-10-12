
from flask import Blueprint, jsonify, request, current_app
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt_identity,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies,
    verify_jwt_in_request,
)
from sqlalchemy import func

from app.extensions import db, limiter
from app.models.user import User
from app.models.organization import Organization
from app.models.membership_request import MembershipRequest

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def json_error(message, status_code):
    return jsonify({"error": message}), status_code


@auth_bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}

    full_name = data.get("full_name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "").strip().capitalize()
    print("REGISTER DEBUG - incoming role:", repr(data.get("role")))
    print("REGISTER DEBUG - normalized role:", repr(role))
    organization_name = data.get("organization_name", "").strip()
    organization_code = data.get("organization_code", "").strip()

    if not full_name or not email or not password:
        return json_error(
            "Full name, email, and password are required.",
            400,
        )

    if role not in {"Admin", "Supervisor", "Employee"}:
        return json_error("A valid role is required.", 400)

    if len(password) < 8:
        return json_error(
            "Password must be at least 8 characters long.",
            400,
        )

    existing_user = User.query.filter(
        func.lower(User.email) == email
    ).first()

    if existing_user:
        return json_error(
            "An account with this email already exists.",
            409,
        )

    organization = None

    if role == "Admin":
        if not organization_name:
            return json_error(
                "Organization name is required for Admin registration.",
                400,
            )

        organization = Organization.query.filter(
            func.lower(Organization.name) == organization_name.lower()
        ).first()

        if organization:
            return json_error(
                "This organization already exists. Ask its Admin for "
                "a membership code.",
                409,
            )

        organization = Organization(name=organization_name)
        db.session.add(organization)
        db.session.flush()

        user = User(
            full_name=full_name,
            email=email,
            role="Admin",
            organization_id=organization.id,
            account_status="active",
            email_verified=False,
        )

        user.set_password(password)
        db.session.add(user)

    else:
        if not organization_code:
            return json_error(
                "Organization code is required.",
                400,
            )

        organization = Organization.query.filter_by(
            organization_code=organization_code
        ).first()

        if organization is None:
            return json_error("Invalid organization code.", 400)

        if organization.status != "active":
            return json_error(
                "This organization is not accepting membership requests.",
                400,
            )

        user = User(
            full_name=full_name,
            email=email,
            role=role,
            organization_id=organization.id,
            account_status="pending",
            email_verified=False,
        )

        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        membership_request = MembershipRequest(
            user_id=user.id,
            organization_id=organization.id,
            requested_role=role,
            status="pending",
        )
        db.session.add(membership_request)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return json_error(
            "Registration could not be completed.",
            500,
        )

    if role == "Admin":
        return jsonify({
            "message": (
                "Admin account created. Email verification has not yet "
                "been configured."
            ),
            "user": user.to_dict(),
            "organization_code": organization.organization_code,
        }), 201

    return jsonify({
        "message": (
            "Membership request submitted. An Admin must approve it "
            "before you can sign in."
        ),
        "user": user.to_dict(),
        "membership_request": membership_request.to_dict(),
    }), 201


@auth_bp.get("/me")
def current_user():
    try:
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        if user is None:
            return json_error("User not found.", 404)
        return jsonify({"user": user.to_dict()}), 200
    except Exception:
        # No valid login cookie means the browser has no active session.
        return json_error("Authentication required.", 401)


@auth_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}

    email = data.get("email")
    password = data.get("password")

    if not isinstance(email, str) or not isinstance(password, str):
        return json_error("Email and password are required.", 400)

    email = email.strip().lower()

    if not email or not password:
        return json_error("Email and password are required.", 400)

    try:
        user = User.query.filter(
            func.lower(User.email) == email
        ).first()

        if user is None or not user.check_password(password):
            return json_error("Invalid email or password.", 401)

        if user.account_status != "active":
            return json_error(
                "Your account is pending approval or unavailable.",
                403,
            )

        access_token = create_access_token(
            identity=str(user.id),
            additional_claims={"user_id": user.id},
        )
        refresh_token = create_refresh_token(identity=str(user.id))

        response = jsonify({
            "message": "Login successful.",
            "user": user.to_dict(),
        })
        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)
        return response, 200

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Login failed unexpectedly.")
        return json_error("Login failed due to a server error.", 500)