from datetime import datetime, timezone, timedelta
import hashlib
from decimal import Decimal, InvalidOperation
from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import or_, cast, String

from app.extensions import db
from app.models.organization_features import TeamPolicy, InventoryItem, InventoryMovement, InventoryRequest, InventorySupplier, InventoryPurchase, InventoryPurchaseLine, InventoryTransfer, InventoryLocationStock, EscalationRule, EscalationEvent
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
    return {"id":p.id,"team_id":p.team_id,"title":p.title,"policy_type":getattr(p,"policy_type","structured") or "structured","objectives":p.objectives or "",
            "responsibilities":p.responsibilities or "","instructions":p.instructions or "",
            "standards":p.standards or "","safety_guidance":p.safety_guidance or "",
            "status":p.status,"version":p.version,"author_id":p.author_id,
            "published_at":p.published_at.isoformat() if p.published_at else None,
            "updated_at":p.updated_at.isoformat() if p.updated_at else None}

def _inventory_dict(x):
    return {"id":x.id,"name":x.name,"category":x.category,"description":x.description or "",
            "sku":x.sku or "","asset_tag":x.asset_tag or "","serial_number":x.serial_number or "",
            "quantity":float(x.quantity or 0),"minimum_quantity":float(x.minimum_quantity or 0),"maximum_quantity":float(getattr(x,"maximum_quantity",0) or 0),
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
    policy_type="custom" if data.get("policy_type")=="custom" else "structured"
    custom_content=str(data.get("custom_content") or "")[:50000].strip()
    if policy_type=="custom" and not custom_content:
        return jsonify({"error":"Write the policy before saving or publishing."}),400
    policy.policy_type=policy_type
    policy.title=title if policy_type=="structured" else (title if title else "Custom policy")
    policy.objectives=str(data.get("objectives") or "")[:12000] if policy_type=="structured" else ""
    policy.responsibilities=str(data.get("responsibilities") or "")[:12000] if policy_type=="structured" else ""
    policy.instructions=(str(data.get("instructions") or "")[:20000] if policy_type=="structured" else custom_content)
    policy.standards=str(data.get("standards") or "")[:12000] if policy_type=="structured" else ""
    policy.safety_guidance=str(data.get("safety_guidance") or "")[:12000] if policy_type=="structured" else ""
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

def _location_balance(user_id, organization_id, item_id, location_name, delta):
    name = str(location_name or "Unassigned / central stock").strip()[:180] or "Unassigned / central stock"
    row = InventoryLocationStock.query.filter_by(organization_id=organization_id,item_id=item_id,location_name=name).with_for_update().first()
    if row is None:
        row = InventoryLocationStock(organization_id=organization_id,item_id=item_id,location_name=name,quantity=0)
        db.session.add(row); db.session.flush()
    next_value = Decimal(str(row.quantity or 0)) + Decimal(str(delta))
    if next_value < 0: raise ValueError("Insufficient stock at the selected location.")
    row.quantity = next_value
    return row


@org_tools_bp.get("/inventory")
@roles_required(*ALL_ROLES)
def inventory_list():
    user=get_current_user()
    q=InventoryItem.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None))) if ids else q.filter(or_(InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None)))
    elif user.role=="Staff":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id)) if ids else q.filter(InventoryItem.assigned_to_id==user.id)
    rows=q.order_by(InventoryItem.name.asc()).limit(1000).all()
    return jsonify({"items":[_inventory_dict(x) for x in rows],"can_manage":user.role in {"Admin","Supervisor"}}),200

@org_tools_bp.post("/inventory")
@roles_required("Admin","Supervisor")
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
        quantity=quantity,minimum_quantity=minimum,maximum_quantity=Decimal(str(d.get("maximum_quantity",0) or 0)),unit=str(d.get("unit") or "units")[:32],
        condition=str(d.get("condition") or "good")[:24],status=str(d.get("status") or "available")[:24],
        storage_location=str(d.get("storage_location") or "")[:180],team_id=d.get("team_id"),
        assigned_to_id=d.get("assigned_to_id"),purchase_date=datetime.strptime(d["purchase_date"],"%Y-%m-%d").date() if d.get("purchase_date") else None,
        warranty_until=datetime.strptime(d["warranty_until"],"%Y-%m-%d").date() if d.get("warranty_until") else None,
        notes=str(d.get("notes") or "")[:8000],
        maintenance_due_at=datetime.strptime(d["maintenance_due_at"],"%Y-%m-%d").replace(tzinfo=timezone.utc) if d.get("maintenance_due_at") else None,
        last_maintenance_at=datetime.strptime(d["last_maintenance_at"],"%Y-%m-%d").replace(tzinfo=timezone.utc) if d.get("last_maintenance_at") else None,
        created_by_id=user.id)
    db.session.add(item);db.session.flush()
    if quantity > 0:
        _location_balance(user.id,user.organization_id,item.id,item.storage_location,quantity)
        db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="opening_balance",quantity=quantity,balance_after=quantity,reference="Initial registration",notes="Opening stock recorded when item was created.",performed_by_id=user.id))
    db.session.commit()
    return jsonify({"item":_inventory_dict(item),"message":"Inventory item saved."}),201

