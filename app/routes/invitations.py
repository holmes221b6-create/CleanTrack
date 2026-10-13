import hashlib
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.invitation import Invitation, generate_invitation_token
from app.models.organization import Organization
from app.models.user import User
from app.services.permissions import get_current_user, roles_required

invitations_bp = Blueprint("invitations", __name__, url_prefix="/api/invitations")


def _utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_expired(invitation):
    expiry = _utc(invitation.expires_at)
    return bool(expiry and expiry <= datetime.now(timezone.utc))


def _frontend_url(token):
    base = os.getenv("FRONTEND_URL", "https://cleantrack-main.onrender.com").strip().rstrip("/")
    return base + "/?invite=" + token


def _audit(admin, action, invitation, details=None):
    db.session.add(AuditLog(
        actor_id=admin.id,
        organization_id=admin.organization_id,
        action=action,
        resource_type="Invitation",
        resource_id=str(invitation.id),
        details={
            "invitation_id": invitation.id,
            "email": invitation.email,
            "role": invitation.role,
            **(details or {}),
        },
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))


def _serialize(invitation):
    item = invitation.to_dict()
    item["effective_status"] = "expired" if invitation.status == "pending" and _is_expired(invitation) else invitation.status
    item["invited_by_name"] = invitation.inviter.full_name if invitation.inviter else None
    item["organization_name"] = invitation.organization.name if invitation.organization else None
    return item


@invitations_bp.get("/resolve")
def resolve_invitation():
    """Safely expose the minimum invitation metadata needed to prefill registration."""
    token = str(request.args.get("token") or "").strip()
    if not token or len(token) > 200:
        return jsonify({"error": "A valid invitation token is required."}), 400
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invitation = Invitation.query.filter_by(token_hash=token_hash).first()
    if invitation is None or invitation.status != "pending":
        return jsonify({"error": "This invitation link is invalid, cancelled, or already used."}), 404
    if _is_expired(invitation):
        return jsonify({"error": "This invitation link has expired. Ask the Admin for a new link."}), 410
    organization = invitation.organization
    if organization is None or organization.status != "active":
        return jsonify({"error": "This organization is not accepting membership requests."}), 410
    return jsonify({
        "invitation": {
            "email": invitation.email,
            "role": invitation.role.lower(),
            "organization_name": organization.name,
            "expires_at": invitation.expires_at.isoformat(),
        },
        "message": "Invitation verified. Submitting registration will still create a pending membership request that an Admin must approve.",
    }), 200


@invitations_bp.get("")
@roles_required("Admin")
def list_invitations():
    admin = get_current_user()
    rows = Invitation.query.filter_by(
        organization_id=admin.organization_id,
    ).order_by(Invitation.created_at.desc()).limit(500).all()
    return jsonify({"invitations": [_serialize(row) for row in rows]}), 200


@invitations_bp.post("")
@roles_required("Admin")
def create_invitation():
    admin = get_current_user()
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    role = str(data.get("role") or "").strip().capitalize()
    if not email or len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return jsonify({"error": "Enter a valid email address."}), 400
    if role not in {"Supervisor", "Employee"}:
        return jsonify({"error": "Invitations can be created for Supervisors or Employees only."}), 400
    organization = db.session.get(Organization, admin.organization_id) if admin.organization_id else None
    if organization is None:
        return jsonify({"error": "Your Admin account is not linked to an organization."}), 409
    if organization.status != "active":
        return jsonify({"error": "Restore the organization before sending invitations."}), 423

    existing_user = User.query.filter(func.lower(User.email) == email).first()
    if existing_user is not None:
        return jsonify({"error": "This email already has a CleanTrack account. Use the existing account/membership workflow instead of creating a new-account invitation."}), 409

    active_invite = Invitation.query.filter_by(
        organization_id=organization.id,
        email=email,
        status="pending",
    ).order_by(Invitation.created_at.desc()).first()
    if active_invite and not _is_expired(active_invite):
        return jsonify({"error": "There is already a pending invitation for this email. Resend it to rotate its link."}), 409

    token = generate_invitation_token()
    invitation = Invitation(
        organization_id=organization.id,
        invited_by_id=admin.id,
        email=email,
        role=role,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.session.add(invitation)
    db.session.flush()
    _audit(admin, "organization.invitation_created", invitation, {"expires_at": invitation.expires_at.isoformat()})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Invitation could not be saved."}), 500
    return jsonify({
        "message": "Invitation created. Share the link yourself; outbound email is not configured.",
        "invitation": _serialize(invitation),
        "invite_url": _frontend_url(token),
        "email_delivery_configured": False,
    }), 201


@invitations_bp.post("/<int:invitation_id>/resend")
@roles_required("Admin")
def resend_invitation(invitation_id):
    admin = get_current_user()
    invitation = db.session.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != admin.organization_id:
        return jsonify({"error": "Invitation not found."}), 404
    if invitation.status not in {"pending"}:
        return jsonify({"error": "Only pending or expired invitations can be renewed. Create a new invitation for this recipient."}), 409
    token = generate_invitation_token()
    invitation.token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invitation.invited_by_id = admin.id
    invitation.expires_at = datetime.now(timezone.utc) + timedelta(days=14)
    _audit(admin, "organization.invitation_renewed", invitation, {"expires_at": invitation.expires_at.isoformat()})
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Invitation link could not be renewed."}), 500
    return jsonify({
        "message": "A new invitation link was generated. The previous link no longer works. Email delivery is not configured.",
        "invitation": _serialize(invitation),
        "invite_url": _frontend_url(token),
        "email_delivery_configured": False,
    }), 200


@invitations_bp.post("/<int:invitation_id>/cancel")
@roles_required("Admin")
def cancel_invitation(invitation_id):
    admin = get_current_user()
    invitation = db.session.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != admin.organization_id:
        return jsonify({"error": "Invitation not found."}), 404
    if invitation.status != "pending":
        return jsonify({"error": "Only a pending invitation can be cancelled."}), 409
    invitation.status = "cancelled"
    _audit(admin, "organization.invitation_cancelled", invitation)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Invitation could not be cancelled."}), 500
    return jsonify({"message": "Invitation cancelled. Its link can no longer be used.", "invitation": _serialize(invitation)}), 200
