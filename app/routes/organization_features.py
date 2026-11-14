from datetime import datetime, timezone, timedelta
import hashlib
from decimal import Decimal, InvalidOperation
from flask import Blueprint, jsonify, request
from sqlalchemy import or_, cast, String

from app.extensions import db
from app.models.organization_features import TeamPolicy, InventoryItem, EscalationRule, EscalationEvent
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.operation import Operation
from app.models.notification import Notification
from app.models.incident_report import IncidentReport
from app.models.operational_records import ShiftHandover
from app.models.quality_inspection import InspectionRecord
from app.services.permissions import get_current_user, roles_required

org_tools_bp = Blueprint("org_tools", __name__, url_prefix="/api/org-tools")
ALL_ROLES = ("Admin", "Supervisor", "Staff")
DONE = {"completed", "complete", "approved", "done", "closed", "cancelled", "canceled"}

def _team_ids(user):
    return [r.team_id for r in TeamMember.query.join(Team, TeamMember.team_id == Team.id)
            .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id,
                    Team.status == "active").all()]

def _can_manage_team(user, team_id):
    if user.role == "Admin":
        return Team.query.filter_by(id=team_id, organization_id=user.organization_id).first()
    if user.role != "Supervisor" or team_id not in _team_ids(user):
        return None
    return Team.query.filter_by(id=team_id, organization_id=user.organization_id).first()

def _policy_dict(p):
    return {"id":p.id,"team_id":p.team_id,"title":p.title,"objectives":p.objectives or "",
            "responsibilities":p.responsibilities or "","instructions":p.instructions or "",
            "standards":p.standards or "","safety_guidance":p.safety_guidance or "",
            "status":p.status,"version":p.version,"author_id":p.author_id,
            "published_at":p.published_at.isoformat() if p.published_at else None,
            "updated_at":p.updated_at.isoformat() if p.updated_at else None}

def _inventory_dict(x):
    return {"id":x.id,"name":x.name,"category":x.category,"description":x.description or "",
            "sku":x.sku or "","asset_tag":x.asset_tag or "","serial_number":x.serial_number or "",
            "quantity":float(x.quantity or 0),"minimum_quantity":float(x.minimum_quantity or 0),
            "unit":x.unit,"condition":x.condition,"status":x.status,
            "storage_location":x.storage_location or "","location_id":x.location_id,"zone_id":x.zone_id,
            "team_id":x.team_id,"assigned_to_id":x.assigned_to_id,
            "purchase_date":x.purchase_date.isoformat() if x.purchase_date else None,
            "warranty_until":x.warranty_until.isoformat() if x.warranty_until else None,
            "maintenance_due_at":x.maintenance_due_at.isoformat() if x.maintenance_due_at else None,
            "last_maintenance_at":x.last_maintenance_at.isoformat() if x.last_maintenance_at else None,
            "notes":x.notes or "","low_stock":float(x.quantity or 0) <= float(x.minimum_quantity or 0)}

def _notify(user, title, message, kind, resource_type, resource_id):
    db.session.add(Notification(recipient_id=user.id, organization_id=user.organization_id,
        title=title[:160], message=message[:1000], notification_type=kind,
        resource_type=resource_type, resource_id=str(resource_id)))

@org_tools_bp.get("/team-policy")
@roles_required(*ALL_ROLES)
def get_team_policy():
    user=get_current_user()
    raw=request.args.get("team_id")
    if user.role=="Admin":
        q=Team.query.filter_by(organization_id=user.organization_id,status="active")
        if raw:
            q=q.filter_by(id=int(raw)) if raw.isdigit() else q.filter(Team.id < 0)
        teams=q.order_by(Team.name.asc()).all()
    else:
        ids=_team_ids(user)
        q=Team.query.filter(Team.organization_id==user.organization_id,Team.id.in_(ids))
        if raw: q=q.filter_by(id=int(raw)) if raw.isdigit() else q.filter(Team.id < 0)
        teams=q.all()
    result=[]
    for team in teams:
        policy=TeamPolicy.query.filter_by(organization_id=user.organization_id,team_id=team.id,status="published").order_by(TeamPolicy.version.desc(),TeamPolicy.id.desc()).first()
        drafts=[]
        if user.role in {"Admin","Supervisor"}:
            drafts=[_policy_dict(row) for row in TeamPolicy.query.filter_by(
                organization_id=user.organization_id,team_id=team.id,status="draft"
            ).order_by(TeamPolicy.updated_at.desc(),TeamPolicy.id.desc()).all()]
        result.append({"team":{"id":team.id,"name":team.name},"policy":_policy_dict(policy) if policy else None,"drafts":drafts})
    return jsonify({"teams":result}),200

