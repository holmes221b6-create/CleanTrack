from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Blueprint, jsonify, request
from sqlalchemy import func
from app.extensions import db
from app.models.quality_inspection import TeamLeaderAssignment, InspectionTemplate, InspectionSchedule, InspectionRecord
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.notification import Notification
from app.models.audit_log import AuditLog
from app.models.operational_records import QualityInspection
from app.routes.supervisor_records import _refs as _supervisor_refs
from app.services.permissions import get_current_user, roles_required

quality_bp = Blueprint("quality", __name__, url_prefix="/api/quality")

def _team_scope(user):
    rows = (db.session.query(Team.id).join(TeamMember, TeamMember.team_id == Team.id)
        .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id, Team.status == "active").all())
    ids = {r[0] for r in rows}
    if len(ids) != 1:
        return None, (jsonify({"error":"Your account must belong to exactly one active team."}), 409)
    team = Team.query.filter_by(id=next(iter(ids)), organization_id=user.organization_id, status="active").first()
    return team, None if team else (jsonify({"error":"Your active team was not found."}), 404)

def _leader(team_id, user_id, organization_id):
    return TeamLeaderAssignment.query.filter_by(team_id=team_id, user_id=user_id, organization_id=organization_id).first()

def _notify(user, title, message, record=None):
    db.session.add(Notification(recipient_id=user.id, organization_id=user.organization_id, title=title[:160],
        message=message[:1000], notification_type="inspection", resource_type="Inspection",
        resource_id=str(record.id) if record else None))

def _dt(value, timezone_name="Asia/Kolkata"):
    if not value: raise ValueError("A scheduled date and time is required.")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            try: parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError: raise ValueError("Choose a valid time zone.")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError("Enter a valid scheduled date and time.")

def _refs(user, team, data):
    operation, location, zone = _supervisor_refs(user, team, data)
    return (location.id if location else None, zone.id if zone else None,
            operation.id if operation else None)

def _checklist(raw):
    if not isinstance(raw, list) or not raw or len(raw) > 60:
        raise ValueError("Provide between 1 and 60 checklist items.")
    result = []
    for item in raw:
        label = item.get("label") if isinstance(item, dict) else item
        if not isinstance(label, str) or not label.strip(): raise ValueError("Every checklist item needs a label.")
        result.append({"label": label.strip()[:240], "checked": False})
    return result

def _next_due(schedule, previous):
    tz = ZoneInfo(schedule.timezone_name or "Asia/Kolkata")
    local = previous.astimezone(tz)
    if schedule.recurrence == "daily": nxt = local + timedelta(days=1)
    elif schedule.recurrence == "weekly":
        days = sorted(set(int(x) for x in (schedule.recurrence_days or [])))
        days = days or [local.weekday()]
        nxt = local + timedelta(days=1)
        for _ in range(7):
            if nxt.weekday() in days: break
            nxt += timedelta(days=1)
    elif schedule.recurrence == "monthly":
        import calendar
        year, month = local.year, local.month + 1
        if month == 13: year, month = year + 1, 1
        day = min(int(schedule.recurrence_day or local.day), calendar.monthrange(year, month)[1])
        nxt = local.replace(year=year, month=month, day=day)
    else: return None
    return nxt.astimezone(timezone.utc)

def _materialize(schedule, now=None):
    now = now or datetime.now(timezone.utc)
    latest = InspectionRecord.query.filter_by(schedule_id=schedule.id).order_by(InspectionRecord.scheduled_at.desc()).first()
    due = _dt(schedule.first_due_at.isoformat(), schedule.timezone_name) if latest is None else _next_due(schedule, latest.scheduled_at)
    made = 0
    # Generate due occurrences through today and one upcoming occurrence. Unique key protects concurrent requests.
    while due and due <= now + timedelta(days=1) and made < 120:
        row = InspectionRecord(organization_id=schedule.organization_id, team_id=schedule.team_id,
            schedule_id=schedule.id, template_id=schedule.template_id, created_by_id=schedule.created_by_id,
            assigned_to_id=schedule.assigned_to_id, operation_id=schedule.operation_id,
            location_id=schedule.location_id, zone_id=schedule.zone_id, title=schedule.title,
            checklist=[dict(x, checked=False) for x in (schedule.checklist_snapshot or [])],
            scheduled_at=due, status="scheduled")
        db.session.add(row)
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
            break
        if schedule.assigned_to_id != schedule.created_by_id and schedule.assignee:
            _notify(schedule.assignee, "Recurring inspection assigned", f"{schedule.title} is due for inspection.", row)
        made += 1
        due = _next_due(schedule, due)
        if schedule.recurrence == "once": break
    return made