@org_tools_bp.patch("/inventory/<int:item_id>")
@roles_required("Admin","Supervisor")
def inventory_update(item_id):
    user=get_current_user(); item=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).first()
    if not item:return jsonify({"error":"Inventory item not found."}),404
    if user.role=="Supervisor" and item.team_id is not None and item.team_id not in _team_ids(user): return jsonify({"error":"This item is outside your team scope."}),403
    d=request.get_json(silent=True) or {}
    previous_quantity=Decimal(str(item.quantity or 0))
    fields={"name":180,"category":80,"description":8000,"sku":80,"asset_tag":100,"serial_number":120,
            "unit":32,"condition":24,"status":24,"storage_location":180,"notes":8000}
    for key,limit in fields.items():
        if key in d:setattr(item,key,str(d[key] or "")[:limit])
    for key in ("quantity","minimum_quantity","maximum_quantity"):
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
    if "quantity" in d and Decimal(str(item.quantity or 0)) != previous_quantity:
        db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="adjustment",quantity=abs(Decimal(str(item.quantity or 0))-previous_quantity),balance_after=item.quantity,reference="Item edit",notes="Stock balance changed through item maintenance form.",performed_by_id=user.id))
    if "quantity" in d and Decimal(str(item.quantity or 0)) != previous_quantity:
        try: _location_balance(user.id,user.organization_id,item.id,item.storage_location,Decimal(str(item.quantity or 0))-previous_quantity)
        except ValueError as exc: db.session.rollback();return jsonify({"error":str(exc)}),409
    if Decimal(str(getattr(item,"maximum_quantity",0) or 0))>0 and Decimal(str(item.maximum_quantity))<Decimal(str(item.minimum_quantity or 0)):
        db.session.rollback();return jsonify({"error":"Maximum stock must be greater than or equal to minimum stock."}),400
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


@org_tools_bp.get("/inventory/<int:item_id>/movements")
@roles_required(*ALL_ROLES)
def inventory_movements(item_id):
    user=get_current_user(); item=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).first()
    if not item:return jsonify({"error":"Inventory item not found."}),404
    if user.role=="Staff" and item.team_id not in _team_ids(user) and item.assigned_to_id!=user.id:return jsonify({"error":"Not permitted."}),403
    rows=InventoryMovement.query.filter_by(organization_id=user.organization_id,item_id=item.id).order_by(InventoryMovement.created_at.desc()).limit(250).all()
    return jsonify({"movements":[{"id":x.id,"type":x.movement_type,"quantity":float(x.quantity),"balance_after":float(x.balance_after),"reference":x.reference or "","notes":x.notes or "","performed_by_id":x.performed_by_id,"created_at":x.created_at.isoformat()} for x in rows]}),200

@org_tools_bp.post("/inventory/<int:item_id>/movements")
@roles_required("Admin","Supervisor")
def inventory_record_movement(item_id):
    user=get_current_user(); item=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).with_for_update().first()
    if not item:return jsonify({"error":"Inventory item not found."}),404
    if user.role=="Supervisor" and item.team_id is not None and item.team_id not in _team_ids(user):return jsonify({"error":"This item is outside your team scope."}),403
    d=request.get_json(silent=True) or {}; kind=str(d.get("type") or "")
    if kind not in {"receipt","consumption","adjustment","transfer_out","transfer_in"}:return jsonify({"error":"Choose a valid stock movement."}),400
    try: qty=Decimal(str(d.get("quantity")))
    except (InvalidOperation,ValueError):return jsonify({"error":"Enter a valid quantity."}),400
    if qty<=0:return jsonify({"error":"Quantity must be greater than zero."}),400
    current=Decimal(str(item.quantity or 0)); balance=current+qty if kind in {"receipt","transfer_in"} else current-qty
    if kind=="adjustment":balance=qty
    if balance<0:return jsonify({"error":"Insufficient stock for this movement."}),409
    location=str(d.get("location") or item.storage_location or "Unassigned / central stock")
    if kind=="adjustment":
        current_loc=InventoryLocationStock.query.filter_by(organization_id=user.organization_id,item_id=item.id,location_name=location).with_for_update().first()
        prior=Decimal(str(current_loc.quantity or 0)) if current_loc else Decimal("0")
        balance=current+(qty-prior)
        if balance<0:return jsonify({"error":"The adjustment would make total stock negative."}),409
    maximum=Decimal(str(getattr(item,"maximum_quantity",0) or 0))
    if kind in {"receipt","transfer_in"} and maximum>0 and balance>maximum and not d.get("allow_over_maximum"):
        return jsonify({"error":"This receipt exceeds the maximum stock threshold.","maximum_quantity":float(maximum),"projected_quantity":float(balance),"requires_override":True}),409
    if kind in {"receipt","transfer_in"} and maximum>0 and balance>maximum and not str(d.get("override_reason") or "").strip():
        return jsonify({"error":"Enter a reason to override the maximum stock threshold."}),400
    delta=abs(balance-current); item.quantity=balance
    try:
        if kind in {"receipt","transfer_in"}: _location_balance(user.id,user.organization_id,item.id,location,delta)
        elif kind in {"consumption","transfer_out"}: _location_balance(user.id,user.organization_id,item.id,location,-delta)
        elif kind=="adjustment":
            current_loc=InventoryLocationStock.query.filter_by(organization_id=user.organization_id,item_id=item.id,location_name=location).with_for_update().first()
            prior=Decimal(str(current_loc.quantity or 0)) if current_loc else Decimal("0")
            _location_balance(user.id,user.organization_id,item.id,location,qty-prior)
    except ValueError as exc:
        db.session.rollback();return jsonify({"error":str(exc)}),409
    movement=InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type=kind,quantity=delta,balance_after=balance,reference=str(d.get("reference") or "")[:180],notes=str(d.get("notes") or "")[:4000],performed_by_id=user.id)
    db.session.add(movement);db.session.commit()
    return jsonify({"item":_inventory_dict(item),"message":"Stock movement recorded."}),201

