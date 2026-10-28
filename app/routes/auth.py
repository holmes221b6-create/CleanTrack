
from flask import Blueprint, jsonify, request, current_app
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies,
    verify_jwt_in_request,
)
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from app.models.password_reset_code import PasswordResetCode

from app.extensions import db, limiter
from app.models.user import User
from app.models.organization import Organization
from app.models.membership_request import MembershipRequest

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def json_error(message, status_code):
    return jsonify({"error": message}), status_code


@auth_bp.get("/csrf")
def csrf_token():
    """Return the current access-cookie CSRF token to the authenticated SPA.

    The cookie is host-only for the API domain, so JavaScript on the separate
    frontend domain cannot read it directly. The frontend then sends this
    value in X-CSRF-TOKEN for protected write requests.
    """
    try:
        verify_jwt_in_request()
        token = get_jwt().get("csrf")
        if not token:
            return json_error("A valid session CSRF token is unavailable.", 401)
        return jsonify({"csrf_token": token}), 200
    except Exception:
        return json_error("Authentication required.", 401)



@auth_bp.get("/refresh")
def refresh_access_token():
    """Renew an expired access cookie using the still-valid refresh cookie."""
    try:
        verify_jwt_in_request(refresh=True, locations=["headers", "cookies"])
        identity = get_jwt_identity()
        try:
            user_id = int(identity)
        except (TypeError, ValueError):
            return json_error("Session expired. Please sign in again.", 401)

        user = db.session.get(User, user_id)
        if user is None or user.account_status not in {"active", "termination_pending"}:
            return json_error("Account is unavailable. Please sign in again.", 401)

        if (
            user.organization is not None
            and user.organization.status == "archived"
            and user.role != "Admin"
        ):
            return json_error(
                "This organization is archived. Contact its Admin before signing in again.",
                403,
            )

        access_token = create_access_token(
            identity=str(user.id),
            additional_claims={"user_id": user.id},
        )
        response = jsonify({"message": "Session refreshed.", "token": access_token})
        response.headers["Cache-Control"] = "no-store"
        set_access_cookies(response, access_token)
        return response, 200
    except Exception:
        return json_error("Session expired. Please sign in again.", 401)


