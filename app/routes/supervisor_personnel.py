from datetime import datetime, timezone, timedelta
import hashlib
import os
from flask import Blueprint, jsonify, request
from sqlalchemy import or_
from app.extensions import db
from app.models.personnel_chat import PersonnelConversation, PersonnelMessage
from app.models.staff_onboarding import StaffOnboarding
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
            last_query=PersonnelMessage.query.filter_by(conversation_id=c.id)
            if c.supervisor_cleared_at: last_query=last_query.filter(PersonnelMessage.created_at>c.supervisor_cleared_at)
            last=last_query.order_by(PersonnelMessage.created_at.desc()).first()
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
    return jsonify({"conversation_id":conv.id,"messages":[_message_dict(m, user.id, staff_id=staff.id) for m in rows]}),200

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
    return jsonify({"conversation_id":conv.id,"messages":[_message_dict(m, user.id, supervisor_id=supervisor.id) for m in rows]}),200

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


# Personnel onboarding is scoped to the Supervisor's one active team.
_ONBOARDING_STEPS = [
    {"id":"profile","label":"Confirm personal and contact information"},
    {"id":"documents","label":"Collect and verify required documents"},
    {"id":"role","label":"Explain role, responsibilities and reporting line"},
    {"id":"policies","label":"Review organisation policies and safety procedures"},
    {"id":"training","label":"Complete role-specific orientation and training"},
    {"id":"access","label":"Confirm required system and site access"},
    {"id":"acknowledgement","label":"Record staff acknowledgement and questions"},
]

@supervisor_personnel_bp.get("/onboarding")
@roles_required("Supervisor")
def onboarding_directory():
    user, team_id, error = _scope()
    if error: return error
    staff_rows = (User.query.join(TeamMember, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team_id, User.organization_id == user.organization_id,
                User.role == "Staff", User.account_status == "active")
        .order_by(User.full_name.asc()).all())
    staff_ids = [member.id for member in staff_rows]
    records = {row.staff_id: row for row in StaffOnboarding.query.filter(
        StaffOnboarding.organization_id == user.organization_id,
        StaffOnboarding.staff_id.in_(staff_ids)).all()} if staff_ids else {}
    result = []
    for member in staff_rows:
        record = records.get(member.id)
        steps = record.steps if record else []
        completed = sum(1 for step in steps if step.get("done"))
        result.append({
            "staff_id": member.id, "name": member.full_name, "email": member.email,
            "avatar_url": member.avatar_url, "status": record.status if record else "not_started",
            "started_at": record.started_at.isoformat() if record and record.started_at else None,
            "completed_at": record.completed_at.isoformat() if record and record.completed_at else None,
            "completed_steps": completed, "total_steps": len(steps) if record else len(_ONBOARDING_STEPS),
            "progress": round(completed / max(len(steps), 1) * 100) if record else 0,
            "steps": steps, "notes": record.notes if record else ""
        })
    return jsonify({"onboarding": result, "template": _ONBOARDING_STEPS}), 200