@org_tools_bp.get("/inventory-requests")
@roles_required(*ALL_ROLES)
def inventory_requests_list():
    user=get_current_user(); q=InventoryRequest.query.filter_by(organization_id=user.organization_id)
    if user.role=="Staff":q=q.filter_by(requester_id=user.id)
    elif user.role=="Supervisor":
        ids=_team_ids(user);q=q.filter(or_(InventoryRequest.team_id.in_(ids),InventoryRequest.requester_id==user.id)) if ids else q.filter_by(requester_id=user.id)
    rows=q.order_by(InventoryRequest.created_at.desc()).limit(500).all()
    return jsonify({"requests":[{"id":x.id,"item_id":x.item_id,"item_name":x.item_name,"requester_id":x.requester_id,"team_id":x.team_id,"type":x.request_type,"quantity":float(x.quantity),"fulfilled_quantity":float(x.fulfilled_quantity or 0),"reason":x.reason or "","status":x.status,"created_at":x.created_at.isoformat()} for x in rows]}),200

@org_tools_bp.post("/inventory-requests")
@roles_required(*ALL_ROLES)
def inventory_request_create():
    user=get_current_user();d=request.get_json(silent=True) or {};name=str(d.get("item_name") or "").strip()
    try:qty=Decimal(str(d.get("quantity")))
    except (InvalidOperation,ValueError):return jsonify({"error":"Enter a valid requested quantity."}),400
    if not name or qty<=0:return jsonify({"error":"Item and a positive quantity are required."}),400
    item=None
    if d.get("item_id"):
        item=InventoryItem.query.filter_by(id=d["item_id"],organization_id=user.organization_id).first()
        if not item:return jsonify({"error":"Inventory item not found."}),404
        if user.role=="Staff" and item.team_id not in _team_ids(user) and item.assigned_to_id!=user.id:return jsonify({"error":"This item is outside your permitted inventory."}),403
        name=item.name
    team_ids=_team_ids(user); team_id=d.get("team_id")
    if user.role!="Admin":team_id=next(iter(team_ids),None)
    req=InventoryRequest(organization_id=user.organization_id,item_id=item.id if item else None,requester_id=user.id,team_id=team_id,request_type=str(d.get("type") or "supply")[:24],item_name=name,quantity=qty,reason=str(d.get("reason") or "")[:4000])
    db.session.add(req);db.session.commit()
    admins=User.query.filter_by(organization_id=user.organization_id,role="Admin",account_status="active").all()
    for admin in admins:_notify(admin,"Inventory request received",f"{user.full_name or user.email} requested {qty} {name}.","inventory_request","InventoryRequest",req.id)
    if team_ids:
        supervisors=User.query.join(TeamMember,TeamMember.user_id==User.id).filter(TeamMember.team_id.in_(team_ids),User.organization_id==user.organization_id,User.role=="Supervisor",User.account_status=="active").all()
        notified=set()
        for supervisor in supervisors:
            if supervisor.id not in notified and supervisor.id!=user.id:
                _notify(supervisor,"Team inventory request",f"{user.full_name or user.email} requested {qty} {name}.","inventory_request","InventoryRequest",req.id)
                notified.add(supervisor.id)
    db.session.commit()
    return jsonify({"id":req.id,"message":"Inventory request submitted."}),201

@org_tools_bp.patch("/inventory-requests/<int:request_id>")
@roles_required("Admin","Supervisor")
def inventory_request_update(request_id):
    user=get_current_user();req=InventoryRequest.query.filter_by(id=request_id,organization_id=user.organization_id).first()
    if not req:return jsonify({"error":"Request not found."}),404
    if user.role=="Supervisor" and req.team_id not in _team_ids(user):return jsonify({"error":"This request is outside your team scope."}),403
    d=request.get_json(silent=True) or {};status=str(d.get("status") or req.status)
    if req.status in {"fulfilled","rejected"}:return jsonify({"error":"This request is already closed."}),409
    if status not in {"approved","rejected","fulfilled","partially_fulfilled"}:return jsonify({"error":"Invalid request status."}),400
    if status in {"fulfilled","partially_fulfilled"}:
        if not req.item_id:return jsonify({"error":"Link this request to a catalogue item before recording fulfilment, so stock can be deducted accurately."}),409
        try:fulfilled=Decimal(str(d.get("fulfilled_quantity",req.quantity)))
        except (InvalidOperation,ValueError):return jsonify({"error":"Invalid fulfilled quantity."}),400
        previous=Decimal(str(req.fulfilled_quantity or 0))
        if fulfilled<previous or fulfilled>Decimal(str(req.quantity)) or (status=="partially_fulfilled" and fulfilled<=0):
            return jsonify({"error":"Fulfilled quantity must be greater than the amount already issued and no greater than requested."}),400
        delta=fulfilled-previous
        if delta>0:
            item=InventoryItem.query.filter_by(id=req.item_id,organization_id=user.organization_id).with_for_update().first()
            if not item or Decimal(str(item.quantity or 0))<delta:return jsonify({"error":"Insufficient stock to fulfil the remaining quantity."}),409
            item.quantity=Decimal(str(item.quantity))-delta
            db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="consumption",quantity=delta,balance_after=item.quantity,reference=f"Request #{req.id}",notes="Incremental request fulfilment",performed_by_id=user.id))
        req.fulfilled_quantity=fulfilled
    req.status=status;req.reviewed_by_id=user.id;req.reviewed_at=datetime.now(timezone.utc)
    db.session.commit()
    recipient=User.query.filter_by(id=req.requester_id,organization_id=user.organization_id).first()
    if recipient:_notify(recipient,"Inventory request updated",f"Request #{req.id} is now {status.replace('_',' ')}.","inventory_request","InventoryRequest",req.id);db.session.commit()
    return jsonify({"message":"Inventory request updated.","status":req.status}),200


# Procurement and receiving. A purchase never changes on-hand stock until a receiving
# action is confirmed; all writes are scoped to the authenticated organisation.
def _purchase_scope(user):
    q = InventoryPurchase.query.filter_by(organization_id=user.organization_id)
    if user.role == "Supervisor":
        ids = _team_ids(user)
        q = q.filter(InventoryPurchase.created_by_id == user.id) if not ids else q.filter(
            or_(InventoryPurchase.created_by_id == user.id,
                InventoryPurchase.created_by_id.in_(
                    db.session.query(InventoryPurchase.created_by_id).filter(
                        InventoryPurchase.organization_id == user.organization_id
                    )
                )))
    elif user.role == "Staff":
        q = q.filter(InventoryPurchase.id < 0)
    return q

