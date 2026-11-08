from datetime import datetime, timezone, timedelta
import hashlib
import os
from flask import Blueprint, jsonify, request
from sqlalchemy import or_
from app.extensions import db
from app.models.personnel_chat import PersonnelConversation, PersonnelMessage
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.invitation import Invitation, generate_invitation_token
from app.models.organization import Organization
from app.models.audit_log import AuditLog
from app.models.operation import Operation
from app.services.permissions import get_current_user, roles_required
from app.routes.operations import _team_ids

supervisor_personnel_bp = Blueprint("supervisor_personnel", __name__, url_prefix="/api/supervisor/personnel")

def _scope():
    user = get_current_user()
    team_ids = _team_ids(user)
    if len(team_ids) != 1:
        return user, None, (jsonify({"error":"Your account must belong to exactly one active team."}), 409)
    team_id = next(iter(team_ids))
    team = Team.query.filter_by(id=team_id, organization_id=user.organization_id, status="active").first()
    if team is None:
        return user, None, (jsonify({"error":"Your assigned team is unavailable."}), 404)
    return user, team_id, None

def _staff_in_team(staff_id, user, team_id):
    return (User.query.join(TeamMember, TeamMember.user_id == User.id)
        .filter(User.id == staff_id, User.organization_id == user.organization_id,
                User.role == "Staff", User.account_status == "active",
                TeamMember.team_id == team_id).first())

def _conversation(user, staff_id, team_id):
    staff = _staff_in_team(staff_id, user, team_id)
    if staff is None:
        return None, None
    row = PersonnelConversation.query.filter_by(
        organization_id=user.organization_id, supervisor_id=user.id, staff_id=staff.id
    ).first()
    if row is None:
        row = PersonnelConversation(organization_id=user.organization_id, supervisor_id=user.id, staff_id=staff.id)
        db.session.add(row)
        db.session.flush()
    row.supervisor_deleted_at = None
    return row, staff

@supervisor_personnel_bp.get("")
@roles_required("Supervisor")
def directory():
    user, team_id, error = _scope()
    if error: return error
    rows = (User.query.join(TeamMember, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team_id, User.organization_id == user.organization_id,
                User.role == "Staff", User.account_status == "active")
        .order_by(User.full_name.asc()).all())
    operations = Operation.query.filter(
        Operation.organization_id == user.organization_id,
        or_(Operation.team_id == team_id, Operation.assigned_supervisor_id == user.id)
    ).all()
    workload = {}
    completed = {}
    overdue = {}
    now = datetime.now(timezone.utc)
    for op in operations:
        sid = op.assigned_to_id
        if sid is None: continue
        status = str(op.status or "").lower().replace(" ","_")
        if status not in {"completed","approved","verified","cancelled","canceled"}:
            workload[sid] = workload.get(sid,0)+1
            due=op.due_at
            if due and (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) < now:
                overdue[sid]=overdue.get(sid,0)+1
        if status in {"completed","approved","verified"}:
            completed[sid]=completed.get(sid,0)+1
    convs = {c.staff_id:c for c in PersonnelConversation.query.filter_by(organization_id=user.organization_id, supervisor_id=user.id).all()}
    members=[]
    for m in rows:
        c=convs.get(m.id)
        last=None
        if c:
            last_query=PersonnelMessage.query.filter_by(conversation_id=c.id)\n            if c.supervisor_cleared_at: last_query=last_query.filter(PersonnelMessage.created_at>c.supervisor_cleared_at)\n            last=last_query.order_by(PersonnelMessage.created_at.desc()).first()
        members.append({"id":m.id,"name":m.full_name,"email":m.email,"phone":m.phone,
            "avatar_url":m.avatar_url,"role":m.role,"account_status":m.account_status,
            "joined_at":m.created_at.isoformat() if m.created_at else None,
            "workload":workload.get(m.id,0),"completed_operations":completed.get(m.id,0),
            "overdue_operations":overdue.get(m.id,0),"availability":"not_recorded",
            "last_message":last.body if last else None,
            "last_message_at":last.created_at.isoformat() if last and last.created_at else None,
            "conversation_id":c.id if c else None})
    return jsonify({"team_id":team_id,"members":members}),200

@supervisor_personnel_bp.get("/<int:staff_id>/profile")
@roles_required("Supervisor")
def profile(staff_id):
    user, team_id, error=_scope()
    if error:return error
    staff=_staff_in_team(staff_id,user,team_id)
    if staff is None:return jsonify({"error":"Staff member not found in your team."}),404
    return jsonify({"profile":{"id":staff.id,"name":staff.full_name,"email":staff.email,"phone":staff.phone,
        "avatar_url":staff.avatar_url,"role":staff.role,"joined_at":staff.created_at.isoformat() if staff.created_at else None,
        "account_status":staff.account_status,"availability":"not_recorded",
        "skills":[],"qualifications":[],"certifications":[]}}),200