@supervisor_personnel_bp.post("/<int:staff_id>/onboarding")
@roles_required("Supervisor")
def start_staff_onboarding(staff_id):
    user, team_id, error = _scope()
    if error: return error
    staff = _staff_in_team(staff_id, user, team_id)
    if staff is None: return jsonify({"error":"Staff member not found in your team."}), 404
    record = StaffOnboarding.query.filter_by(organization_id=user.organization_id, staff_id=staff.id).first()
    if record is None:
        record = StaffOnboarding(organization_id=user.organization_id, team_id=team_id,
            staff_id=staff.id, started_by_id=user.id,
            steps=[dict(step, done=False, completed_at=None) for step in _ONBOARDING_STEPS])
        db.session.add(record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error":"Onboarding could not be started."}), 500
    return jsonify({"message":"Onboarding started.", "staff_id":staff.id}), 201

@supervisor_personnel_bp.patch("/<int:staff_id>/onboarding")
@roles_required("Supervisor")
def update_staff_onboarding(staff_id):
    user, team_id, error = _scope()
    if error: return error
    staff = _staff_in_team(staff_id, user, team_id)
    if staff is None: return jsonify({"error":"Staff member not found in your team."}), 404
    record = StaffOnboarding.query.filter_by(organization_id=user.organization_id,
        staff_id=staff.id).first()
    if record is None: return jsonify({"error":"Start onboarding before updating its checklist."}), 409
    record.team_id = team_id
    data = request.get_json(silent=True) or {}
    submitted = data.get("steps")
    if not isinstance(submitted, list) or len(submitted) != len(_ONBOARDING_STEPS):
        return jsonify({"error":"Submit the complete onboarding checklist."}), 400
    allowed = {step["id"]: step["label"] for step in _ONBOARDING_STEPS}
    now = datetime.now(timezone.utc).isoformat()
    normalized = []
    for item in submitted:
        if not isinstance(item, dict) or item.get("id") not in allowed:
            return jsonify({"error":"Invalid onboarding checklist item."}), 400
        done = bool(item.get("done"))
        normalized.append({"id":item["id"], "label":allowed[item["id"]], "done":done,
            "completed_at": (item.get("completed_at") or now) if done else None})
    record.steps = normalized
    record.notes = str(data.get("notes") or "").strip()[:4000]
    complete = all(item["done"] for item in normalized)
    record.status = "completed" if complete else "in_progress"
    record.completed_at = datetime.now(timezone.utc) if complete else None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error":"Onboarding progress could not be saved."}), 500
    return jsonify({"message":"Onboarding progress saved.", "status":record.status}), 200

# File attachments are stored in the database, not the ephemeral application filesystem.
_ATTACHMENT_LIMIT = 20 * 1024 * 1024
_ATTACHMENT_TYPES = {
    "file": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".rtf", ".zip", ".7z", ".json", ".xml", ".md", ".log"},
    "media": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm"},
    "voice": {".webm", ".ogg", ".mp3", ".m4a", ".wav"}
}
_ATTACHMENT_MIMES = {
    ".pdf":"application/pdf", ".doc":"application/msword",
    ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls":"application/vnd.ms-excel",
    ".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt":"application/vnd.ms-powerpoint",
    ".pptx":"application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt":"text/plain", ".csv":"text/csv", ".rtf":"application/rtf",
    ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png",
    ".gif":"image/gif", ".webp":"image/webp", ".mp4":"video/mp4",
    ".mov":"video/quicktime", ".webm":"video/webm", ".zip":"application/zip", ".7z":"application/x-7z-compressed", ".json":"application/json", ".xml":"application/xml", ".md":"text/markdown", ".log":"text/plain", ".ogg":"audio/ogg", ".mp3":"audio/mpeg", ".m4a":"audio/mp4", ".wav":"audio/wav"
}

def _message_dict(message, current_user_id, staff_id=None, supervisor_id=None):
    attachment = None
    if message.attachment_filename:
        attachment = {"filename":message.attachment_filename, "content_type":message.attachment_content_type,
            "size":message.attachment_size, "kind":message.attachment_kind,
            "url": (f"supervisor/personnel/{staff_id}/attachments/{message.id}" if staff_id
                    else f"supervisor/personnel/staff/{supervisor_id}/attachments/{message.id}")}
    return {"id":message.id, "sender_id":message.sender_id, "recipient_id":message.recipient_id,
        "body":message.body, "created_at":message.created_at.isoformat(), "mine":message.sender_id==current_user_id,
        "attachment":attachment}

def _save_attachment_message(conversation, sender, recipient, upload, caption=""):
    from werkzeug.utils import secure_filename
    import os as _os
    filename = secure_filename(upload.filename or "")[:255]
    extension = _os.path.splitext(filename.lower())[1]
    kind = str(request.form.get("kind") or "").lower()
    if kind not in _ATTACHMENT_TYPES or extension not in _ATTACHMENT_TYPES[kind]:
        return None, "This file type is not allowed for the selected attachment category."
    data = upload.stream.read(_ATTACHMENT_LIMIT + 1)
    if not data or len(data) > _ATTACHMENT_LIMIT:
        return None, "Attachments must be between 1 byte and 20 MB."
    mime = _ATTACHMENT_MIMES.get(extension, upload.mimetype or "application/octet-stream")
    stored_kind = kind
    if kind == "media":
        if extension in {".jpg", ".jpeg", ".png", ".gif", ".webp"} and mime.startswith("image/"):
            stored_kind = "photo"
        elif extension in {".mp4", ".mov", ".webm"} and mime.startswith("video/"):
            stored_kind = "video"
        else:
            return None, "Select a supported photo or video."
    elif kind == "voice":
        if not mime.startswith("audio/"): return None, "Select a valid audio recording."
        stored_kind = "voice"
    row = PersonnelMessage(conversation_id=conversation.id, organization_id=sender.organization_id,
        sender_id=sender.id, recipient_id=recipient.id, body=str(caption or "").strip()[:1000],
        attachment_filename=filename, attachment_content_type=mime, attachment_size=len(data),
        attachment_kind=stored_kind, attachment_data=data)
    conversation.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    try: db.session.commit()
    except Exception:
        db.session.rollback()
        return None, "Attachment could not be saved."
    return row, None

