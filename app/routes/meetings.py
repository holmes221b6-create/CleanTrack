import base64, hashlib, hmac, json, os, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.meeting import Meeting
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.notification import Notification
from app.services.permissions import get_current_user, roles_required

meetings_bp = Blueprint("meetings", __name__, url_prefix="/api/meetings")

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _zoom_config():
    keys = ("ZOOM_ACCOUNT_ID", "ZOOM_SERVER_CLIENT_ID", "ZOOM_SERVER_CLIENT_SECRET",
            "ZOOM_MEETING_SDK_CLIENT_ID", "ZOOM_MEETING_SDK_CLIENT_SECRET")
    return {key: os.environ.get(key, "").strip() for key in keys}

def _zoom_token(cfg):
    body = urlencode({"grant_type": "account_credentials", "account_id": cfg["ZOOM_ACCOUNT_ID"]}).encode()
    auth = base64.b64encode((cfg["ZOOM_SERVER_CLIENT_ID"] + ":" + cfg["ZOOM_SERVER_CLIENT_SECRET"]).encode()).decode()
    req = Request("https://zoom.us/oauth/token", data=body, headers={"Authorization":"Basic "+auth,
        "Content-Type":"application/x-www-form-urlencoded"}, method="POST")
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())["access_token"]

def _zoom_api(method, path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request("https://api.zoom.us/v2"+path, data=data,
        headers={"Authorization":"Bearer "+token, "Content-Type":"application/json"}, method=method)
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode() or "{}")

def _sdk_signature(meeting_number, role):
    cfg = _zoom_config()
    now = int(time.time())
    header = _b64(json.dumps({"alg":"HS256","typ":"JWT"}, separators=(",",":")).encode())
    payload = _b64(json.dumps({"appKey":cfg["ZOOM_MEETING_SDK_CLIENT_ID"],"mn":str(meeting_number),
        "role":int(role),"iat":now-30,"exp":now+60*60,"tokenExp":now+60*60},
        separators=(",",":")).encode())
    message = (header+"."+payload).encode()
    signature = _b64(hmac.new(cfg["ZOOM_MEETING_SDK_CLIENT_SECRET"].encode(), message, hashlib.sha256).digest())
    return header+"."+payload+"."+signature

def _team_for_user(team_id, user):
    return (Team.query.join(TeamMember, TeamMember.team_id == Team.id)
        .filter(Team.id == team_id, Team.organization_id == user.organization_id,
                Team.status == "active", TeamMember.user_id == user.id).first())

def _serialize(row, user):
    return {"id":row.id,"team_id":row.team_id,"team_name":row.team.name if row.team else ("All Supervisors" if row.audience_role == "Supervisor" else "Team"),"audience_role":row.audience_role,
        "topic":row.topic,"agenda":row.agenda,"start_at":row.start_at.isoformat(),
        "duration_minutes":row.duration_minutes,"status":row.status,
        "created_by":row.creator.full_name if row.creator else "",
        "is_host":row.created_by_id == user.id,"host_start_url":row.start_url if row.created_by_id == user.id and row.status == "scheduled" else None,"join_url":row.join_url if row.status != "cancelled" else None,
        "created_at":row.created_at.isoformat() if row.created_at else None}

@meetings_bp.get("")
@roles_required("Admin","Supervisor","Staff")
def list_meetings():
    user = get_current_user()
    q = Meeting.query.filter_by(organization_id=user.organization_id)
    if user.role == "Admin":
        pass
    elif user.role == "Supervisor":
        team_ids = db.session.query(TeamMember.team_id).filter_by(user_id=user.id).subquery()
        q = q.filter(db.or_(Meeting.team_id.in_(team_ids), Meeting.audience_role == "Supervisor"))
    else:
        team_ids = db.session.query(TeamMember.team_id).filter_by(user_id=user.id).subquery()
        q = q.filter(Meeting.team_id.in_(team_ids), Meeting.audience_role == "Staff")
    rows = q.order_by(Meeting.start_at.desc()).limit(100).all()
    return jsonify({"meetings":[_serialize(row,user) for row in rows]})