def _purchase_dict(p):
    lines = InventoryPurchaseLine.query.filter_by(purchase_id=p.id, organization_id=p.organization_id).order_by(InventoryPurchaseLine.id.asc()).all()
    supplier = InventorySupplier.query.filter_by(id=p.supplier_id, organization_id=p.organization_id).first() if p.supplier_id else None
    return {"id":p.id,"supplier_id":p.supplier_id,"supplier_name":supplier.name if supplier else "Supplier not set",
        "invoice_number":p.invoice_number or "","invoice_date":p.invoice_date.isoformat() if p.invoice_date else None,
        "order_reference":p.purchase_order_number or "","status":p.status,"currency":p.currency,
        "subtotal":float(p.subtotal or 0),"tax_total":float(p.tax_total or 0),"grand_total":float(p.grand_total or 0),
        "invoice_filename":p.invoice_filename,"extraction_status":p.extraction_status,"notes":p.notes or "",
        "created_at":p.created_at.isoformat(),"approved_at":p.reviewed_at.isoformat() if p.reviewed_at else None,
        "received_at":p.received_at.isoformat() if p.received_at else None,
        "lines":[{"id":x.id,"item_id":x.item_id,"description":x.item_name,"sku":x.sku or "",
            "quantity":float(x.quantity_ordered),"received_quantity":float(x.quantity_received or 0),"unit":x.unit,
            "unit_price":float(x.unit_price or 0),"tax_amount":float(x.tax_amount or 0),"line_total":float(x.line_total or 0)} for x in lines]}

@org_tools_bp.get("/inventory-suppliers")
@roles_required("Admin","Supervisor")
def inventory_suppliers_list():
    user=get_current_user()
    rows=InventorySupplier.query.filter_by(organization_id=user.organization_id).order_by(InventorySupplier.name.asc()).all()
    return jsonify({"suppliers":[{"id":x.id,"name":x.name,"contact_name":x.contact_name or "",
        "email":x.email or "","phone":x.phone or "","tax_reference":x.tax_identifier or "",
        "address":x.address or "","notes":x.notes or ""} for x in rows]}),200

@org_tools_bp.post("/inventory-suppliers")
@roles_required("Admin","Supervisor")
def inventory_supplier_create():
    user=get_current_user(); d=request.get_json(silent=True) or {}; name=str(d.get("name") or "").strip()
    if not name or len(name)>180:return jsonify({"error":"Supplier name is required."}),400
    row=InventorySupplier(organization_id=user.organization_id,name=name,contact_name=str(d.get("contact_name") or "")[:120],
        email=str(d.get("email") or "")[:180],phone=str(d.get("phone") or "")[:60],
        tax_identifier=str(d.get("tax_reference") or "")[:80],address=str(d.get("address") or "")[:4000],
        notes=str(d.get("notes") or "")[:4000],created_by_id=user.id)
    db.session.add(row)
    try:db.session.commit()
    except Exception:
        db.session.rollback();return jsonify({"error":"A supplier with that name may already exist in this organisation."}),409
    return jsonify({"id":row.id,"name":row.name}),201

@org_tools_bp.get("/inventory-purchases")
@roles_required("Admin","Supervisor")
def inventory_purchases_list():
    user=get_current_user(); q=InventoryPurchase.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        # Supervisors see purchases they created; Admin retains organisation-wide oversight.
        q=q.filter_by(created_by_id=user.id)
    rows=q.order_by(InventoryPurchase.created_at.desc()).limit(500).all()
    return jsonify({"purchases":[_purchase_dict(x) for x in rows]}),200

@org_tools_bp.post("/inventory-purchases")
@roles_required("Admin","Supervisor")
def inventory_purchase_create():
    user=get_current_user(); d=request.get_json(silent=True) or {}
    supplier_name=str(d.get("supplier_name") or "").strip()
    invoice_number=str(d.get("invoice_number") or "").strip()
    raw_lines=d.get("lines") if isinstance(d.get("lines"),list) else []
    if not supplier_name or not raw_lines:return jsonify({"error":"Supplier and at least one purchase line are required."}),400
    supplier=None
    if d.get("supplier_id"):
        supplier=InventorySupplier.query.filter_by(id=d["supplier_id"],organization_id=user.organization_id).first()
        if not supplier:return jsonify({"error":"Supplier does not belong to this organisation."}),400
        supplier_name=supplier.name
    if invoice_number and InventoryPurchase.query.filter_by(organization_id=user.organization_id,invoice_number=invoice_number).first():
        return jsonify({"error":"This invoice number is already recorded in this organisation."}),409
    parsed=[]; subtotal=Decimal("0"); tax_total=Decimal("0")
    try:
        for line in raw_lines[:100]:
            desc=str(line.get("description") or "").strip()
            qty=Decimal(str(line.get("quantity"))); price=Decimal(str(line.get("unit_price",0))); tax=Decimal(str(line.get("tax_amount",0)))
            if not desc or qty<=0 or price<0 or tax<0:raise InvalidOperation()
            item_id=line.get("item_id")
            if item_id:
                linked=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).first()
                if not linked:return jsonify({"error":"A selected catalogue item is outside this organisation."}),400
                if user.role=="Supervisor" and linked.team_id is not None and linked.team_id not in _team_ids(user) and linked.assigned_to_id!=user.id:return jsonify({"error":"A linked item is outside your team scope."}),403
            total=(qty*price)+tax; subtotal += qty*price; tax_total += tax
            parsed.append((line,desc,qty,price,tax,total,item_id))
    except (InvalidOperation,ValueError,TypeError):
        return jsonify({"error":"Check every line: item name, positive quantity, non-negative price and tax are required."}),400
    if supplier is None:
        supplier=InventorySupplier.query.filter_by(organization_id=user.organization_id,name=supplier_name).first()
    if supplier is None:
        supplier=InventorySupplier(organization_id=user.organization_id,name=supplier_name,created_by_id=user.id)
        db.session.add(supplier);db.session.flush()
    try:invoice_date=datetime.strptime(d["invoice_date"],"%Y-%m-%d").date() if d.get("invoice_date") else None
    except ValueError:return jsonify({"error":"Invalid invoice date."}),400
    purchase=InventoryPurchase(organization_id=user.organization_id,supplier_id=supplier.id,
        invoice_number=invoice_number or None,invoice_date=invoice_date,
        purchase_order_number=str(d.get("order_reference") or "")[:100],status="draft",
        currency=str(d.get("currency") or "INR")[:8],subtotal=subtotal,tax_total=tax_total,
        grand_total=subtotal+tax_total,extraction_status="manual",notes=str(d.get("notes") or "")[:8000],created_by_id=user.id)
    db.session.add(purchase);db.session.flush()
    for line,desc,qty,price,tax,total,item_id in parsed:
        db.session.add(InventoryPurchaseLine(organization_id=user.organization_id,purchase_id=purchase.id,item_id=item_id,
            item_name=desc,sku=str(line.get("sku") or "")[:80],quantity_ordered=qty,quantity_received=Decimal("0"),
            unit=str(line.get("unit") or "units")[:32],unit_price=price,tax_amount=tax,discount_amount=Decimal("0"),line_total=total))
    db.session.commit()
    return jsonify({"purchase":_purchase_dict(purchase),"message":"Purchase draft recorded. Stock has not changed."}),201

