from flask import Blueprint, jsonify, request
from flask_jwt_extended import verify_jwt_in_request

from app.extensions import db
from app.models.membership_request import MembershipRequest
from app.models.user import User
from app.models.audit_log import AuditLog
from app.services.permissions import get_current_user, roles_required


membership_bp = Blueprint(
    "membership",
    __name__,
    url_prefix="/api/membership",
)


@membership_bp.get("/requests")
@roles_required("Admin")
def list_membership_requests():
    admin = get_current_user()
    requested_status = str(request.args.get("status", "pending") or "pending").strip().lower()
    allowed_statuses = {"pending", "approved", "rejected", "all"}
    if requested_status not in allowed_statuses:
        return jsonify({"error": "Status must be pending, approved, rejected, or all."}), 400

    query = MembershipRequest.query.filter_by(organization_id=admin.organization_id)
    if requested_status != "all":
        query = query.filter_by(status=requested_status)
    requests_list = query.order_by(MembershipRequest.created_at.desc()).limit(500).all()

    return jsonify({
        "membership_requests": [
            {
                **membership_request.to_dict(),
                "applicant": ({
                    **membership_request.user.to_dict(),
                    "phone": membership_request.user.phone,
                } if membership_request.user else None),
                "reviewer": ({
                    "id": membership_request.reviewer.id,
                    "name": membership_request.reviewer.full_name,
                    "email": membership_request.reviewer.email,
                } if membership_request.reviewer else None),
            }
            for membership_request in requests_list
        ],
        "status_filter": requested_status,
    }), 200


@membership_bp.post("/requests/<int:request_id>/review")
@roles_required("Admin")
def review_membership_request(request_id):
    admin = get_current_user()
    data = request.get_json(silent=True) or {}

    decision = data.get("decision", "").strip().lower()
    review_message = data.get("review_message", "").strip()

    if decision not in {"approve", "reject"}:
        return jsonify({
            "error": "Decision must be 'approve' or 'reject'."
        }), 400

    membership_request = db.session.get(
        MembershipRequest,
        request_id,
    )

    if (
        membership_request is None
        or membership_request.organization_id != admin.organization_id
    ):
        return jsonify({
            "error": "Membership request not found."
        }), 404

    if membership_request.status != "pending":
        return jsonify({
            "error": "This request has already been reviewed."
        }), 409

    applicant = db.session.get(User, membership_request.user_id)

    if applicant is None:
        return jsonify({
            "error": "Applicant account not found."
        }), 404

    if applicant.account_status != "pending":
        return jsonify({
            "error": "Applicant account is not pending."
        }), 409

    if decision == "approve":
        applicant.account_status = "active"
        membership_request.status = "approved"
    else:
        applicant.account_status = "rejected"
        membership_request.status = "rejected"

    membership_request.review_message = review_message or None
    membership_request.reviewed_by_id = admin.id
    membership_request.reviewed_at = db.func.now()

    db.session.add(AuditLog(
        actor_id=admin.id,
        organization_id=admin.organization_id,
        action="organization.membership_request_" + ("approved" if decision == "approve" else "rejected"),
        resource_type="MembershipRequest",
        resource_id=str(membership_request.id),
        details={
            "request_id": membership_request.id,
            "member_id": applicant.id,
            "member_name": applicant.full_name,
            "member_email": applicant.email,
            "requested_role": membership_request.requested_role,
            "decision": decision,
            "review_message": review_message or None,
        },
        ip_address=request.remote_addr,
        user_agent=(request.headers.get("User-Agent", "")[:300] or None),
    ))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "Membership review could not be saved."
        }), 500

    return jsonify({
        "message": (
            "Membership request approved."
            if decision == "approve"
            else "Membership request rejected."
        ),
        "membership_request": membership_request.to_dict(),
        "user": applicant.to_dict(),
    }), 200