@supervisor_personnel_bp.get("/<int:staff_id>/messages")
@roles_required("Supervisor")
def messages(staff_id):
    user,team_id,error=_scope()
    if error:return error
    conv,staff=_conversation(user,staff_id,team_id)
    if conv is None:return jsonify({"error":"Staff member not found in your team."}),404
    db.session.commit()
    rows=PersonnelMessage.query.filter_by(conversation_id=conv.id).filter(
        PersonnelMessage.created_at > (conv.supervisor_cleared_at or datetime.min.replace(tzinfo=timezone.utc))
    ).order_by(PersonnelMessage.created_at.asc()).limit(500).all()
    return jsonify({"conversation_id":conv.id,"messages":[{"id":m.id,"sender_id":m.sender_id,"recipient_id":m.recipient_id,
        "body":m.body,"created_at":m.created_at.isoformat(),"mine":m.sender_id==user.id} for m in rows]}),200

@supervisor_personnel_bp.post("/<int:staff_id>/messages")
@roles_required("Supervisor")
def send_message(staff_id):
    user,team_id,error=_scope()
    if error:return error
    data=request.get_json(silent=True) or {}
    body=str(data.get("body") or "").strip()
    if not body or len(body)>5000:return jsonify({"error":"Message must contain 1–5,000 characters."}),400
    conv,staff=_conversation(user,staff_id,team_id)
    if conv is None:return jsonify({"error":"Staff member not found in your team."}),404
    row=PersonnelMessage(conversation_id=conv.id,organization_id=user.organization_id,sender_id=user.id,recipient_id=staff.id,body=body)
    conv.updated_at=datetime.now(timezone.utc)
    db.session.add(row)
    try:db.session.commit()
    except Exception:
        db.session.rollback();return jsonify({"error":"Message could not be saved."}),500
    return jsonify({"message":{"id":row.id,"sender_id":row.sender_id,"recipient_id":row.recipient_id,"body":row.body,"created_at":row.created_at.isoformat(),"mine":True}}),201

@supervisor_personnel_bp.post("/<int:staff_id>/clear")
@roles_required("Supervisor")
def clear_chat(staff_id):
    user,team_id,error=_scope()
    if error:return error
    conv,staff=_conversation(user,staff_id,team_id)
    if conv is None:return jsonify({"error":"Staff member not found in your team."}),404
    conv.supervisor_cleared_at=datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"message":"Chat cleared from your view. Messages remain retained in the system record."}),200

@supervisor_personnel_bp.delete("/<int:staff_id>/chat")
@roles_required("Supervisor")
def delete_chat(staff_id):
    user,team_id,error=_scope()
    if error:return error
    conv,staff=_conversation(user,staff_id,team_id)
    if conv is None:return jsonify({"error":"Staff member not found in your team."}),404
    conv.supervisor_deleted_at=datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"message":"Chat removed from your list. The other participant's history and retained messages are unchanged."}),200


@supervisor_personnel_bp.post("/invites")
@roles_required("Supervisor")
def invite_staff():
    user, team_id, error = _scope()
    if error: return error
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    if not email or len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return jsonify({"error":"Enter a valid email address."}), 400
    if User.query.filter(db.func.lower(User.email) == email).first():
        return jsonify({"error":"This email already has a MECHNAR account. Use the membership workflow instead."}), 409
    organization = db.session.get(Organization, user.organization_id)
    if organization is None or organization.status != "active":
        return jsonify({"error":"Your organization is not accepting invitations."}), 409
    existing = Invitation.query.filter_by(organization_id=user.organization_id, email=email, role="Staff", status="pending").order_by(Invitation.created_at.desc()).first()
    if existing:
        expiry = existing.expires_at
        if expiry and (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc):
            return jsonify({"error":"A pending invitation already exists for this email."}), 409
    token = generate_invitation_token()
    invitation = Invitation(organization_id=user.organization_id, invited_by_id=user.id, email=email, role="Staff",
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(), status="pending",
        expires_at=datetime.now(timezone.utc)+timedelta(days=14))
    db.session.add(invitation)
    db.session.flush()
    db.session.add(AuditLog(actor_id=user.id, organization_id=user.organization_id,
        action="organization.staff_invitation_created", resource_type="Invitation", resource_id=str(invitation.id),
        details={"email":email,"role":"Staff","team_id":team_id}))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error":"Invitation could not be saved."}), 500
    base=os.getenv("FRONTEND_URL","https://cleantrack-main.onrender.com").strip().rstrip("/")
    return jsonify({"message":"Staff invitation created. The applicant must register, then wait for Admin approval.",
        "invite_url":base+"/?invite="+token,"expires_at":invitation.expires_at.isoformat()}),201