@org_tools_bp.patch("/inventory-purchases/<int:purchase_id>")
@roles_required("Admin","Supervisor")
def inventory_purchase_update(purchase_id):
    user=get_current_user(); purchase=InventoryPurchase.query.filter_by(id=purchase_id,organization_id=user.organization_id).first()
    if not purchase:return jsonify({"error":"Purchase not found."}),404
    if user.role=="Supervisor" and purchase.created_by_id!=user.id:return jsonify({"error":"You can only manage purchases you created."}),403
    d=request.get_json(silent=True) or {}; action=d.get("action")
    if action=="verify_details":
        if purchase.status!="draft":return jsonify({"error":"Only draft purchases can be edited."}),409
        supplier_name=str(d.get("supplier_name") or "").strip()
        invoice_number=str(d.get("invoice_number") or "").strip()
        if not supplier_name:return jsonify({"error":"Supplier name is required."}),400
        if invoice_number:
            duplicate=InventoryPurchase.query.filter(InventoryPurchase.organization_id==user.organization_id,
                InventoryPurchase.invoice_number==invoice_number,InventoryPurchase.id!=purchase.id).first()
            if duplicate:return jsonify({"error":"This invoice number is already recorded in this organisation."}),409
        supplier=None
        if d.get("supplier_id"):
            supplier=InventorySupplier.query.filter_by(id=d["supplier_id"],organization_id=user.organization_id).first()
            if not supplier:return jsonify({"error":"Supplier does not belong to this organisation."}),400
        raw_lines=d.get("lines")
        if not isinstance(raw_lines,list) or not raw_lines:return jsonify({"error":"Add at least one verified invoice line."}),400
        parsed=[]; subtotal=Decimal("0"); tax_total=Decimal("0")
        try:
            for line in raw_lines[:100]:
                desc=str(line.get("description") or "").strip()
                qty=Decimal(str(line.get("quantity"))); price=Decimal(str(line.get("unit_price",0))); tax=Decimal(str(line.get("tax_amount",0)))
                item_id=line.get("item_id")
                if not desc or qty<=0 or price<0 or tax<0:raise InvalidOperation()
                if item_id:
                    linked=InventoryItem.query.filter_by(id=item_id,organization_id=user.organization_id).first()
                    if not linked:return jsonify({"error":"A linked item is outside this organisation."}),400
                    if user.role=="Supervisor" and linked.team_id is not None and linked.team_id not in _team_ids(user) and linked.assigned_to_id!=user.id:return jsonify({"error":"A linked item is outside your team scope."}),403
                total=qty*price+tax;subtotal+=qty*price;tax_total+=tax
                parsed.append((line,desc,qty,price,tax,total,item_id))
        except (InvalidOperation,ValueError,TypeError):
            return jsonify({"error":"Check invoice line descriptions, quantities, prices and taxes."}),400
        InventoryPurchaseLine.query.filter_by(purchase_id=purchase.id,organization_id=user.organization_id).delete()
        if supplier is None:
            supplier=InventorySupplier.query.filter_by(organization_id=user.organization_id,name=supplier_name).first()
        if supplier is None:
            supplier=InventorySupplier(organization_id=user.organization_id,name=supplier_name,created_by_id=user.id)
            db.session.add(supplier);db.session.flush()
        purchase.supplier_id=supplier.id
        purchase.invoice_number=invoice_number or None
        try:purchase.invoice_date=datetime.strptime(d["invoice_date"],"%Y-%m-%d").date() if d.get("invoice_date") else None
        except ValueError:return jsonify({"error":"Invalid invoice date."}),400
        purchase.purchase_order_number=str(d.get("order_reference") or "")[:100]
        purchase.currency=str(d.get("currency") or "INR")[:8]
        purchase.subtotal=subtotal;purchase.tax_total=tax_total;purchase.grand_total=subtotal+tax_total
        purchase.notes=str(d.get("notes") or "")[:8000]
        purchase.extraction_status="human_verified"
        for line,desc,qty,price,tax,total,item_id in parsed:
            db.session.add(InventoryPurchaseLine(organization_id=user.organization_id,purchase_id=purchase.id,item_id=item_id,
                item_name=desc,sku=str(line.get("sku") or "")[:80],quantity_ordered=qty,quantity_received=Decimal("0"),
                unit=str(line.get("unit") or "units")[:32],unit_price=price,tax_amount=tax,discount_amount=Decimal("0"),line_total=total))
        db.session.commit()
        return jsonify({"purchase":_purchase_dict(purchase),"message":"Invoice details saved for review. Stock remains unchanged."}),200
    if action=="approve":
        if purchase.status!="draft":return jsonify({"error":"Only draft purchases can be approved."}),409
        if purchase.extraction_status!="human_verified" or not InventoryPurchaseLine.query.filter_by(purchase_id=purchase.id,organization_id=user.organization_id).first():return jsonify({"error":"Verify supplier and invoice line details before approval."}),409
        purchase.status="approved";purchase.reviewed_by_id=user.id;purchase.reviewed_at=datetime.now(timezone.utc)
    elif action=="receive":
        if purchase.status not in {"approved","partially_received"}:return jsonify({"error":"Approve the purchase before receiving goods."}),409
        lines=InventoryPurchaseLine.query.filter_by(purchase_id=purchase.id,organization_id=user.organization_id).with_for_update().all()
        received=d.get("received_quantities",{})
        any_received=False
        for line in lines:
            try: target=Decimal(str(received.get(str(line.id),line.quantity_ordered)))
            except (InvalidOperation,ValueError):return jsonify({"error":"Invalid received quantity."}),400
            already=Decimal(str(line.quantity_received or 0))
            if target<already or target>Decimal(str(line.quantity_ordered)):return jsonify({"error":"Received quantities must be between the quantity already received and the ordered quantity."}),400
            delta=target-already
            if delta<=0:continue
            any_received=True
            if line.item_id:
                item=InventoryItem.query.filter_by(id=line.item_id,organization_id=user.organization_id).with_for_update().first()
                if not item:return jsonify({"error":"Linked inventory item no longer exists."}),409
            else:
                item=InventoryItem.query.filter_by(organization_id=user.organization_id,sku=line.sku).first() if line.sku else None
                if not item:
                    item=InventoryItem(organization_id=user.organization_id,name=line.item_name,category="General",sku=line.sku or None,
                        quantity=Decimal("0"),minimum_quantity=Decimal("0"),unit=line.unit,created_by_id=user.id)
                    db.session.add(item);db.session.flush()
                line.item_id=item.id
            current_total=Decimal(str(item.quantity or 0)); projected=current_total+delta
            maximum=Decimal(str(getattr(item,"maximum_quantity",0) or 0))
            if maximum>0 and projected>maximum and not d.get("allow_over_maximum"):
                db.session.rollback();return jsonify({"error":"Receipt exceeds maximum stock threshold.","maximum_quantity":float(maximum),"projected_quantity":float(projected),"requires_override":True}),409
            if maximum>0 and projected>maximum and not str(d.get("override_reason") or "").strip():
                db.session.rollback();return jsonify({"error":"Enter a reason to override the maximum stock threshold."}),400
            _location_balance(user.id,user.organization_id,item.id,str(d.get("location") or item.storage_location or "Unassigned / central stock"),delta)
            item.quantity=projected
            db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="receipt",
                quantity=delta,balance_after=item.quantity,reference=purchase.invoice_number or ("Purchase #"+str(purchase.id)),
                notes="Goods received against purchase #"+str(purchase.id),performed_by_id=user.id))
            line.quantity_received=target
        if not any_received:return jsonify({"error":"Enter at least one quantity greater than the amount already received."}),400
        all_lines=InventoryPurchaseLine.query.filter_by(purchase_id=purchase.id,organization_id=user.organization_id).all()
        purchase.status="received" if all(Decimal(str(x.quantity_received or 0))>=Decimal(str(x.quantity_ordered)) for x in all_lines) else "partially_received"
        purchase.received_by_id=user.id
        if purchase.status=="received":purchase.received_at=datetime.now(timezone.utc)
    elif action=="cancel":
        if purchase.status in {"received","partially_received"}:return jsonify({"error":"A purchase with received stock cannot be cancelled; use a return/adjustment workflow."}),409
        purchase.status="cancelled"
    else:return jsonify({"error":"Choose approve, receive or cancel."}),400
    db.session.commit()
    return jsonify({"purchase":_purchase_dict(purchase),"message":"Purchase updated."}),200