@org_tools_bp.post("/team-policy")
@roles_required("Admin","Supervisor")
def save_team_policy():
    user=get_current_user(); data=request.get_json(silent=True) or {}
    team_id=data.get("team_id")
    if not isinstance(team_id,int): return jsonify({"error":"Choose a valid team."}),400
    team=_can_manage_team(user,team_id)
    if not team: return jsonify({"error":"You can only manage policy for teams you administer or supervise."}),403
    title=str(data.get("title") or "").strip()
    if not title or len(title)>180: return jsonify({"error":"Policy title is required (maximum 180 characters)."}),400
    status="published" if data.get("publish") is True else "draft"
    policy_id=data.get("policy_id")
    if policy_id is not None:
        policy=TeamPolicy.query.filter_by(id=policy_id,organization_id=user.organization_id,
            team_id=team.id,status="draft").first()
        if policy is None:return jsonify({"error":"Draft not found or no longer editable."}),404
    else:
        latest=TeamPolicy.query.filter_by(organization_id=user.organization_id,team_id=team.id).order_by(TeamPolicy.version.desc()).first()
        policy=TeamPolicy(organization_id=user.organization_id,team_id=team.id,author_id=user.id,
            version=(latest.version+1 if latest else 1))
        db.session.add(policy)
    policy.title=title
    policy.objectives=str(data.get("objectives") or "")[:12000]
    policy.responsibilities=str(data.get("responsibilities") or "")[:12000]
    policy.instructions=str(data.get("instructions") or "")[:20000]
    policy.standards=str(data.get("standards") or "")[:12000]
    policy.safety_guidance=str(data.get("safety_guidance") or "")[:12000]
    policy.status=status
    policy.published_at=datetime.now(timezone.utc) if status=="published" else None
    db.session.flush()
    if status=="published":
        recipients=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(
            TeamMember.team_id==team.id,User.organization_id==user.organization_id,
            User.account_status=="active",User.id!=user.id).all()
        for recipient in recipients:
            _notify(recipient,"Team policy published",f"{team.name}: {title} (version {policy.version}) is now available.","team_policy","TeamPolicy",policy.id)
    db.session.commit()
    return jsonify({"policy":_policy_dict(policy),"message":"Policy published to the team." if status=="published" else "Draft saved."}),201

@org_tools_bp.get("/inventory")
@roles_required(*ALL_ROLES)
def inventory_list():
    user=get_current_user()
    q=InventoryItem.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id)) if ids else q.filter(InventoryItem.assigned_to_id==user.id)
    elif user.role=="Staff":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id)) if ids else q.filter(InventoryItem.assigned_to_id==user.id)
    rows=q.order_by(InventoryItem.name.asc()).limit(1000).all()
    return jsonify({"items":[_inventory_dict(x) for x in rows],"can_manage":user.role=="Admin"}),200