@supervisor_personnel_bp.get("/staff/conversations")
@roles_required("Staff")
def staff_conversations():
    user = get_current_user()
    rows = PersonnelConversation.query.filter_by(organization_id=user.organization_id, staff_id=user.id).all()
    result=[]
    for conv in rows:
        if conv.staff_deleted_at is not None:
            continue
        supervisor=User.query.filter_by(id=conv.supervisor_id, organization_id=user.organization_id, role="Supervisor").first()
        if supervisor is None: continue
        last=PersonnelMessage.query.filter_by(conversation_id=conv.id).order_by(PersonnelMessage.created_at.desc()).first()
        result.append({"supervisor_id":supervisor.id,"name":supervisor.full_name,"email":supervisor.email,
            "avatar_url":supervisor.avatar_url,"last_message":last.body if last else None,
            "last_message_at":last.created_at.isoformat() if last and last.created_at else None})
    result.sort(key=lambda x:x.get("last_message_at") or "",reverse=True)
    return jsonify({"conversations":result}),200

@supervisor_personnel_bp.get("/staff/<int:supervisor_id>/messages")
@roles_required("Staff")
def staff_messages(supervisor_id):
    user=get_current_user()
    supervisor=User.query.filter_by(id=supervisor_id,organization_id=user.organization_id,role="Supervisor",account_status="active").first()
    if supervisor is None:return jsonify({"error":"Supervisor not found."}),404
    conv=PersonnelConversation.query.filter_by(organization_id=user.organization_id,supervisor_id=supervisor.id,staff_id=user.id).first()
    if conv is None:return jsonify({"conversation_id":None,"messages":[]}),200
    conv.staff_deleted_at=None
    db.session.commit()
    cutoff=conv.staff_cleared_at or datetime.min.replace(tzinfo=timezone.utc)
    rows=PersonnelMessage.query.filter_by(conversation_id=conv.id).filter(PersonnelMessage.created_at>cutoff).order_by(PersonnelMessage.created_at.asc()).limit(500).all()
    return jsonify({"conversation_id":conv.id,"messages":[{"id":m.id,"sender_id":m.sender_id,"recipient_id":m.recipient_id,"body":m.body,"created_at":m.created_at.isoformat(),"mine":m.sender_id==user.id} for m in rows]}),200

@supervisor_personnel_bp.post("/staff/<int:supervisor_id>/messages")
@roles_required("Staff")
def staff_send_message(supervisor_id):
    user=get_current_user()
    supervisor=User.query.filter_by(id=supervisor_id,organization_id=user.organization_id,role="Supervisor",account_status="active").first()
    if supervisor is None:return jsonify({"error":"Supervisor not found."}),404
    memberships=TeamMember.query.filter_by(user_id=user.id).all()
    team_ids={m.team_id for m in memberships}
    if not team_ids or not TeamMember.query.filter(TeamMember.user_id==supervisor.id,TeamMember.team_id.in_(team_ids)).first():
        return jsonify({"error":"This Supervisor is not assigned to your team."}),403
    conv=PersonnelConversation.query.filter_by(organization_id=user.organization_id,supervisor_id=supervisor.id,staff_id=user.id).first()
    if conv is None:
        conv=PersonnelConversation(organization_id=user.organization_id,supervisor_id=supervisor.id,staff_id=user.id)
        db.session.add(conv);db.session.flush()
    conv.staff_deleted_at=None
    body=str((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not body or len(body)>5000:return jsonify({"error":"Message must contain 1–5,000 characters."}),400
    row=PersonnelMessage(conversation_id=conv.id,organization_id=user.organization_id,sender_id=user.id,recipient_id=supervisor.id,body=body)
    conv.updated_at=datetime.now(timezone.utc)
    db.session.add(row)
    try:db.session.commit()
    except Exception:
        db.session.rollback();return jsonify({"error":"Message could not be saved."}),500
    return jsonify({"message":{"id":row.id,"sender_id":row.sender_id,"recipient_id":row.recipient_id,"body":row.body,"created_at":row.created_at.isoformat(),"mine":True}}),201

@supervisor_personnel_bp.post("/staff/<int:supervisor_id>/clear")
@roles_required("Staff")
def staff_clear_chat(supervisor_id):
    user=get_current_user()
    conv=PersonnelConversation.query.filter_by(organization_id=user.organization_id,supervisor_id=supervisor_id,staff_id=user.id).first()
    if conv is None:return jsonify({"message":"There is no conversation to clear."}),200
    conv.staff_cleared_at=datetime.now(timezone.utc);db.session.commit()
    return jsonify({"message":"Chat cleared from your view. Messages remain retained in the system record."}),200

@supervisor_personnel_bp.delete("/staff/<int:supervisor_id>/chat")
@roles_required("Staff")
def staff_delete_chat(supervisor_id):
    user=get_current_user()
    conv=PersonnelConversation.query.filter_by(organization_id=user.organization_id,supervisor_id=supervisor_id,staff_id=user.id).first()
    if conv is None:return jsonify({"message":"There is no conversation to remove."}),200
    conv.staff_deleted_at=datetime.now(timezone.utc);conv.staff_cleared_at=conv.staff_deleted_at;db.session.commit()
    return jsonify({"message":"Conversation removed from your chat list. The other participant's history is unchanged."}),200