@org_tools_bp.get("/inventory-purchases/<int:purchase_id>/invoice")
@roles_required("Admin","Supervisor")
def inventory_purchase_invoice(purchase_id):
    user=get_current_user(); purchase=InventoryPurchase.query.filter_by(id=purchase_id,organization_id=user.organization_id).first()
    if not purchase:return jsonify({"error":"Invoice not found."}),404
    if user.role=="Supervisor" and purchase.created_by_id!=user.id:return jsonify({"error":"You cannot access this invoice."}),403
    if not purchase.invoice_data:return jsonify({"error":"No invoice file is attached."}),404
    from io import BytesIO
    return send_file(BytesIO(purchase.invoice_data),mimetype=purchase.invoice_content_type or "application/octet-stream",
        as_attachment=True,download_name=purchase.invoice_filename or "invoice")


def _extract_invoice_text(data, mimetype):
    text_content = ""
    method = "manual_review"
    if mimetype == "application/pdf":
        try:
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(data))
            text_content = "\\n".join((page.extract_text() or "") for page in reader.pages[:20])
            if text_content.strip(): method = "pdf_text"
        except Exception:
            text_content = ""
    elif mimetype.startswith("image/"):
        try:
            from PIL import Image
            import pytesseract
            from io import BytesIO
            text_content = pytesseract.image_to_string(Image.open(BytesIO(data)))[:60000]
            if text_content.strip(): method = "local_ocr"
        except Exception:
            text_content = ""
    return text_content[:60000], method