@org_tools_bp.post("/inventory")
@roles_required("Admin")
def inventory_create():
    user=get_current_user(); d=request.get_json(silent=True) or {}
    name=str(d.get("name") or "").strip()
    if not name or len(name)>180:return jsonify({"error":"Item name is required (maximum 180 characters)."}),400
    try:
        quantity=Decimal(str(d.get("quantity",1))); minimum=Decimal(str(d.get("minimum_quantity",0)))
        if quantity<0 or minimum<0: raise InvalidOperation()
    except (InvalidOperation,ValueError):return jsonify({"error":"Quantity values must be zero or greater."}),400
    for key, model in (("team_id",Team),):
        if d.get(key) is not None and not model.query.filter_by(id=d[key],organization_id=user.organization_id).first():
            return jsonify({"error":"Selected team is outside your organization."}),400
    item=InventoryItem(organization_id=user.organization_id,name=name,category=str(d.get("category") or "General")[:80],
        description=str(d.get("description") or "")[:8000],sku=str(d.get("sku") or "")[:80],
        asset_tag=str(d.get("asset_tag") or "")[:100],serial_number=str(d.get("serial_number") or "")[:120],
        quantity=quantity,minimum_quantity=minimum,unit=str(d.get("unit") or "units")[:32],
        condition=str(d.get("condition") or "good")[:24],status=str(d.get("status") or "available")[:24],
        storage_location=str(d.get("storage_location") or "")[:180],team_id=d.get("team_id"),
        assigned_to_id=d.get("assigned_to_id"),purchase_date=datetime.strptime(d["purchase_date"],"%Y-%m-%d").date() if d.get("purchase_date") else None,
        warranty_until=datetime.strptime(d["warranty_until"],"%Y-%m-%d").date() if d.get("warranty_until") else None,
        notes=str(d.get("notes") or "")[:8000],
        maintenance_due_at=datetime.strptime(d["maintenance_due_at"],"%Y-%m-%d").replace(tzinfo=timezone.utc) if d.get("maintenance_due_at") else None,
        last_maintenance_at=datetime.strptime(d["last_maintenance_at"],"%Y-%m-%d").replace(tzinfo=timezone.utc) if d.get("last_maintenance_at") else None,
        created_by_id=user.id)
    db.session.add(item);db.session.commit()
    return jsonify({"item":_inventory_dict(item),"message":"Inventory item saved."}),201

@org_tools_bp.patch("/inventory/<int:item_id>")
@roles_required("Admin")
def inventory_update(item_id):
    user=get_current_user(); item=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).first()
    if not item:return jsonify({"error":"Inventory item not found."}),404
    d=request.get_json(silent=True) or {}
    fields={"name":180,"category":80,"description":8000,"sku":80,"asset_tag":100,"serial_number":120,
            "unit":32,"condition":24,"status":24,"storage_location":180,"notes":8000}
    for key,limit in fields.items():
        if key in d:setattr(item,key,str(d[key] or "")[:limit])
    for key in ("quantity","minimum_quantity"):
        if key in d:
            try:
                value=Decimal(str(d[key]))
                if value<0:raise InvalidOperation()
                setattr(item,key,value)
            except (InvalidOperation,ValueError):return jsonify({"error":f"{key} must be zero or greater."}),400
    for key in ("purchase_date","warranty_until"):
        if key in d:
            try:setattr(item,key,datetime.strptime(d[key],"%Y-%m-%d").date() if d[key] else None)
            except (TypeError,ValueError):return jsonify({"error":f"Invalid {key} date."}),400
    for key in ("maintenance_due_at","last_maintenance_at"):
        if key in d:
            try:setattr(item,key,datetime.strptime(d[key],"%Y-%m-%d").replace(tzinfo=timezone.utc) if d[key] else None)
            except (TypeError,ValueError):return jsonify({"error":f"Invalid {key} date."}),400
    if "team_id" in d:
        if d["team_id"] is not None and not Team.query.filter_by(id=d["team_id"],organization_id=user.organization_id).first():return jsonify({"error":"Invalid team."}),400
        item.team_id=d["team_id"]
    db.session.commit();return jsonify({"item":_inventory_dict(item)}),200