def _materialize_team(team_id, organization_id):
    schedules = InspectionSchedule.query.filter_by(team_id=team_id, organization_id=organization_id, active=True).all()
    for schedule in schedules: _materialize(schedule)
    if schedules: db.session.commit()

def _import_legacy_inspections(user, team=None):
    query=QualityInspection.query.filter_by(organization_id=user.organization_id)
    if team is not None: query=query.filter_by(team_id=team.id)
    legacy_rows=query.order_by(QualityInspection.created_at.asc()).limit(3000).all()
    existing={row[0] for row in db.session.query(InspectionRecord.legacy_source_id).filter(
        InspectionRecord.legacy_source_id.isnot(None),InspectionRecord.organization_id==user.organization_id).all()}
    changed=False
    for old in legacy_rows:
        if old.id in existing: continue
        checklist=old.checklist or []
        passed=sum(1 for item in checklist if isinstance(item,dict) and item.get("checked"))
        score=round(passed/len(checklist)*100,1) if checklist else None
        done=old.status=="completed"
        row=InspectionRecord(organization_id=old.organization_id,team_id=old.team_id,
            legacy_source_id=old.id,created_by_id=old.created_by_id,assigned_to_id=old.created_by_id,
            inspected_by_id=old.created_by_id if done else None,operation_id=old.operation_id,
            location_id=old.location_id,zone_id=old.zone_id,title=old.title,checklist=checklist,
            status="legacy_completed" if done else "scheduled",outcome=old.outcome,
            findings=old.findings,score=score,scheduled_at=old.inspected_at or old.created_at,
            submitted_at=old.inspected_at if done else None)
        db.session.add(row);changed=True
    if changed: db.session.commit()

def _record_query(user, team=None):
    q = InspectionRecord.query.filter_by(organization_id=user.organization_id)
    if team: q = q.filter_by(team_id=team.id)
    if user.role == "Staff":
        q = q.filter_by(assigned_to_id=user.id)
    return q

def process_due_inspections():
    now=datetime.now(timezone.utc)
    schedules=InspectionSchedule.query.filter_by(active=True).all()
    for schedule in schedules:
        _materialize(schedule,now)
    if schedules:
        db.session.commit()

@quality_bp.get("/team-leader")
@roles_required("Staff", "Supervisor")
def my_team_leader_status():
    user = get_current_user()
    team, err = _team_scope(user)
    if err: return err
    assignment = TeamLeaderAssignment.query.filter_by(team_id=team.id, organization_id=user.organization_id).first()
    is_leader = bool(assignment and assignment.user_id == user.id)
    return jsonify({"is_team_leader": is_leader, "designation": "Team Leader" if is_leader else None,
        "team_id": team.id, "team_name": team.name,
        "team_leader": ({"id":assignment.user.id,"name":assignment.user.full_name} if assignment and assignment.user else None)}), 200

@quality_bp.get("/team-leader/<int:team_id>")
@roles_required("Supervisor")
def team_leader_detail(team_id):
    user = get_current_user()
    team = Team.query.filter_by(id=team_id, organization_id=user.organization_id, status="active").first()
    if not team or not TeamMember.query.filter_by(team_id=team.id, user_id=user.id).first():
        return jsonify({"error":"Team not found in your supervisor access."}), 404
    assignment = TeamLeaderAssignment.query.filter_by(team_id=team.id, organization_id=user.organization_id).first()
    return jsonify({"team_id":team.id, "team_leader": ({"id":assignment.user.id,"name":assignment.user.full_name} if assignment and assignment.user else None)}), 200