def _parse_invoice_rules(raw):
    import re
    clean = re.sub(r"[ \\t]+", " ", raw or "")
    def find(pattern):
        match = re.search(pattern, clean, re.I | re.M)
        return match.group(1).strip() if match else ""
    invoice = find(r"(?:invoice\\s*(?:no|number|#)?|bill\\s*no)\\s*[:#-]?\\s*([A-Z0-9/-]{3,40})")
    date = find(r"(?:invoice\\s*date|date)\\s*[:#-]?\\s*(\\d{1,2}[/-]\\d{1,2}[/-]\\d{2,4})")
    supplier = ""
    for line in clean.splitlines():
        candidate = line.strip()
        if candidate and len(candidate) > 2 and not re.search(r"invoice|tax|gst|date|total|address|phone", candidate, re.I):
            supplier = candidate[:180]; break
    totals = find(r"(?:grand\\s*total|total\\s*amount|amount\\s*due)\\s*[:₹$ ]+([0-9,]+\\.\\d{1,2})")
    return {"supplier_name":supplier,"invoice_number":invoice,"invoice_date_raw":date,"grand_total_raw":totals,"lines":[]}


@org_tools_bp.post("/inventory-purchases/invoice-upload")
@roles_required("Admin","Supervisor")
def inventory_purchase_invoice_upload():
    user=get_current_user(); uploaded=request.files.get("invoice")
    if not uploaded:return jsonify({"error":"Choose an invoice file."}),400
    filename=str(uploaded.filename or "invoice").replace("\\","/").split("/")[-1][:255]
    mimetype=(uploaded.mimetype or "").lower()
    allowed={"application/pdf","image/jpeg","image/png","image/webp"}
    if mimetype not in allowed:return jsonify({"error":"Upload a PDF, JPG, PNG or WEBP invoice."}),415
    data=uploaded.read(8*1024*1024+1)
    if not data or len(data)>8*1024*1024:return jsonify({"error":"Invoice must be between 1 byte and 8 MB."}),413
    purchase=InventoryPurchase(organization_id=user.organization_id,supplier_name="Pending verification",
        status="draft",invoice_filename=filename,invoice_content_type=mimetype,invoice_size=len(data),invoice_data=data,
        extraction_status="awaiting_review",notes="Invoice uploaded. Verify supplier, invoice details and line items before approval.",
        created_by_id=user.id)
    db.session.add(purchase);db.session.commit()
    return jsonify({"purchase":_purchase_dict(purchase),
        "message":"Invoice securely attached to a draft purchase. Enter or verify its extracted details before approval.",
        "extraction":{"status":"not_configured","message":"Automated OCR/AI extraction requires an OCR/AI provider configuration. No invoice values have been guessed."}}),201

@org_tools_bp.get("/inventory-transfers")
@roles_required("Admin","Supervisor")
def inventory_transfers_list():
    user=get_current_user()
    q=InventoryTransfer.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user)
        q=q.join(InventoryItem,InventoryTransfer.item_id==InventoryItem.id).filter(
            or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None))
            if ids else or_(InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None)))
    rows=q.order_by(InventoryTransfer.created_at.desc()).limit(300).all()
    return jsonify({"transfers":[{"id":x.id,"item_id":x.item_id,"from_location":x.from_location,"to_location":x.to_location,
        "quantity":float(x.quantity),"status":x.status,"notes":x.notes or "","created_at":x.created_at.isoformat()} for x in rows]}),200

@org_tools_bp.post("/inventory-transfers")
@roles_required("Admin","Supervisor")
def inventory_transfer_create():
    user=get_current_user(); d=request.get_json(silent=True) or {}
    item=InventoryItem.query.filter_by(id=d.get("item_id"),organization_id=user.organization_id).first()
    if not item:return jsonify({"error":"Choose an inventory item in this organisation."}),400
    if user.role=="Supervisor" and item.team_id is not None and item.team_id not in _team_ids(user) and item.assigned_to_id!=user.id:return jsonify({"error":"This item is outside your team scope."}),403
    source=str(d.get("from_location") or "").strip(); target=str(d.get("to_location") or "").strip()
    try:qty=Decimal(str(d.get("quantity")))
    except (InvalidOperation,ValueError):return jsonify({"error":"Enter a valid transfer quantity."}),400
    if not source or not target or source==target or qty<=0:return jsonify({"error":"Choose different source and destination locations and a positive quantity."}),400
    row=InventoryTransfer(organization_id=user.organization_id,item_id=item.id,from_location=source,to_location=target,
        quantity=qty,requested_by_id=user.id,notes=str(d.get("notes") or "")[:4000])
    db.session.add(row);db.session.commit()
    return jsonify({"id":row.id,"status":row.status,"message":"Transfer request created. Stock has not moved yet."}),201