@org_tools_bp.get("/escalations")
@roles_required(*ALL_ROLES)
def escalation_list():
    user=get_current_user(); _evaluate_escalations(user.organization_id)
    q=EscalationEvent.query.filter_by(organization_id=user.organization_id)
    if user.role!="Admin":
        ids=_team_ids(user)
        if user.role=="Supervisor":q=q.filter(EscalationEvent.team_id.in_(ids)) if ids else q.filter(EscalationEvent.id<0)
        else:
            assigned=Operation.query.filter_by(organization_id=user.organization_id,assigned_to_id=user.id).with_entities(cast(Operation.id, String))
            incidents=IncidentReport.query.filter_by(organization_id=user.organization_id,reported_by_id=user.id).with_entities(cast(IncidentReport.id, String))
            handovers=ShiftHandover.query.filter_by(organization_id=user.organization_id,created_by_id=user.id).with_entities(cast(ShiftHandover.id, String))
            inspections=InspectionRecord.query.filter_by(organization_id=user.organization_id,assigned_to_id=user.id).with_entities(cast(InspectionRecord.id, String))
            q=q.filter(or_(
                (EscalationEvent.resource_type=="Operation") & EscalationEvent.resource_id.in_(assigned),
                (EscalationEvent.resource_type=="IncidentReport") & EscalationEvent.resource_id.in_(incidents),
                (EscalationEvent.resource_type=="ShiftHandover") & EscalationEvent.resource_id.in_(handovers),
                (EscalationEvent.resource_type=="InspectionRecord") & EscalationEvent.resource_id.in_(inspections)
            ))
    events=q.order_by(EscalationEvent.created_at.desc()).limit(300).all()
    serialized=[]
    for e in events:
        can_update=user.role=="Admin" or (user.role=="Supervisor" and e.team_id in _team_ids(user))
        if user.role=="Staff" and e.resource_type=="Operation" and e.resource_id.isdigit():
            can_update=Operation.query.filter_by(id=int(e.resource_id),organization_id=user.organization_id,assigned_to_id=user.id).first() is not None
        serialized.append({"id":e.id,"team_id":e.team_id,"resource_type":e.resource_type,"resource_id":e.resource_id,
            "title":e.title,"message":e.message,"severity":e.severity,"status":e.status,"can_update":can_update,
            "created_at":e.created_at.isoformat(),"acknowledged_at":e.acknowledged_at.isoformat() if e.acknowledged_at else None,
            "resolved_at":e.resolved_at.isoformat() if e.resolved_at else None})
    return jsonify({"events":serialized,
        "rules":[{"id":r.id,"name":r.name,"trigger_type":r.trigger_type,"threshold_minutes":r.threshold_minutes,
        "severity":r.severity,"team_id":r.team_id,"notify_admin":r.notify_admin,"enabled":r.enabled}
        for r in EscalationRule.query.filter_by(organization_id=user.organization_id).order_by(EscalationRule.id.asc()).all()] if user.role=="Admin" else []}),200

@org_tools_bp.post("/escalations/rules")
@roles_required("Admin")
def escalation_rule_create():
    user=get_current_user();d=request.get_json(silent=True) or {}
    name=str(d.get("name") or "").strip(); trigger=d.get("trigger_type","operation_overdue")
    if not name or len(name)>160:return jsonify({"error":"Rule name is required."}),400
    if trigger not in {"operation_overdue","high_priority_overdue","incident_unacknowledged","inspection_failed","handover_unacknowledged"}:return jsonify({"error":"Unsupported trigger type."}),400
    try:threshold=int(d.get("threshold_minutes",60))
    except (TypeError,ValueError):return jsonify({"error":"Threshold must be minutes."}),400
    if threshold<1 or threshold>525600:return jsonify({"error":"Threshold must be between 1 minute and 365 days."}),400
    team_id=d.get("team_id")
    if team_id is not None and not Team.query.filter_by(id=team_id,organization_id=user.organization_id).first():return jsonify({"error":"Invalid team."}),400
    rule=EscalationRule(organization_id=user.organization_id,name=name,trigger_type=trigger,threshold_minutes=threshold,
        severity=d.get("severity","medium") if d.get("severity") in {"low","medium","high","critical"} else "medium",
        team_id=team_id,notify_admin=bool(d.get("notify_admin",True)),enabled=bool(d.get("enabled",True)),created_by_id=user.id)
    db.session.add(rule);db.session.commit();return jsonify({"rule_id":rule.id,"message":"Escalation rule created."}),201