@quality_bp.put("/team-leader/<int:team_id>")
@roles_required("Supervisor")
def set_team_leader(team_id):
    user = get_current_user()
    team = Team.query.filter_by(id=team_id, organization_id=user.organization_id, status="active").first()
    if not team or not TeamMember.query.filter_by(team_id=team.id, user_id=user.id).first():
        return jsonify({"error":"Team not found in your supervisor access."}), 404
    data = request.get_json(silent=True) or {}
    staff_id = data.get("staff_id")
    if staff_id in (None, ""):
        current = TeamLeaderAssignment.query.filter_by(team_id=team.id, organization_id=user.organization_id).first()
        if current:
            _notify(current.user, "Team Leader designation removed", "Your Team Leader inspection access has been removed.")
            db.session.delete(current)
        db.session.add(AuditLog(actor_id=user.id, organization_id=user.organization_id, action="team_leader.removed",
            resource_type="Team", resource_id=str(team.id), details={"team_id":team.id}))
        db.session.commit()
        return jsonify({"message":"Team Leader designation removed.", "team_leader":None}), 200
    staff = User.query.join(TeamMember, TeamMember.user_id == User.id).filter(
        User.id == staff_id, User.organization_id == user.organization_id, User.role == "Staff",
        User.account_status == "active", TeamMember.team_id == team.id).first()
    if not staff: return jsonify({"error":"Select an active Staff member belonging to this team."}), 400
    current = TeamLeaderAssignment.query.filter_by(team_id=team.id, organization_id=user.organization_id).first()
    if current and current.user_id == staff.id:
        return jsonify({"message":"This person is already the Team Leader.", "team_leader":{"id":staff.id,"name":staff.full_name}}), 200
    if current:
        _notify(current.user, "Team Leader designation changed", "Another Staff member has been designated as your team's Team Leader.")
        db.session.delete(current)
    # A person may hold only one Team Leader designation across the organisation.
    other = TeamLeaderAssignment.query.filter_by(user_id=staff.id, organization_id=user.organization_id).first()
    if other: db.session.delete(other)
    assignment = TeamLeaderAssignment(organization_id=user.organization_id, team_id=team.id,
        user_id=staff.id, promoted_by_id=user.id)
    db.session.add(assignment)
    _notify(staff, "You have been designated Team Leader", "You now have access to your team's inspection workspace.")
    db.session.add(AuditLog(actor_id=user.id, organization_id=user.organization_id, action="team_leader.promoted",
        resource_type="User", resource_id=str(staff.id), details={"team_id":team.id,"team_name":team.name}))
    try: db.session.commit()
    except Exception: db.session.rollback(); return jsonify({"error":"Could not update Team Leader assignment."}), 409
    return jsonify({"message":"Team Leader designation updated.", "team_leader":{"id":staff.id,"name":staff.full_name}}), 200

@quality_bp.get("/templates")
@roles_required("Supervisor")
def templates_list():
    user=get_current_user(); team,err=_team_scope(user)
    if err:return err
    rows=InspectionTemplate.query.filter_by(organization_id=user.organization_id,team_id=team.id).order_by(InspectionTemplate.is_archived,InspectionTemplate.name).all()
    return jsonify({"templates":[x.to_dict() for x in rows]}),200

@quality_bp.post("/templates")
@roles_required("Supervisor")
def templates_create():
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    d=request.get_json(silent=True) or {}
    try: checks=_checklist(d.get("checklist"))
    except ValueError as e:return jsonify({"error":str(e)}),400
    name=str(d.get("name") or "").strip()
    if not name:return jsonify({"error":"Template name is required."}),400
    category=str(d.get("category") or "general").lower()
    if category not in {"cleaning","safety","maintenance","general"}:return jsonify({"error":"Choose a valid template category."}),400
    row=InspectionTemplate(organization_id=user.organization_id,team_id=team.id,created_by_id=user.id,
        name=name[:140],category=category,description=str(d.get("description") or "").strip()[:500],checklist=checks)
    db.session.add(row);db.session.commit()
    return jsonify({"template":row.to_dict()}),201