@org_tools_bp.patch("/inventory-transfers/<int:transfer_id>")
@roles_required("Admin","Supervisor")
def inventory_transfer_update(transfer_id):
    user=get_current_user(); row=InventoryTransfer.query.filter_by(id=transfer_id,organization_id=user.organization_id).first()
    if not row:return jsonify({"error":"Transfer not found."}),404
    d=request.get_json(silent=True) or {}; action=d.get("action")
    item=InventoryItem.query.filter_by(id=row.item_id,organization_id=user.organization_id).with_for_update().first()
    if not item:return jsonify({"error":"Inventory item not found."}),404
    if user.role=="Supervisor" and item.team_id is not None and item.team_id not in _team_ids(user) and item.assigned_to_id!=user.id:return jsonify({"error":"This item is outside your team scope."}),403
    if action=="dispatch" and row.status=="pending":
        current=Decimal(str(item.quantity or 0)); qty=Decimal(str(row.quantity))
        if current<qty:return jsonify({"error":"Insufficient stock to dispatch this transfer."}),409
        try: _location_balance(user.id,user.organization_id,item.id,row.from_location,-qty)
        except ValueError as exc: db.session.rollback();return jsonify({"error":str(exc)}),409
        item.quantity=current-qty;row.status="in_transit";row.dispatched_by_id=user.id;row.dispatched_at=datetime.now(timezone.utc)
        db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="transfer_out",quantity=qty,balance_after=item.quantity,reference="Transfer #"+str(row.id),notes=row.from_location+" → "+row.to_location,performed_by_id=user.id))
    elif action=="receive" and row.status=="in_transit":
        qty=Decimal(str(row.quantity))
        _location_balance(user.id,user.organization_id,item.id,row.to_location,qty)
        item.quantity=Decimal(str(item.quantity or 0))+qty;row.status="received";row.received_by_id=user.id;row.received_at=datetime.now(timezone.utc)
        db.session.add(InventoryMovement(organization_id=user.organization_id,item_id=item.id,movement_type="transfer_in",quantity=qty,balance_after=item.quantity,reference="Transfer #"+str(row.id),notes=row.from_location+" → "+row.to_location,performed_by_id=user.id))
    else:return jsonify({"error":"This transfer action is not valid for its current status."}),409
    db.session.commit()
    return jsonify({"message":"Transfer updated.","status":row.status}),200


@org_tools_bp.get("/inventory-locations")
@roles_required(*ALL_ROLES)
def inventory_location_balances():
    user=get_current_user()
    q=InventoryLocationStock.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user)
        q=q.join(InventoryItem,InventoryItem.id==InventoryLocationStock.item_id).filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None))) if ids else q.join(InventoryItem).filter(or_(InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None)))
    elif user.role=="Staff":
        ids=_team_ids(user)
        q=q.join(InventoryItem).filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id)) if ids else q.join(InventoryItem).filter(InventoryItem.assigned_to_id==user.id)
    rows=q.order_by(InventoryLocationStock.location_name.asc()).all()
    return jsonify({"locations":[{"id":x.id,"item_id":x.item_id,"location":x.location_name,"quantity":float(x.quantity or 0)} for x in rows]}),200


@org_tools_bp.get("/inventory-analytics")
@roles_required(*ALL_ROLES)
def inventory_analytics():
    user=get_current_user()
    q=InventoryItem.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None))) if ids else q.filter(or_(InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None)))
    elif user.role=="Staff":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id)) if ids else q.filter(InventoryItem.assigned_to_id==user.id)
    items=q.all(); ids=[x.id for x in items]
    movements=InventoryMovement.query.filter(InventoryMovement.organization_id==user.organization_id,InventoryMovement.item_id.in_(ids or [-1])).order_by(InventoryMovement.created_at.desc()).limit(3000).all()
    monthly={}
    for mv in movements:
        key=mv.created_at.strftime("%Y-%m")
        bucket=monthly.setdefault(key,{"receipts":0.0,"consumption":0.0,"adjustments":0.0})
        amount=float(mv.quantity or 0)
        if mv.movement_type in {"receipt","transfer_in"}: bucket["receipts"]+=amount
        elif mv.movement_type in {"consumption","transfer_out"}: bucket["consumption"]+=amount
        elif mv.movement_type=="adjustment": bucket["adjustments"]+=amount
    return jsonify({"summary":{"items":len(items),"on_hand":sum(float(x.quantity or 0) for x in items),"low_stock":sum(1 for x in items if float(x.quantity or 0)<=float(x.minimum_quantity or 0)),"over_maximum":sum(1 for x in items if float(getattr(x,"maximum_quantity",0) or 0)>0 and float(x.quantity or 0)>float(x.maximum_quantity))},"monthly":[{"month":k,**v} for k,v in sorted(monthly.items())],"data_points":len(movements)}),200


@org_tools_bp.get("/inventory-forecast")
@roles_required("Admin","Supervisor")
def inventory_forecast():
    user=get_current_user(); days=request.args.get("days",type=int) or 30
    if days not in {7,14,30,60,90}: return jsonify({"error":"Choose a 7, 14, 30, 60 or 90 day forecast horizon."}),400
    q=InventoryItem.query.filter_by(organization_id=user.organization_id)
    if user.role=="Supervisor":
        ids=_team_ids(user); q=q.filter(or_(InventoryItem.team_id.in_(ids),InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None))) if ids else q.filter(or_(InventoryItem.assigned_to_id==user.id,InventoryItem.team_id.is_(None)))
    results=[]; since=datetime.now(timezone.utc)-timedelta(days=90)
    for item in q.all():
        rows=InventoryMovement.query.filter_by(organization_id=user.organization_id,item_id=item.id,movement_type="consumption").filter(InventoryMovement.created_at>=since).all()
        if len(rows)<3: continue
        first=min(x.created_at for x in rows); active_days=max(1,min(90,(datetime.now(timezone.utc)-first).days+1))
        if active_days<14: continue
        daily=sum(float(x.quantity or 0) for x in rows)/active_days
        expected=daily*days; on_hand=float(item.quantity or 0)
        results.append({"item_id":item.id,"item_name":item.name,"unit":item.unit,"on_hand":on_hand,"average_daily_consumption":round(daily,3),"forecast_consumption":round(expected,2),"projected_balance":round(on_hand-expected,2),"suggested_replenishment":round(max(0,expected+float(item.minimum_quantity or 0)-on_hand),2),"observations":len(rows),"history_days":active_days,"confidence":"indicative"})
    return jsonify({"days":days,"method":"Trailing observed consumption average; no external AI or invented data.","minimum_observations":3,"forecasts":results}),200
