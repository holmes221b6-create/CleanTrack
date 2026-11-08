from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from sqlalchemy import or_
from app.extensions import db
from app.models.personnel_chat import PersonnelConversation, PersonnelMessage
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
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
            last=PersonnelMessage.query.filter_by(conversation_id=c.id).order_by(PersonnelMessage.created_at.desc()).first()
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