@quality_bp.patch("/templates/<int:template_id>")
@roles_required("Supervisor")
def templates_update(template_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionTemplate.query.filter_by(id=template_id,organization_id=user.organization_id,team_id=team.id).first_or_404()
    d=request.get_json(silent=True) or {}
    if "name" in d:
        name=str(d.get("name") or "").strip()
        if not name:return jsonify({"error":"Template name cannot be empty."}),400
        row.name=name[:140]
    if "description" in d:row.description=str(d.get("description") or "").strip()[:500]
    if "category" in d:
        category=str(d.get("category") or "").lower()
        if category not in {"cleaning","safety","maintenance","general"}:return jsonify({"error":"Invalid category."}),400
        row.category=category
    if "checklist" in d:
        try:row.checklist=_checklist(d["checklist"])
        except ValueError as e:return jsonify({"error":str(e)}),400
    if "is_archived" in d:row.is_archived=bool(d["is_archived"])
    db.session.commit();return jsonify({"template":row.to_dict()}),200

@quality_bp.get("/schedules")
@roles_required("Supervisor")
def schedules_list():
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    _materialize_team(team.id,user.organization_id)
    rows=InspectionSchedule.query.filter_by(organization_id=user.organization_id,team_id=team.id).order_by(InspectionSchedule.first_due_at.desc()).all()
    return jsonify({"schedules":[{"id":s.id,"title":s.title,"assigned_to_id":s.assigned_to_id,"assigned_to":s.assignee.full_name if s.assignee else None,
        "recurrence":s.recurrence,"first_due_at":s.first_due_at.isoformat(),"active":s.active,"timezone":s.timezone_name} for s in rows]}),200

@quality_bp.patch("/schedules/<int:schedule_id>")
@roles_required("Supervisor")
def schedule_update(schedule_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionSchedule.query.filter_by(id=schedule_id,organization_id=user.organization_id,team_id=team.id).first_or_404()
    d=request.get_json(silent=True) or {}
    if "scheduled_at" in d:
        try: new_due=_dt(d.get("scheduled_at"),row.timezone_name)
        except ValueError as e:return jsonify({"error":str(e)}),400
        if new_due < datetime.now(timezone.utc):return jsonify({"error":"Choose a future date and time."}),400
        existing=InspectionRecord.query.filter_by(schedule_id=row.id).all()
        if any(x.status not in {"scheduled","cancelled"} for x in existing):
            return jsonify({"error":"A schedule cannot be rescheduled after an inspection has started or been submitted."}),409
        row.first_due_at=new_due
        if row.assignee and row.assigned_to_id!=row.created_by_id:
            _notify(row.assignee,"Inspection rescheduled",f"{row.title} has a new scheduled time: {new_due.isoformat()}.")
        pending=sorted(existing,key=lambda x:x.scheduled_at or datetime.min.replace(tzinfo=timezone.utc))
        if pending:
            pending[0].scheduled_at=new_due
            pending[0].status="scheduled"
            for extra in pending[1:]:
                db.session.delete(extra)
    if "active" in d:
        row.active=bool(d["active"])
        if row.assignee and row.assigned_to_id!=row.created_by_id:
            _notify(row.assignee,"Inspection schedule updated",f"The schedule for {row.title} was "+("paused." if not row.active else "resumed."))
        pending=InspectionRecord.query.filter_by(schedule_id=row.id,status="scheduled").all()
        if not row.active:
            for item in pending:item.status="cancelled"
        else:
            cancelled=InspectionRecord.query.filter_by(schedule_id=row.id,status="cancelled").order_by(InspectionRecord.scheduled_at.asc()).all()
            if cancelled:
                cancelled[0].status="scheduled"
                for extra in cancelled[1:]:db.session.delete(extra)
    db.session.commit()
    return jsonify({"message":"Inspection schedule updated.","active":row.active}),200

@quality_bp.post("/schedules")
@roles_required("Supervisor")
def schedule_create():
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    d=request.get_json(silent=True) or {};tzname=str(d.get("timezone") or "Asia/Kolkata")
    try: due=_dt(d.get("scheduled_at"),tzname);lid,zid,oid=_refs(user,team,d)
    except ValueError as e:return jsonify({"error":str(e)}),400
    if due < datetime.now(timezone.utc):return jsonify({"error":"Schedule the inspection for a future date and time."}),400
    template=None
    if d.get("template_id"):
        template=InspectionTemplate.query.filter_by(id=d.get("template_id"),organization_id=user.organization_id,team_id=team.id,is_archived=False).first()
        if not template:return jsonify({"error":"The selected template is unavailable."}),404
        checklist=[dict(x,checked=False) for x in (template.checklist or [])];title=str(d.get("title") or template.name).strip();template_id=template.id
    else:
        try:checklist=_checklist(d.get("checklist"))
        except ValueError as e:return jsonify({"error":str(e)}),400
        title=str(d.get("title") or "").strip();template_id=None
    if not title:return jsonify({"error":"Inspection title is required."}),400
    recurrence=str(d.get("recurrence") or "once").lower()
    if recurrence not in {"once","daily","weekly","monthly"}:return jsonify({"error":"Invalid recurrence."}),400
    days=d.get("recurrence_days") or []
    if recurrence=="weekly" and (not isinstance(days,list) or any(not isinstance(x,int) or x<0 or x>6 for x in days)):return jsonify({"error":"Choose valid weekdays."}),400
    day=d.get("recurrence_day")
    if recurrence=="monthly" and (not isinstance(day,int) or day<1 or day>31):return jsonify({"error":"Choose a monthly day between 1 and 31."}),400
    assignee_id=d.get("assigned_to_id") or user.id
    if str(assignee_id)!=str(user.id):
        assignment=TeamLeaderAssignment.query.filter_by(team_id=team.id,organization_id=user.organization_id,user_id=assignee_id).first()
        if not assignment:return jsonify({"error":"You can assign inspections only to yourself or your current Team Leader."}),400
        assignee=User.query.filter_by(id=assignee_id,organization_id=user.organization_id,role="Staff",account_status="active").first()
    else:assignee=user
    if not assignee:return jsonify({"error":"Inspector is not an active member of this team."}),400
    schedule=InspectionSchedule(organization_id=user.organization_id,team_id=team.id,created_by_id=user.id,
        assigned_to_id=assignee.id,template_id=template_id,title=title[:180],checklist_snapshot=checklist,
        location_id=lid,zone_id=zid,operation_id=oid,first_due_at=due,recurrence=recurrence,
        recurrence_days=days,recurrence_day=day,timezone_name=tzname)
    db.session.add(schedule);db.session.flush()
    record=InspectionRecord(organization_id=user.organization_id,team_id=team.id,schedule_id=schedule.id,
        template_id=template_id,created_by_id=user.id,assigned_to_id=assignee.id,operation_id=oid,
        location_id=lid,zone_id=zid,title=title[:180],checklist=checklist,scheduled_at=due,status="scheduled")
    db.session.add(record)
    db.session.flush()
    if assignee.id!=user.id:_notify(assignee,"Inspection assigned",f"{title} has been assigned to you.",record)
    db.session.commit()
    return jsonify({"schedule_id":schedule.id,"inspection":record.to_dict()}),201

@quality_bp.get("/inspections")
@roles_required("Supervisor","Staff","Admin")
def inspections_list():
    user=get_current_user()
    if user.role=="Supervisor":
        team,err=_team_scope(user)
        if err:return err
        _import_legacy_inspections(user,team)
        _materialize_team(team.id,user.organization_id)
        rows=_record_query(user,team).order_by(InspectionRecord.scheduled_at.desc()).limit(1000).all()
    elif user.role=="Staff":
        team,err=_team_scope(user)
        if err:return err
        if not _leader(team.id,user.id,user.organization_id):return jsonify({"error":"Team Leader inspection access is required."}),403
        _materialize_team(team.id,user.organization_id)
        rows=_record_query(user,team).order_by(InspectionRecord.scheduled_at.desc()).limit(500).all()
    else:
        _import_legacy_inspections(user)
        rows=InspectionRecord.query.filter_by(organization_id=user.organization_id).order_by(InspectionRecord.scheduled_at.desc()).limit(2000).all()
    return jsonify({"items":[x.to_dict() for x in rows]}),200

@quality_bp.patch("/inspections/<int:record_id>/submit")
@roles_required("Staff","Supervisor")
def inspection_submit(record_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionRecord.query.filter_by(id=record_id,organization_id=user.organization_id,team_id=team.id).first_or_404()
    if user.role=="Staff":
        if not _leader(team.id,user.id,user.organization_id) or row.assigned_to_id!=user.id:return jsonify({"error":"This inspection is not assigned to your Team Leader account."}),403
    elif row.assigned_to_id!=user.id and row.created_by_id!=user.id:return jsonify({"error":"This inspection is not assigned to you."}),403
    if row.status not in {"scheduled","in_progress","returned"}:return jsonify({"error":"This inspection cannot be submitted in its current state."}),409
    d=request.get_json(silent=True) or {};checks=d.get("checklist")
    if not isinstance(checks,list) or len(checks)!=len(row.checklist or []):return jsonify({"error":"Complete the full inspection checklist."}),400
    normalized=[]
    for old,new in zip(row.checklist,checks):
        if not isinstance(new,dict) or not isinstance(new.get("checked"),bool):return jsonify({"error":"Invalid checklist response."}),400
        normalized.append({"label":old["label"],"checked":new["checked"]})
    passed=sum(1 for x in normalized if x["checked"]);total=len(normalized)
    row.checklist=normalized;row.outcome="pass" if passed==total else "fail";row.score=round(passed/total*100,1) if total else 0
    row.findings=str(d.get("findings") or "").strip()[:4000]
    if passed<total and not row.findings:return jsonify({"error":"Add findings or corrective-action details for checklist items that failed."}),400
    row.inspected_by_id=user.id
    row.submitted_at=datetime.now(timezone.utc)
    if user.role=="Supervisor":
        # A Supervisor's own inspection does not enter their own review queue.
        row.status="approved";row.reviewed_by_id=user.id;row.reviewed_at=row.submitted_at
    else:
        row.status="submitted"
        supervisors=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(
            TeamMember.team_id==team.id,User.organization_id==user.organization_id,User.role=="Supervisor",User.account_status=="active").all()
        for sup in supervisors:_notify(sup,"Inspection submitted",f"{row.title} has been submitted for your review.",row)
    db.session.commit();return jsonify({"inspection":row.to_dict()}),200

@quality_bp.patch("/inspections/<int:record_id>/review")
@roles_required("Supervisor")
def inspection_review(record_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionRecord.query.filter_by(id=record_id,organization_id=user.organization_id,team_id=team.id).first_or_404()
    if row.status!="submitted":return jsonify({"error":"Only submitted inspections can be reviewed."}),409
    d=request.get_json(silent=True) or {};action=str(d.get("action") or "").lower()
    if action not in {"approve","return"}:return jsonify({"error":"Choose approve or return."}),400
    note=str(d.get("notes") or "").strip()[:4000]
    if action=="return" and not note:return jsonify({"error":"Explain what needs correction before returning the inspection."}),400
    row.reviewed_by_id=user.id;row.reviewed_at=datetime.now(timezone.utc);row.review_notes=note
    row.status="approved" if action=="approve" else "returned"
    if action=="return":
        _notify(row.inspector or row.assignee,"Inspection returned for correction",note,row)
    else:
        _notify(row.assignee,"Inspection approved",f"{row.title} was approved by your Supervisor.",row)
    db.session.commit();return jsonify({"inspection":row.to_dict()}),200

@quality_bp.post("/inspections/<int:record_id>/forward")
@roles_required("Supervisor")
def inspection_forward(record_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionRecord.query.filter_by(id=record_id,organization_id=user.organization_id,team_id=team.id,status="approved").first_or_404()
    row.forwarded_at=datetime.now(timezone.utc);row.status="forwarded"
    admins=User.query.filter_by(organization_id=user.organization_id,role="Admin",account_status="active").all()
    for admin in admins:_notify(admin,"Inspection report forwarded",f"{row.title} was forwarded by {user.full_name}.",row)
    db.session.commit();return jsonify({"inspection":row.to_dict()}),200

@quality_bp.patch("/inspections/<int:record_id>/admin-review")
@roles_required("Admin")
def inspection_admin_review(record_id):
    user=get_current_user();row=InspectionRecord.query.filter_by(id=record_id,organization_id=user.organization_id,status="forwarded").first_or_404()
    d=request.get_json(silent=True) or {};action=str(d.get("action") or "").lower()
    if action not in {"acknowledge","return"}:return jsonify({"error":"Choose acknowledge or return."}),400
    notes=str(d.get("notes") or "").strip()[:4000]
    if action=="return" and not notes:return jsonify({"error":"Add a reason before returning the report."}),400
    row.admin_reviewed_at=datetime.now(timezone.utc);row.admin_notes=notes
    row.status="admin_acknowledged" if action=="acknowledge" else "admin_returned"
    if action=="return":
        supervisors=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(TeamMember.team_id==row.team_id,User.role=="Supervisor",User.organization_id==user.organization_id,User.account_status=="active").all()
        for sup in supervisors:_notify(sup,"Admin returned an inspection report",notes,row)
    db.session.commit();return jsonify({"inspection":row.to_dict()}),200

@quality_bp.post("/inspections/<int:record_id>/resend-admin")
@roles_required("Supervisor")
def inspection_resend_admin(record_id):
    user=get_current_user();team,err=_team_scope(user)
    if err:return err
    row=InspectionRecord.query.filter_by(id=record_id,organization_id=user.organization_id,team_id=team.id,status="admin_returned").first_or_404()
    d=request.get_json(silent=True) or {}
    row.admin_notes=str(d.get("notes") or "").strip()[:4000]
    row.admin_reviewed_at=None
    row.forwarded_at=datetime.now(timezone.utc)
    row.status="forwarded"
    admins=User.query.filter_by(organization_id=user.organization_id,role="Admin",account_status="active").all()
    for admin in admins:_notify(admin,"Updated inspection report resubmitted",f"{row.title} has been resubmitted by {user.full_name}.",row)
    db.session.commit()
    return jsonify({"inspection":row.to_dict()}),200

@quality_bp.get("/analytics")
@roles_required("Supervisor","Admin")
def quality_analytics():
    user=get_current_user()
    if user.role=="Supervisor":
        team,err=_team_scope(user)
        if err:return err
        q=InspectionRecord.query.filter_by(organization_id=user.organization_id,team_id=team.id)
    else:q=InspectionRecord.query.filter_by(organization_id=user.organization_id)
    _import_legacy_inspections(user,team if user.role=="Supervisor" else None)
    rows=q.order_by(InspectionRecord.scheduled_at.desc()).limit(5000).all()
    completed=[r for r in rows if r.status in {"approved","forwarded","admin_acknowledged","admin_returned","legacy_completed"}]
    passed=[r for r in completed if r.outcome=="pass"]
    failed=[r for r in completed if r.outcome=="fail"]
    now=datetime.now(timezone.utc)
    pending=[r for r in rows if r.status in {"scheduled","in_progress","submitted","returned"}]
    overdue=[r for r in pending if r.scheduled_at and r.scheduled_at<now]
    item_failures={}
    location_groups={}
    for r in completed:
        key=(r.location_id,r.zone_id)
        group=location_groups.setdefault(key,{"location":r.location.name if r.location else "Unspecified location",
            "zone":r.zone.name if r.zone else "Unspecified zone","completed":0,"passed":0,"failed":0})
        group["completed"]+=1
        if r.outcome=="pass":group["passed"]+=1
        if r.outcome=="fail":group["failed"]+=1
        for item in (r.checklist or []):
            if isinstance(item,dict) and not item.get("checked"):
                label=item.get("label","Unspecified item")
                item_failures[label]=item_failures.get(label,0)+1
    recurring=[{"checklist_item":k,"failures":v} for k,v in item_failures.items() if v>=2]
    by_location=[{**g,"pass_rate":round(g["passed"]/g["completed"]*100,1) if g["completed"] else None}
        for g in location_groups.values()]
    trend=[]
    month_start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    for offset in range(5,-1,-1):
        year=month_start.year;month=month_start.month-offset
        while month<=0:year-=1;month+=12
        while month>12:year+=1;month-=12
        start_month=month_start.replace(year=year,month=month)
        end_month=start_month.replace(year=year+1,month=1) if month==12 else start_month.replace(month=month+1)
        subset=[r for r in completed if r.scheduled_at and start_month<=r.scheduled_at<end_month]
        trend.append({"month":start_month.strftime("%b %Y"),"completed":len(subset),
            "passed":sum(1 for r in subset if r.outcome=="pass"),"failed":sum(1 for r in subset if r.outcome=="fail")})
    returned=sum(1 for r in rows if r.status in {"returned","admin_returned"})
    submitted=sum(1 for r in rows if r.submitted_at is not None)
    return jsonify({"total":len(rows),"completed":len(completed),"pending":len(pending),"overdue":len(overdue),
        "pass_rate":round(len(passed)/len(completed)*100,1) if completed else None,"passed":len(passed),"failed":len(failed),
        "returned":returned,"reinspection_rate":round(returned/submitted*100,1) if submitted else None,
        "average_score":round(sum(r.score for r in completed if r.score is not None)/max(1,sum(1 for r in completed if r.score is not None)),1) if any(r.score is not None for r in completed) else None,
        "repeated_failures":recurring,"by_location":by_location,"monthly_trend":trend,
        "recent":[r.to_dict() for r in rows[:20]]}),200