@org_tools_bp.patch("/escalations/<int:event_id>")
@roles_required(*ALL_ROLES)
def escalation_update(event_id):
    user=get_current_user();event=EscalationEvent.query.filter_by(id=event_id,organization_id=user.organization_id).first()
    if not event:return jsonify({"error":"Escalation not found."}),404
    if user.role=="Supervisor" and event.team_id not in _team_ids(user):return jsonify({"error":"This escalation is outside your teams."}),403
    if user.role=="Staff":
        op=Operation.query.filter_by(id=int(event.resource_id),organization_id=user.organization_id,assigned_to_id=user.id).first() if event.resource_type=="Operation" and event.resource_id.isdigit() else None
        if not op:return jsonify({"error":"You cannot update this escalation."}),403
    d=request.get_json(silent=True) or {}; action=d.get("action")
    if action=="acknowledge" and event.status=="open":
        event.status="acknowledged";event.acknowledged_by_id=user.id;event.acknowledged_at=datetime.now(timezone.utc)
    elif action=="resolve":
        event.status="resolved";event.resolved_by_id=user.id;event.resolved_at=datetime.now(timezone.utc)
    else:return jsonify({"error":"Choose acknowledge or resolve for an open escalation."}),400
    db.session.commit();return jsonify({"message":"Escalation updated.","status":event.status}),200

@org_tools_bp.get("/workforce")
@roles_required(*ALL_ROLES)
def workforce_capacity():
    user=get_current_user(); ids=_team_ids(user)
    tq=Team.query.filter_by(organization_id=user.organization_id,status="active")
    if user.role!="Admin":tq=tq.filter(Team.id.in_(ids)) if ids else tq.filter(Team.id<0)
    teams=tq.order_by(Team.name.asc()).all(); result=[]
    for team in teams:
        member_ids=[x.user_id for x in TeamMember.query.filter_by(team_id=team.id).all()]
        members=User.query.filter(User.id.in_(member_ids),User.organization_id==user.organization_id,User.account_status=="active").all() if member_ids else []
        open_ops=Operation.query.filter(Operation.organization_id==user.organization_id,Operation.team_id==team.id,Operation.status.notin_(list(DONE))).all()
        workload=sum(int(op.estimated_minutes or 0) for op in open_ops)
        people=[]
        if user.role != "Staff":
            for person in members:
                assigned=[op for op in open_ops if op.assigned_to_id==person.id]
                people.append({"id":person.id,"name":person.full_name,"open_operations":len(assigned),
                    "estimated_minutes":sum(int(op.estimated_minutes or 0) for op in assigned)})
        else:
            own=[op for op in open_ops if op.assigned_to_id==user.id]
            people=[{"id":user.id,"name":"My workload","open_operations":len(own),
                "estimated_minutes":sum(int(op.estimated_minutes or 0) for op in own)}]
        unassigned_count=sum(1 for op in open_ops if not op.assigned_to_id)
        missing_estimates=sum(1 for op in open_ops if op.estimated_minutes is None)
        workload_values=[p["estimated_minutes"] for p in people]
        average_workload=(sum(workload_values)/len(workload_values)) if workload_values else 0
        overloaded=[p for p in people if average_workload and p["estimated_minutes"]>average_workload*1.5 and p["estimated_minutes"]>=60]
        recommendations=[]
        if unassigned_count and user.role!="Staff":recommendations.append(f"{unassigned_count} open operation(s) are not assigned to an individual.")
        if overloaded and user.role!="Staff":recommendations.append("Workload is uneven: "+", ".join(p["name"] for p in overloaded)+" have more than 1.5× the team average estimated workload.")
        if missing_estimates and user.role!="Staff":recommendations.append(f"{missing_estimates} open operation(s) have no effort estimate, so workload totals may be understated.")
        if team.capacity is not None and len(members)>team.capacity and user.role!="Staff":recommendations.append("Active membership exceeds the team's configured headcount capacity.")
        if user.role=="Staff" and not people:recommendations.append("No assigned workload was found for your account.")
        result.append({"team_id":team.id,"team_name":team.name,"staff_count":len(members) if user.role != "Staff" else None,
            "configured_capacity":team.capacity if user.role != "Staff" else None,
            "open_operations":len(open_ops) if user.role != "Staff" else len([op for op in open_ops if op.assigned_to_id==user.id]),
            "estimated_workload_minutes":workload if user.role != "Staff" else sum(int(op.estimated_minutes or 0) for op in open_ops if op.assigned_to_id==user.id),
            "unassigned_operations":unassigned_count if user.role!="Staff" else None,
            "missing_effort_estimates":missing_estimates if user.role!="Staff" else None,
            "recommendations":recommendations,"people":people,
            "capacity_flag":"over_capacity" if user.role != "Staff" and team.capacity is not None and len(members)>team.capacity else "within_capacity"})
    return jsonify({"teams":result,"method":"Based on active team membership and open operations. Estimated workload uses operation estimated_minutes; missing estimates are not guessed."}),200

