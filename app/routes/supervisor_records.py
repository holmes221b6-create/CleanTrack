from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from sqlalchemy import or_
from app.extensions import db
from app.models.operational_records import OperationalIncident, ShiftHandover, QualityInspection
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.operation import Operation
from app.models.location import Location
from app.models.zone import Zone
from app.routes.operations import _scoped_operation_query
from app.services.permissions import get_current_user, roles_required
supervisor_records_bp=Blueprint("supervisor_records",__name__,url_prefix="/api/supervisor")
def _team(user):
 ids={r[0] for r in db.session.query(TeamMember.team_id).join(Team,Team.id==TeamMember.team_id).filter(TeamMember.user_id==user.id,Team.organization_id==user.organization_id,Team.status=="active").all()}
 if len(ids)!=1:return None,(jsonify({"error":"Your account must belong to exactly one active team."}),409)
 return Team.query.filter_by(id=next(iter(ids)),organization_id=user.organization_id,status="active").first(),None
def _dt(value,field):
 if not value:return None
 try:d=datetime.fromisoformat(str(value).replace("Z","+00:00"))
 except ValueError:raise ValueError(field+" must be a valid date and time.")
 return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
def _accessible_locations(user,team):
 zone_location_ids=db.session.query(Zone.location_id).filter(Zone.organization_id==user.organization_id,or_(Zone.team_id==team.id,Zone.supervisor_id==user.id))
 return Location.query.filter(Location.organization_id==user.organization_id,or_(Location.team_id==team.id,Location.id.in_(zone_location_ids)))
def _accessible_zones(user,team,location_ids=None):
 rules=[Zone.team_id==team.id,Zone.supervisor_id==user.id]
 if location_ids is not None: rules.append(Zone.location_id.in_(location_ids))
 return Zone.query.filter(Zone.organization_id==user.organization_id,or_(*rules))
def _refs(user,team,data):
 op_id=data.get("operation_id") or None; loc_id=data.get("location_id") or None; zone_id=data.get("zone_id") or None
 op=_scoped_operation_query(user).filter(Operation.id==op_id).first() if op_id else None
 if op_id and op is None:raise ValueError("The selected operation is outside your team access.")
 loc=_accessible_locations(user,team).filter(Location.id==loc_id).first() if loc_id else None
 if loc_id and loc is None:raise ValueError("The selected location is outside your team access.")
 location_ids=[loc.id] if loc else None
 zone=_accessible_zones(user,team,location_ids).filter(Zone.id==zone_id).first() if zone_id else None
 if zone_id and zone is None:raise ValueError("The selected zone is outside your team access.")
 if zone and loc and zone.location_id!=loc.id:raise ValueError("The selected zone does not belong to that location.")
 return op,loc,zone
@supervisor_records_bp.get("/records/options")
@roles_required("Supervisor")
def records_options():
 u=get_current_user();team,err=_team(u)
 if err:return err
 ops=_scoped_operation_query(u).order_by(Operation.created_at.desc()).limit(200).all()
 locs=Location.query.filter_by(organization_id=u.organization_id).order_by(Location.name.asc()).all()
 zones=Zone.query.filter_by(organization_id=u.organization_id).order_by(Zone.name.asc()).all()
 return jsonify({"operations":[{"id":x.id,"title":x.title} for x in ops],"locations":[{"id":x.id,"name":x.name} for x in locs],"zones":[{"id":x.id,"name":x.name,"location_id":x.location_id} for x in zones]}),200
@supervisor_records_bp.route("/incidents",methods=["GET","POST"])
@roles_required("Supervisor")
def incidents():
 u=get_current_user();team,err=_team(u)
 if err:return err
 if request.method=="GET":
  rows=OperationalIncident.query.filter_by(organization_id=u.organization_id,team_id=team.id).order_by(OperationalIncident.created_at.desc()).limit(300).all()
  return jsonify({"items":[x.to_dict() for x in rows]}),200
 d=request.get_json(silent=True) or {};title=str(d.get("title") or "").strip();desc=str(d.get("description") or "").strip()
 if not title or not desc:return jsonify({"error":"A title and description are required."}),400
 severity=str(d.get("severity") or "medium").lower();category=str(d.get("category") or "other").lower()
 if severity not in {"low","medium","high","critical"} or category not in {"equipment","safety","staffing","resource","facility","other"}:return jsonify({"error":"Choose a valid incident category and severity."}),400
 try:op,loc,zone=_refs(u,team,d)
 except ValueError as e:return jsonify({"error":str(e)}),400
 x=OperationalIncident(organization_id=u.organization_id,team_id=team.id,reported_by_id=u.id,operation_id=op.id if op else None,location_id=loc.id if loc else None,zone_id=zone.id if zone else None,title=title[:180],description=desc,category=category,severity=severity)
 db.session.add(x);db.session.commit();return jsonify({"item":x.to_dict()}),201
@supervisor_records_bp.patch("/incidents/<int:item_id>")
@roles_required("Supervisor")
def update_incident(item_id):
 u=get_current_user();team,err=_team(u)
 if err:return err
 x=OperationalIncident.query.filter_by(id=item_id,organization_id=u.organization_id,team_id=team.id).first_or_404();d=request.get_json(silent=True) or {};status=str(d.get("status") or x.status).lower()
 if status not in {"open","in_progress","resolved","closed"}:return jsonify({"error":"Invalid incident status."}),400
 resolution=str(d.get("resolution") or "").strip()
 if status in {"resolved","closed"} and not resolution and not x.resolution:return jsonify({"error":"Add a resolution note before closing an incident."}),400
 x.status=status;x.resolution=resolution or x.resolution;x.resolved_at=datetime.now(timezone.utc) if status in {"resolved","closed"} else None
 db.session.commit();return jsonify({"item":x.to_dict()}),200