@supervisor_personnel_bp.post("/<int:staff_id>/attachments")
@roles_required("Supervisor")
def supervisor_send_attachment(staff_id):
    user, team_id, error = _scope()
    if error: return error
    conversation, staff = _conversation(user, staff_id, team_id)
    if conversation is None: return jsonify({"error":"Staff member not found in your team."}), 404
    upload = request.files.get("file")
    if upload is None: return jsonify({"error":"Choose a file to send."}), 400
    row, error = _save_attachment_message(conversation, user, staff, upload, request.form.get("caption"))
    if error: return jsonify({"error":error}), 400
    return jsonify({"message":_message_dict(row, user.id, staff_id=staff.id)}), 201

@supervisor_personnel_bp.post("/staff/<int:supervisor_id>/attachments")
@roles_required("Staff")
def staff_send_attachment(supervisor_id):
    user = get_current_user()
    supervisor = User.query.filter_by(id=supervisor_id, organization_id=user.organization_id,
        role="Supervisor", account_status="active").first()
    if supervisor is None: return jsonify({"error":"Supervisor not found."}), 404
    memberships = TeamMember.query.filter_by(user_id=user.id).all()
    team_ids = {item.team_id for item in memberships}
    if not team_ids or not TeamMember.query.filter(TeamMember.user_id==supervisor.id,
            TeamMember.team_id.in_(team_ids)).first():
        return jsonify({"error":"This Supervisor is not assigned to your team."}), 403
    conversation = PersonnelConversation.query.filter_by(organization_id=user.organization_id,
        supervisor_id=supervisor.id, staff_id=user.id).first()
    if conversation is None:
        conversation = PersonnelConversation(organization_id=user.organization_id,
            supervisor_id=supervisor.id, staff_id=user.id)
        db.session.add(conversation)
        db.session.flush()
    conversation.staff_deleted_at = None
    upload = request.files.get("file")
    if upload is None: return jsonify({"error":"Choose a file to send."}), 400
    row, error = _save_attachment_message(conversation, user, supervisor, upload, request.form.get("caption"))
    if error: return jsonify({"error":error}), 400
    return jsonify({"message":_message_dict(row, user.id, supervisor_id=supervisor.id)}), 201

@supervisor_personnel_bp.get("/<int:staff_id>/attachments/<int:message_id>")
@roles_required("Supervisor")
def supervisor_download_attachment(staff_id, message_id):
    from io import BytesIO
    from flask import send_file
    user, team_id, error = _scope()
    if error: return error
    if _staff_in_team(staff_id, user, team_id) is None: return jsonify({"error":"Staff member not found in your team."}), 404
    message = PersonnelMessage.query.join(PersonnelConversation,
        PersonnelConversation.id == PersonnelMessage.conversation_id).filter(
        PersonnelMessage.id == message_id, PersonnelConversation.organization_id == user.organization_id,
        PersonnelConversation.supervisor_id == user.id, PersonnelConversation.staff_id == staff_id,
        PersonnelMessage.attachment_data.isnot(None)).first()
    if message is None: return jsonify({"error":"Attachment not found."}), 404
    return send_file(BytesIO(message.attachment_data), mimetype=message.attachment_content_type,
        as_attachment=True, download_name=message.attachment_filename, max_age=0)

@supervisor_personnel_bp.get("/staff/<int:supervisor_id>/attachments/<int:message_id>")
@roles_required("Staff")
def staff_download_attachment(supervisor_id, message_id):
    from io import BytesIO
    from flask import send_file
    user = get_current_user()
    message = PersonnelMessage.query.join(PersonnelConversation,
        PersonnelConversation.id == PersonnelMessage.conversation_id).filter(
        PersonnelMessage.id == message_id, PersonnelConversation.organization_id == user.organization_id,
        PersonnelConversation.supervisor_id == supervisor_id, PersonnelConversation.staff_id == user.id,
        PersonnelMessage.attachment_data.isnot(None)).first()
    if message is None: return jsonify({"error":"Attachment not found."}), 404
    return send_file(BytesIO(message.attachment_data), mimetype=message.attachment_content_type,
        as_attachment=True, download_name=message.attachment_filename, max_age=0)