def _evaluate_escalations(organization_id):
    # Organisation defaults are system-managed. No manual rule setup is needed.
    defaults=[
        ("MECHNAR · High incident unresolved","incident_high_unresolved",30,"high"),
        ("MECHNAR · Critical incident unresolved","incident_critical_unresolved",5,"critical"),
        ("MECHNAR · Repeated inspection failures","inspection_repeat_failure",0,"high"),
        ("MECHNAR · High-priority operation overdue","high_priority_overdue",30,"high"),
    ]
    existing={r.name for r in EscalationRule.query.filter_by(organization_id=organization_id).all()}
    system_actor=User.query.filter_by(organization_id=organization_id,role="Admin",account_status="active").first()
    if system_actor is None:
        system_actor=User.query.filter_by(organization_id=organization_id,account_status="active").first()
    if system_actor:
        for name,trigger,threshold,severity in defaults:
            if name not in existing:
                db.session.add(EscalationRule(organization_id=organization_id,name=name,
                    trigger_type=trigger,threshold_minutes=threshold,severity=severity,
                    team_id=None,notify_admin=True,enabled=True,created_by_id=system_actor.id))
        db.session.flush()
    now=datetime.now(timezone.utc)
    rules=EscalationRule.query.filter_by(organization_id=organization_id,enabled=True).all()
    for rule in rules:
        cutoff=now-timedelta(minutes=rule.threshold_minutes)
        candidates=[]
        if rule.trigger_type in {"operation_overdue","high_priority_overdue"}:
            q=Operation.query.filter(Operation.organization_id==organization_id,Operation.due_at.isnot(None),
                Operation.due_at<=cutoff,Operation.status.notin_(list(DONE)))
            if rule.team_id:q=q.filter(Operation.team_id==rule.team_id)
            if rule.trigger_type=="high_priority_overdue":q=q.filter(Operation.priority.in_(["High","Critical","Urgent"]))
            candidates=[("Operation",op.id,op.team_id,op.title,
                f"{op.title} is still open at least {rule.threshold_minutes} minutes after its deadline.",
                [op.assignee,op.assigned_supervisor]) for op in q.limit(500).all()]
        elif rule.trigger_type in {"incident_unacknowledged","incident_high_unresolved","incident_critical_unresolved"}:
            q=IncidentReport.query.filter(IncidentReport.organization_id==organization_id,
                IncidentReport.created_at<=cutoff,
                IncidentReport.status.notin_(["resolved","closed","rejected"]))
            if rule.trigger_type=="incident_unacknowledged":
                q=q.filter(IncidentReport.supervisor_acknowledged_at.is_(None))
            else:
                q=q.filter(IncidentReport.severity==rule.severity)
            if rule.team_id:q=q.filter(IncidentReport.team_id==rule.team_id)
            candidates=[("IncidentReport",x.id,x.team_id,x.title,
                f"{rule.severity.title()} priority incident remains unresolved after {rule.threshold_minutes} minutes.",
                [x.reporter,x.supervisor]) for x in q.limit(500).all()]
        elif rule.trigger_type=="inspection_failed":
            q=InspectionRecord.query.filter(InspectionRecord.organization_id==organization_id,
                InspectionRecord.outcome=="fail",InspectionRecord.status.notin_(["approved","closed"]))
            if rule.team_id:q=q.filter(InspectionRecord.team_id==rule.team_id)
            candidates=[("InspectionRecord",x.id,x.team_id,x.title,
                "A failed inspection requires follow-up and review.",[x.assignee,x.reviewer]) for x in q.limit(500).all()]
        elif rule.trigger_type=="inspection_repeat_failure":
            since=now-timedelta(days=30)
            failed=InspectionRecord.query.filter(InspectionRecord.organization_id==organization_id,
                InspectionRecord.outcome=="fail",InspectionRecord.submitted_at>=since).order_by(
                InspectionRecord.submitted_at.desc()).limit(1500).all()
            patterns={}
            for row in failed:
                key=(row.team_id,row.location_id,row.zone_id,(row.title or "").strip().lower())
                patterns.setdefault(key,[]).append(row)
            for key,rows in patterns.items():
                if len(rows)<2:continue
                latest=rows[0]
                fingerprint=hashlib.sha1("|".join(map(str,key)).encode("utf-8")).hexdigest()[:24]
                recipients=[latest.assignee,latest.reviewer]
                recipients.extend(User.query.join(TeamMember,TeamMember.user_id==User.id).filter(
                    TeamMember.team_id==latest.team_id,User.organization_id==organization_id,
                    User.role=="Supervisor",User.account_status=="active").all())
                candidates.append(("InspectionPattern","pattern:"+fingerprint,latest.team_id,
                    latest.title or "Inspection pattern",
                    f"MECHNAR detected {len(rows)} failed inspections with the same title and work area in the last 30 days. Review the recurring cause.",
                    recipients))
        elif rule.trigger_type=="handover_unacknowledged":
            q=ShiftHandover.query.filter(ShiftHandover.organization_id==organization_id,
                ShiftHandover.acknowledged_at.is_(None),ShiftHandover.outgoing_at<=cutoff,
                ShiftHandover.status.notin_(["draft","cancelled","canceled"]))
            if rule.team_id:q=q.filter(ShiftHandover.team_id==rule.team_id)
            candidates=[("ShiftHandover",x.id,x.team_id,x.shift_label,
                f"Shift handover has not been acknowledged within {rule.threshold_minutes} minutes.",
                [x.creator]) for x in q.limit(500).all()]
        for resource_type,resource_id,team_id,title,message,resource_recipients in candidates:
            exists=EscalationEvent.query.filter_by(organization_id=organization_id,rule_id=rule.id,
                resource_type=resource_type,resource_id=str(resource_id)).first()
            if exists:continue
            event=EscalationEvent(organization_id=organization_id,rule_id=rule.id,team_id=team_id,
                resource_type=resource_type,resource_id=str(resource_id),title=f"MECHNAR Alert: {title}",
                message=message,severity=rule.severity)
            db.session.add(event);db.session.flush()
            recipients=[x for x in resource_recipients if x]
            if rule.notify_admin:
                recipients.extend(User.query.filter_by(organization_id=organization_id,role="Admin",account_status="active").all())
            seen=set()
            for recipient in recipients:
                if recipient.id in seen or recipient.organization_id!=organization_id or recipient.account_status!="active":continue
                seen.add(recipient.id)
                _notify(recipient,"MECHNAR operational alert",message,"escalation","EscalationEvent",event.id)
    db.session.commit()
