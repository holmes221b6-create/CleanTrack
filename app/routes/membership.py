from flask import Blueprint, jsonify, request
import os
import smtplib
from email.message import EmailMessage
from flask_jwt_extended import verify_jwt_in_request

from app.extensions import db
from app.models.membership_request import MembershipRequest
from app.models.user import User
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.organization import Organization
from app.services.permissions import get_current_user, roles_required


membership_bp = Blueprint(
    "membership",
    __name__,
    url_prefix="/api/membership",
)


def _send_membership_approval_email(to_email, subject, body):
    """Send approval mail using backend-only Gmail SMTP credentials."""
    sender = (os.getenv("GMAIL_SENDER") or "").strip()
    app_password = (os.getenv("GMAIL_APP_PASSWORD") or "").strip()
    if not sender or not app_password:
        return False, "Email is not configured on the server. Set GMAIL_SENDER and GMAIL_APP_PASSWORD."
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender, app_password)
            smtp.send_message(message)
        return True, "Approval email sent."
    except Exception:
        return False, "Membership was approved, but the email could not be sent. Check the backend email configuration and logs."


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
    review_message = str(data.get("review_message") or "").strip()
    approval_email_message = str(data.get("approval_email_message") or "").strip()
    if len(review_message) > 2000 or len(approval_email_message) > 10000:
        return jsonify({"error": "Review notes must be 2,000 characters or fewer and approval email text 10,000 characters or fewer."}), 400

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

    if decision == "approve":
        db.session.add(Notification(
            recipient_id=applicant.id,
            organization_id=admin.organization_id,
            title="Membership request approved",
            message="Your request to join the organization was approved. You can now sign in to CleanTrack.",
            notification_type="membership_decision",
            resource_type="MembershipRequest",
            resource_id=str(membership_request.id),
        ))

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

    email_sent = None
    email_message = None
    if decision == "approve":
        organization = db.session.get(Organization, admin.organization_id)
        org_name = organization.name if organization else "your organization"
        default_message = (
            "Hello {name},\n\n"
            "Good news! Your request to join {organization} on CleanTrack has been approved. "
            "Your account is now active, and you can sign in using {email}.\n\n"
            "Approved role: {role}\n"
            "Organization: {organization}\n\n"
            "Welcome aboard!\n"
            "{organization} Admin team"
        )
        template = approval_email_message or default_message
        values = {
            "name": applicant.full_name or "there",
            "organization": org_name,
            "role": membership_request.requested_role or applicant.role or "Member",
            "email": applicant.email or "",
        }
        try:
            body = template.format_map(values)
        except (KeyError, ValueError):
            body = template
        email_sent, email_message = _send_membership_approval_email(
            applicant.email,
            "Your CleanTrack membership request was approved",
            body,
        )

    response_message = (
        "Membership request approved."
        if decision == "approve"
        else "Membership request rejected."
    )
    if decision == "approve" and email_sent is False:
        response_message += " " + (email_message or "The approval email was not sent.")
    elif decision == "approve" and email_sent:
        response_message += " Approval email sent."

    return jsonify({
        "message": response_message,
        "email_sent": email_sent,
        "email_message": email_message,
        "membership_request": membership_request.to_dict(),
        "user": applicant.to_dict(),
    }), 200