@supervisor_records_bp.route("/handovers",methods=["GET","POST"])
@roles_required("Supervisor")
def handovers():
 u=get_current_user();team,err=_team(u)
 if err:return err
 if request.method=="GET":
  rows=ShiftHandover.query.filter_by(organization_id=u.organization_id,team_id=team.id).order_by(ShiftHandover.created_at.desc()).limit(200).all()
  return jsonify({"items":[x.to_dict() for x in rows]}),200
 d=request.get_json(silent=True) or {};label=str(d.get("shift_label") or "").strip();summary=str(d.get("summary") or "").strip()
 if not label or not summary:return jsonify({"error":"Shift name and handover summary are required."}),400
 try:out=_dt(d.get("outgoing_at"),"Outgoing time");inc=_dt(d.get("incoming_at"),"Incoming time")
 except ValueError as e:return jsonify({"error":str(e)}),400
 if out is None:return jsonify({"error":"Outgoing shift time is required."}),400
 items=d.get("outstanding_items") or []
 if not isinstance(items,list) or len(items)>100 or any(not isinstance(x,str) for x in items):return jsonify({"error":"Outstanding items must be a list of up to 100 text entries."}),400
 x=ShiftHandover(organization_id=u.organization_id,team_id=team.id,created_by_id=u.id,shift_label=label[:100],outgoing_at=out,incoming_at=inc,summary=summary,outstanding_items=[v.strip()[:500] for v in items if v.strip()],status="published" if d.get("publish") else "draft")
 db.session.add(x);db.session.commit();return jsonify({"item":x.to_dict()}),201
@supervisor_records_bp.post("/handovers/<int:item_id>/acknowledge")
@roles_required("Supervisor")
def acknowledge_handover(item_id):
 u=get_current_user();team,err=_team(u)
 if err:return err
 x=ShiftHandover.query.filter_by(id=item_id,organization_id=u.organization_id,team_id=team.id).first_or_404()
 if x.status!="published":return jsonify({"error":"Only published handovers can be acknowledged."}),409
 if x.created_by_id==u.id:return jsonify({"error":"The handover creator cannot acknowledge their own handover."}),403
 x.status="acknowledged";x.acknowledged_by_id=u.id;x.acknowledged_at=datetime.now(timezone.utc);db.session.commit()
 return jsonify({"item":x.to_dict()}),200
@supervisor_records_bp.route("/inspections",methods=["GET","POST"])
@roles_required("Supervisor")
def inspections():
 u=get_current_user();team,err=_team(u)
 if err:return err
 if request.method=="GET":
  rows=QualityInspection.query.filter_by(organization_id=u.organization_id,team_id=team.id).order_by(QualityInspection.created_at.desc()).limit(300).all()
  return jsonify({"items":[x.to_dict() for x in rows]}),200
 d=request.get_json(silent=True) or {};title=str(d.get("title") or "").strip();checks=d.get("checklist") or []
 if not title or not isinstance(checks,list) or not checks or len(checks)>60 or any(not isinstance(v,str) or not v.strip() for v in checks):return jsonify({"error":"Provide an inspection title and between 1 and 60 checklist points."}),400
 try:op,loc,zone=_refs(u,d)
 except ValueError as e:return jsonify({"error":str(e)}),400
 x=QualityInspection(organization_id=u.organization_id,team_id=team.id,created_by_id=u.id,operation_id=op.id if op else None,location_id=loc.id if loc else None,zone_id=zone.id if zone else None,title=title[:180],checklist=[{"label":v.strip()[:240],"checked":False} for v in checks],status="planned")
 db.session.add(x);db.session.commit();return jsonify({"item":x.to_dict()}),201
@supervisor_records_bp.patch("/inspections/<int:item_id>")
@roles_required("Supervisor")
def complete_inspection(item_id):
 u=get_current_user();team,err=_team(u)
 if err:return err
 x=QualityInspection.query.filter_by(id=item_id,organization_id=u.organization_id,team_id=team.id).first_or_404();d=request.get_json(silent=True) or {};status=str(d.get("status") or x.status).lower()
 if status not in {"in_progress","completed"}:return jsonify({"error":"Invalid inspection status."}),400
 if status=="completed":
  outcome=str(d.get("outcome") or "").lower();checks=d.get("checklist",x.checklist)
  if outcome not in {"pass","fail"}:return jsonify({"error":"Choose a pass or fail outcome."}),400
  if not isinstance(checks,list) or len(checks)!=len(x.checklist) or any(not isinstance(v,dict) or not isinstance(v.get("checked"),bool) for v in checks):return jsonify({"error":"Inspection checklist data is invalid."}),400
  if outcome=="pass" and not all(v["checked"] for v in checks):return jsonify({"error":"All checklist points must pass before marking the inspection as passed."}),400
  x.checklist=checks;x.outcome=outcome;x.findings=str(d.get("findings") or "").strip()[:4000];x.inspected_at=datetime.now(timezone.utc)
 x.status=status;db.session.commit();return jsonify({"item":x.to_dict()}),200