@meetings_bp.post("")
@roles_required("Admin","Supervisor")
def create_meeting():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic") or "").strip()
    agenda = str(data.get("agenda") or "").strip()
    try:
        duration = int(data.get("duration_minutes", 40))
        team_id = int(data.get("team_id")) if user.role == "Supervisor" else None
        start_at = datetime.fromisoformat(str(data.get("start_at")).replace("Z","+00:00"))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        start_at = start_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return jsonify({"error":"Provide a valid team, start time and duration."}), 400
    if not topic or len(topic)>200 or len(agenda)>5000 or duration < 5 or duration > 180:
        return jsonify({"error":"Topic, agenda or duration is invalid. Duration must be 5–180 minutes."}), 400
    team = _team_for_user(team_id,user) if user.role == "Supervisor" else None
    if user.role == "Supervisor" and not team:
        return jsonify({"error":"You can only schedule meetings for a team you belong to."}), 403
    cfg = _zoom_config()
    if not all(cfg.values()):
        return jsonify({"error":"Zoom is not configured on the server. Add all five Zoom credentials to Render environment variables first."}), 503
    try:
        token = _zoom_token(cfg)
        zoom = _zoom_api("POST","/users/me/meetings",token,{"topic":topic,"type":2,
            "start_time":start_at.strftime("%Y-%m-%dT%H:%M:%SZ"),"duration":duration,
            "timezone":"UTC","agenda":agenda,"settings":{"join_before_host":False,"waiting_room":True}})
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
        detail = "Zoom could not create the meeting. Check the Server-to-Server app scopes and credentials."
        if isinstance(exc, HTTPError):
            try: detail = json.loads(exc.read().decode()).get("message",detail)
            except Exception: pass
        return jsonify({"error":detail}), 502
    if not zoom.get("id") or not zoom.get("join_url"):
        return jsonify({"error":"Zoom returned an incomplete meeting response."}), 502
    row = Meeting(organization_id=user.organization_id,team_id=team.id if team else None,
        audience_role="Staff" if user.role == "Supervisor" else "Supervisor",created_by_id=user.id,
        zoom_meeting_id=str(zoom["id"]),topic=topic,agenda=agenda,start_at=start_at,
        duration_minutes=duration,status="scheduled",join_url=zoom["join_url"],
        start_url=zoom.get("start_url"),password=zoom.get("password"))
    db.session.add(row)
    db.session.flush()
    if user.role == "Admin":
        members = User.query.filter_by(organization_id=user.organization_id,
            account_status="active",role="Supervisor").all()
    else:
        members = (User.query.join(TeamMember, TeamMember.user_id == User.id)
            .filter(TeamMember.team_id==team.id,User.organization_id==user.organization_id,
                    User.account_status=="active",User.role=="Staff",User.id != user.id).all())
    for member in members:
        db.session.add(Notification(recipient_id=member.id,organization_id=user.organization_id,
            title="Team meeting scheduled",message=f"{topic} — {start_at.strftime('%d %b %Y, %H:%M UTC')}",
            notification_type="meeting",resource_type="Meeting",resource_id=str(row.id)))
    db.session.commit()
    return jsonify({"meeting":_serialize(row,user),"host_start_url":row.start_url}),201

@meetings_bp.post("/<int:meeting_id>/signature")
@roles_required("Admin","Supervisor","Staff")
def meeting_signature(meeting_id):
    user = get_current_user()
    row = Meeting.query.filter_by(id=meeting_id,organization_id=user.organization_id).first()
    if not row or row.status != "scheduled":
        return jsonify({"error":"Meeting not found or no longer available."}),404
    if user.role == "Staff" and (row.audience_role != "Staff" or not _team_for_user(row.team_id,user)):
        return jsonify({"error":"You are not authorised to join this meeting."}),403
    if user.role == "Supervisor" and row.audience_role == "Staff" and not _team_for_user(row.team_id,user):
        return jsonify({"error":"You are not authorised to join this meeting."}),403
    if user.role == "Supervisor" and row.audience_role == "Supervisor" and row.created_by_id == user.id:
        return jsonify({"error":"The host should launch the meeting using the Zoom host link."}),403
    cfg = _zoom_config()
    if not cfg["ZOOM_MEETING_SDK_CLIENT_ID"] or not cfg["ZOOM_MEETING_SDK_CLIENT_SECRET"]:
        return jsonify({"error":"Meeting SDK credentials are not configured on the server."}),503
    # Host launches the Zoom host URL; embedded SDK is for participants.
    sdk_role = 0
    result = {"signature":_sdk_signature(row.zoom_meeting_id,sdk_role),
        "client_id":cfg["ZOOM_MEETING_SDK_CLIENT_ID"],"meeting_number":row.zoom_meeting_id,
        "password":row.password or "","user_name":user.full_name,"user_email":user.email,
        "role":sdk_role}
    return jsonify(result),200

@meetings_bp.post("/<int:meeting_id>/cancel")
@roles_required("Admin","Supervisor")
def cancel_meeting(meeting_id):
    user = get_current_user()
    row = Meeting.query.filter_by(id=meeting_id,organization_id=user.organization_id).first()
    if not row:
        return jsonify({"error":"Meeting not found."}),404
    if user.role == "Supervisor" and (row.audience_role != "Staff" or row.created_by_id != user.id or not _team_for_user(row.team_id,user)):
        return jsonify({"error":"Only the hosting Supervisor or an Admin can cancel this meeting."}),403
    if row.status != "scheduled":
        return jsonify({"error":"Only scheduled meetings can be cancelled."}),409
    cfg = _zoom_config()
    try:
        token = _zoom_token(cfg)
        _zoom_api("DELETE",f"/meetings/{row.zoom_meeting_id}",token)
    except Exception:
        return jsonify({"error":"Zoom could not cancel this meeting. No local changes were made."}),502
    row.status="cancelled"
    if row.audience_role == "Supervisor":
        members = User.query.filter_by(organization_id=user.organization_id,
            account_status="active",role="Supervisor").all()
    else:
        members = (User.query.join(TeamMember, TeamMember.user_id == User.id)
            .filter(TeamMember.team_id == row.team_id,
                    User.organization_id == user.organization_id,
                    User.account_status == "active",
                    User.id != user.id).all())
    for member in members:
        db.session.add(Notification(
            recipient_id=member.id,
            organization_id=user.organization_id,
            title="Team meeting cancelled",
            message=f'"{row.topic}" scheduled for {row.start_at.strftime("%d %b %Y, %H:%M UTC")} has been cancelled.',
            notification_type="meeting_cancelled",
            resource_type="Meeting",
            resource_id=str(row.id),
        ))
    db.session.commit()
    return jsonify({"message":"Meeting cancelled and team notifications sent."})