@auth_bp.post("/register")
def register():
    from datetime import datetime, timezone
    import hashlib

    from app.models.audit_log import AuditLog
    from app.models.invitation import Invitation
    from app.models.notification import Notification

    data = request.get_json(silent=True) or {}
    full_name = str(data.get("full_name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    role = str(data.get("role") or "").strip().capitalize()
    organization_name = str(data.get("organization_name") or "").strip()
    organization_code = str(data.get("organization_code") or "").strip()
    phone = str(data.get("phone") or "").strip()
    invitation_token = str(data.get("invitation_token") or "").strip()
    request_message = str(data.get("message") or "").strip()
    if len(request_message) > 1000:
        return json_error("Your message must be 1,000 characters or fewer.", 400)

    if not full_name or not email or not password:
        return json_error("Full name, email, and password are required.", 400)
    if len(full_name) < 2 or len(full_name) > 120:
        return json_error("Full name must contain 2 to 120 characters.", 400)
    if len(email) > 254 or len(phone) > 30:
        return json_error("Email or phone number is too long.", 400)
    if not isinstance(data.get("accept_terms", True), bool):
        return json_error("Invalid terms acceptance value.", 400)
    if role not in {"Admin", "Supervisor", "Employee"}:
        return json_error("A valid role is required.", 400)
    if len(password) < 8:
        return json_error("Password must be at least 8 characters long.", 400)

    if User.query.filter(func.lower(User.email) == email).first():
        return json_error("An account with this email already exists.", 409)

    organization = None
    invitation = None

    if invitation_token:
        if role not in {"Supervisor", "Employee"}:
            return json_error("Invitation links can only be used for Supervisor or Employee accounts.", 400)
        token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
        invitation = Invitation.query.filter_by(token_hash=token_hash).first()
        if invitation is None or invitation.status != "pending":
            return json_error("This invitation link is invalid, cancelled, or already used.", 400)
        expiry = invitation.expires_at
        now = datetime.now(timezone.utc)
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                return json_error("This invitation link has expired. Ask the Admin for a new link.", 410)
        if invitation.email.lower() != email:
            return json_error("Use the email address this invitation was issued to.", 400)
        if invitation.role != role:
            return json_error("Choose the role specified in this invitation.", 400)
        organization = invitation.organization
        if organization is None or organization.status != "active":
            return json_error("This organization is not accepting membership requests.", 400)
    elif role == "Admin":
        if not organization_name:
            return json_error("Organization name is required for Admin registration.", 400)
        organization = Organization.query.filter(
            func.lower(Organization.name) == organization_name.lower()
        ).first()
        if organization:
            return json_error("This organization already exists. Ask its Admin for a membership code.", 409)
        organization = Organization(name=organization_name)
        db.session.add(organization)
        db.session.flush()
    else:
        if not organization_code:
            return json_error("Organization code is required.", 400)
        organization = Organization.query.filter_by(organization_code=organization_code).first()
        if organization is None:
            return json_error("Invalid organization code.", 400)
        if organization.status != "active":
            return json_error("This organization is not accepting membership requests.", 400)

    user = User(
        full_name=full_name,
        email=email,
        phone=phone or None,
        role=role,
        organization_id=organization.id,
        account_status="active" if role == "Admin" else "pending",
        email_verified=False,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    if role != "Admin":
        membership_request = MembershipRequest(
            user_id=user.id,
            organization_id=organization.id,
            requested_role=role,
            status="pending",
            message=(request_message or ("Submitted through an Admin invitation link." if invitation else None)),
        )
        db.session.add(membership_request)
        db.session.flush()

        if invitation:
            invitation.status = "accepted"
            invitation.accepted_at = datetime.now(timezone.utc)

        db.session.add(AuditLog(
            actor_id=user.id,
            organization_id=organization.id,
            action="organization.membership_request_submitted",
            resource_type="MembershipRequest",
            resource_id=str(membership_request.id),
            details={
                "request_id": membership_request.id,
                "member_id": user.id,
                "member_name": user.full_name,
                "member_email": user.email,
                "requested_role": role,
                "invitation_id": invitation.id if invitation else None,
            },
            ip_address=request.remote_addr,
            user_agent=(request.headers.get("User-Agent", "")[:300] or None),
        ))

        admins = User.query.filter_by(
            organization_id=organization.id,
            role="Admin",
            account_status="active",
        ).all()
        for admin in admins:
            db.session.add(Notification(
                recipient_id=admin.id,
                organization_id=organization.id,
                title="New membership request",
                message=f"{user.full_name} requested to join as {role}. Review the request on the Invitations page.",
                notification_type="membership_request",
                resource_type="MembershipRequest",
                resource_id=str(membership_request.id),
            ))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return json_error("Registration could not be completed.", 500)

    if role == "Admin":
        return jsonify({
            "message": "Admin account created. Email verification has not yet been configured.",
            "user": user.to_dict(),
            "organization_code": organization.organization_code,
        }), 201

    return jsonify({
        "message": "Membership request submitted. An Admin must approve it before you can sign in. Email notifications are not configured, but the request is in the Admin's Invitations page.",
        "user": user.to_dict(),
        "membership_request": membership_request.to_dict(),
        "membership_status": "pending",
    }), 201


@auth_bp.route("/me", methods=["GET", "PATCH"])
def current_user():
    try:
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        if user is None:
            return json_error("User not found.", 404)
        if user.account_status not in {"active", "termination_pending"}:
            return json_error("Your account is no longer active.", 403)

        if request.method == "PATCH":
            data = request.get_json(silent=True) or {}
            if "full_name" in data:
                full_name = data.get("full_name")
                if not isinstance(full_name, str) or not 2 <= len(full_name.strip()) <= 120:
                    return json_error("Full name must contain 2 to 120 characters.", 400)
                user.full_name = full_name.strip()
            if "phone" in data:
                phone = data.get("phone")
                if phone is not None and not isinstance(phone, str):
                    return json_error("Phone must be text.", 400)
                phone = (phone or "").strip()
                if len(phone) > 30:
                    return json_error("Phone must be 30 characters or fewer.", 400)
                user.phone = phone or None
            if "current_password" in data or "new_password" in data:
                current_password = data.get("current_password")
                new_password = data.get("new_password")
                if not isinstance(current_password, str) or not current_password:
                    return json_error("Current password is required.", 400)
                if not user.check_password(current_password):
                    return json_error("Current password is incorrect.", 400)
                if not isinstance(new_password, str) or len(new_password) < 8:
                    return json_error("New password must be at least 8 characters long.", 400)
                if len(new_password) > 128:
                    return json_error("New password is too long.", 400)
                user.set_password(new_password)

            if "avatar_url" in data:
                avatar_url = data.get("avatar_url")
                if avatar_url is not None and not isinstance(avatar_url, str):
                    return json_error("Avatar URL must be text.", 400)
                avatar_url = (avatar_url or "").strip()
                if len(avatar_url) > 500 or (
                    avatar_url and not avatar_url.startswith(("https://", "http://"))
                ):
                    return json_error("Avatar URL must be an HTTP(S) URL of at most 500 characters.", 400)
                user.avatar_url = avatar_url or None

            if "preferences" in data:
                incoming = data.get("preferences")
                if not isinstance(incoming, dict) or len(str(incoming)) > 20000:
                    return json_error("Preferences must be a valid settings object.", 400)
                allowed = {
                    "language", "dateFormat", "timeFormat", "timeZone",
                    "currency", "theme", "notifications", "reports", "privacy",
                }
                if any(key not in allowed for key in incoming):
                    return json_error("One or more preference fields are not supported.", 400)

                cleaned = {}
                choices = {
                    "language": {"en", "es", "ar", "hi", "ur", "zh"},
                    "dateFormat": {"YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD Mon YYYY"},
                    "timeFormat": {"12", "24"},
                    "theme": {"default", "light", "black"},
                    "currency": {
                        "USD", "EUR", "GBP", "INR", "CNY", "JPY", "KRW", "AED",
                        "SAR", "QAR", "KWD", "BHD", "OMR", "JOD", "SGD", "AUD", "CAD", "CHF", "NZD",
                        "HKD", "TWD", "THB", "IDR", "MYR", "PHP", "VND", "BDT", "PKR", "LKR", "NPR",
                        "ZAR", "NGN", "KES", "EGP", "TRY", "BRL", "MXN", "ARS", "CLP", "COP", "PEN",
                        "UYU", "DOP", "PLN", "CZK", "HUF", "RON", "SEK", "NOK", "DKK", "ISK", "RUB",
                        "ILS", "UAH", "MAD", "GHS", "UGX", "TZS", "ETB", "KZT", "MUR",
                    },
                }
                for key, options in choices.items():
                    if key in incoming:
                        value = incoming[key]
                        if not isinstance(value, str) or value not in options:
                            return json_error(f"Invalid {key} preference.", 400)
                        cleaned[key] = value
                if "timeZone" in incoming:
                    zone = incoming["timeZone"]
                    if not isinstance(zone, str) or not 1 <= len(zone) <= 100:
                        return json_error("Invalid time zone preference.", 400)
                    cleaned["timeZone"] = zone

                nested_rules = {
                    "notifications": {"inApp", "taskUpdates", "announcements", "security", "email"},
                    "reports": {"exportFormat", "dateRange", "includeArchived"},
                    "privacy": {"restricted", "restrictedVisibility"},
                }
                for section, keys in nested_rules.items():
                    if section not in incoming:
                        continue
                    value = incoming[section]
                    if not isinstance(value, dict) or any(key not in keys for key in value):
                        return json_error(f"Invalid {section} preferences.", 400)
                    current = user.preferences.get(section, {}) if isinstance(user.preferences, dict) else {}
                    merged = dict(current) if isinstance(current, dict) else {}
                    for key, setting in value.items():
                        if section in {"notifications", "privacy"}:
                            if not isinstance(setting, bool):
                                return json_error(f"{section} preferences must be true or false.", 400)
                        elif key == "exportFormat":
                            if not isinstance(setting, str) or setting not in {"csv", "xlsx", "json", "pdf"}:
                                return json_error("Invalid export format preference.", 400)
                        elif key == "dateRange":
                            if str(setting) not in {"7", "30", "90", "365"}:
                                return json_error("Invalid report date range preference.", 400)
                            setting = str(setting)
                        elif key == "includeArchived" and not isinstance(setting, bool):
                            return json_error("Include archived must be true or false.", 400)
                        merged[key] = setting
                    cleaned[section] = merged

                merged_preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
                merged_preferences.update(cleaned)
                user.preferences = merged_preferences

            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                return json_error("Personal settings could not be saved.", 500)

        return jsonify({"user": user.to_dict()}), 200
    except Exception:
        db.session.rollback()
        # No valid login cookie means the browser has no active session.
        return json_error("Authentication required.", 401)


@auth_bp.post("/logout")
def logout():
    """Clear the authenticated browser session cookies."""
    response = jsonify({"message": "Logged out successfully."})
    unset_jwt_cookies(response)
    return response, 200


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

        if user.account_status not in {"active", "termination_pending"}:
            return json_error(
                "Your account is pending approval or unavailable.",
                403,
            )

        if user.organization is not None and user.organization.status == "archived" and user.role != "Admin":
            return json_error(
                "This organization is archived. Contact its Admin before signing in again.",
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
            "token": access_token,
            "refresh_token": refresh_token,
        })
        response.headers["Cache-Control"] = "no-store"
        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)
        return response, 200

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Login failed unexpectedly.")
        return json_error("Login failed due to a server error.", 500)

def _send_password_reset_email(recipient, code):
    """Send the one-time recovery code through the configured Resend account."""
    import os
    import resend

    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("RESEND_FROM_EMAIL", "").strip()
    if not api_key or not sender:
        current_app.logger.error("Password reset email is not configured: set RESEND_API_KEY and RESEND_FROM_EMAIL.")
        return False
    resend.api_key = api_key
    resend.Emails.send({
        "from": sender,
        "to": [recipient],
        "subject": "Your CleanTrack password reset code",
        "html": (
            "<div style='font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#172033'>"
            "<h2>Reset your CleanTrack password</h2>"
            "<p>Use this one-time code to reset your password:</p>"
            f"<p style='font-size:30px;font-weight:700;letter-spacing:8px'>{code}</p>"
            "<p>This code expires in 10 minutes. If you did not request this, you can ignore this email.</p>"
            "</div>"
        ),
    })
    return True


@auth_bp.post("/forgot-password")
@limiter.limit("5 per hour")
def forgot_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    if not email or len(email) > 254 or "@" not in email:
        return json_error("Enter the email address registered to your account.", 400)

    # Return the same response whether or not the address exists to prevent account discovery.
    generic = {"message": "If an account exists for that email, a reset code will be sent shortly."}
    user = User.query.filter(func.lower(User.email) == email).first()
    if user is None or user.account_status not in {"active", "termination_pending"}:
        return jsonify(generic), 200

    now = datetime.now(timezone.utc)
    PasswordResetCode.query.filter_by(user_id=user.id, used_at=None).delete(synchronize_session=False)
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = PasswordResetCode(
        user_id=user.id,
        code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        expires_at=now + timedelta(minutes=10),
    )
    db.session.add(record)
    try:
        db.session.commit()
        if not _send_password_reset_email(user.email, code):
            db.session.delete(record)
            db.session.commit()
            return json_error("Password recovery email is not configured. Please contact CleanTrack support.", 503)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not issue password reset code.")
        return json_error("Could not send a password reset email right now. Please try again later.", 503)

    return jsonify(generic), 200


@auth_bp.post("/reset-password")
@limiter.limit("10 per hour")
def reset_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()
    password = data.get("new_password")
    if not email or not isinstance(password, str) or len(password) < 8 or len(password) > 128:
        return json_error("Enter your registered email and a new password of 8 to 128 characters.", 400)
    if len(code) != 6 or not code.isdigit():
        return json_error("Enter the six-digit code sent to your email.", 400)

    user = User.query.filter(func.lower(User.email) == email).first()
    if user is None:
        return json_error("The code is invalid or expired. Request a new code and try again.", 400)

    now = datetime.now(timezone.utc)
    records = PasswordResetCode.query.filter_by(user_id=user.id, used_at=None).order_by(PasswordResetCode.created_at.desc()).all()
    record = next((item for item in records if (
        (item.expires_at.replace(tzinfo=timezone.utc) if item.expires_at.tzinfo is None else item.expires_at) > now
        and secrets.compare_digest(item.code_hash, hashlib.sha256(code.encode("utf-8")).hexdigest())
    )), None)
    if record is None:
        return json_error("The code is invalid or expired. Request a new code and try again.", 400)

    user.set_password(password)
    record.used_at = now
    # Invalidate any other outstanding codes for this account.
    for item in records:
        if item.id != record.id:
            item.used_at = now
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not reset password.")
        return json_error("Your password could not be changed. Please try again.", 500)

    response = jsonify({"message": "Password updated. You can now sign in with your new password."})
    response.headers["Cache-Control"] = "no-store"
    return response, 200
