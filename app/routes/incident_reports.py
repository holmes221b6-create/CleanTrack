from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.incident_report import IncidentReport, IncidentReportEvent
from app.models.notification import Notification
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.operation import Operation
from app.models.location import Location
from app.models.zone import Zone
from app.services.permissions import get_current_user, roles_required

incident_reports_bp = Blueprint("incident_reports", __name__, url_prefix="/api")

def _now():
    return datetime.now(timezone.utc)

def _memberships(user):
    return (db.session.query(TeamMember, Team).join(Team, Team.id == TeamMember.team_id)
            .filter(TeamMember.user_id == user.id, Team.organization_id == user.organization_id,
                    Team.status == "active").all())

def _notify(user_id, org_id, title, message, report_id, kind):
    db.session.add(Notification(recipient_id=user_id, organization_id=org_id, title=title,
        message=message, notification_type=kind, resource_type="incident_report", resource_id=str(report_id)))

def _event(report, actor, event_type, message):
    db.session.add(IncidentReportEvent(report_id=report.id, organization_id=report.organization_id,
        actor_id=actor.id, event_type=event_type, message=message))

def _query(user):
    return IncidentReport.query.filter_by(organization_id=user.organization_id)

def _optional_ref(value, model, user, team_id, label):
    if value in (None, ""):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("Select a valid " + label + ".")
    q = model.query.filter_by(id=value, organization_id=user.organization_id)
    if model in (Operation, Location, Zone):
        q = q.filter(model.team_id == team_id)
    row = q.first()
    if row is None:
        raise ValueError("The selected " + label + " is outside your team access.")
    return row

@incident_reports_bp.post("/staff/incident-reports")
@roles_required("Staff")
def create_staff_report():
    user = get_current_user()
    memberships = _memberships(user)
    if len(memberships) != 1:
        return jsonify({"error": "Your account must belong to exactly one active team."}), 409
    _, team = memberships[0]
    supervisors = (User.query.join(TeamMember, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team.id, User.organization_id == user.organization_id,
                User.role == "Supervisor", User.account_status == "active").all())
    if len(supervisors) != 1:
        return jsonify({"error": "Your team must have exactly one active Supervisor assigned before reports can be submitted."}), 409
    d = request.get_json(silent=True) or {}
    title, description = str(d.get("title") or "").strip(), str(d.get("description") or "").strip()
    kind, category, severity = (str(d.get("report_type") or "issue").lower(),
        str(d.get("category") or "other").lower(), str(d.get("severity") or "medium").lower())
    if not title or not description:
        return jsonify({"error": "A title and description are required."}), 400
    if kind not in {"incident", "issue"} or category not in {"equipment", "safety", "staffing", "resource", "facility", "operational", "other"} or severity not in {"low", "medium", "high", "critical"}:
        return jsonify({"error": "Choose a valid report type, category and severity."}), 400
    try:
        operation = _optional_ref(d.get("operation_id"), Operation, user, team.id, "operation")
        location = _optional_ref(d.get("location_id"), Location, user, team.id, "location")
        zone = _optional_ref(d.get("zone_id"), Zone, user, team.id, "zone")
        if zone and location and zone.location_id != location.id:
            raise ValueError("The selected zone does not belong to that location.")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    report = IncidentReport(organization_id=user.organization_id, team_id=team.id,
        reported_by_id=user.id, supervisor_id=supervisors[0].id,
        operation_id=operation.id if operation else None, location_id=location.id if location else None,
        zone_id=zone.id if zone else None, title=title[:180], description=description,
        report_type=kind, category=category, severity=severity, status="reported")
    db.session.add(report)
    db.session.flush()
    _event(report, user, "reported", "Staff submitted this " + kind + " to the Team Supervisor.")
    _notify(supervisors[0].id, user.organization_id, "New staff " + kind,
        user.full_name + " reported: " + title, report.id, "incident_report")
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "The report could not be saved."}), 500
    return jsonify({"item": report.to_dict()}), 201

@incident_reports_bp.get("/supervisor/incident-reports")
@roles_required("Supervisor")
def supervisor_staff_reports():
    user = get_current_user()
    memberships = _memberships(user)
    team_ids = [team.id for _, team in memberships]
    if len(team_ids) != 1:
        return jsonify({"error": "Your account must belong to exactly one active team."}), 409
    rows = _query(user).filter(IncidentReport.team_id == team_ids[0],
        IncidentReport.supervisor_id == user.id,
        IncidentReport.supervisor_acknowledged_at.is_(None)
    ).order_by(IncidentReport.created_at.desc()).limit(300).all()
    return jsonify({"items": [row.to_dict() for row in rows]}), 200

@incident_reports_bp.get("/supervisor/incident-reports/history")
@roles_required("Supervisor")
def supervisor_report_history():
    user = get_current_user()
    memberships = _memberships(user)
    team_ids = [team.id for _, team in memberships]
    if len(team_ids) != 1:
        return jsonify({"error": "Your account must belong to exactly one active team."}), 409
    cutoff = _now() - timedelta(days=7)
    rows = _query(user).filter(IncidentReport.team_id == team_ids[0],
        IncidentReport.supervisor_id == user.id, IncidentReport.created_at >= cutoff
    ).order_by(IncidentReport.created_at.desc()).limit(500).all()
    return jsonify({"items": [row.to_dict() for row in rows]}), 200

@incident_reports_bp.get("/supervisor/incident-reports/<int:report_id>/events")
@roles_required("Supervisor")
def supervisor_report_events(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id, supervisor_id=user.id).first_or_404()
    events = IncidentReportEvent.query.filter_by(report_id=report.id, organization_id=user.organization_id).order_by(IncidentReportEvent.created_at.asc()).all()
    return jsonify({"events": [event.to_dict() for event in events]}), 200

@incident_reports_bp.post("/supervisor/incident-reports/<int:report_id>/acknowledge")
@roles_required("Supervisor")
def supervisor_acknowledge_resolution(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id, supervisor_id=user.id).first_or_404()
    if not report.staff_resolved_at:
        return jsonify({"error": "The Staff member has not submitted a resolution yet."}), 409
    if report.forwarded_at and not report.admin_resolution_acknowledged_at:
        return jsonify({"error": "Admin must acknowledge the forwarded resolution update first."}), 409
    if not report.supervisor_acknowledged_at:
        report.supervisor_acknowledged_at = _now()
        report.status = "resolved"
        _event(report, user, "supervisor_acknowledged", "Supervisor acknowledged the Staff resolution.")
        db.session.commit()
    return jsonify({"item": report.to_dict()}), 200

@incident_reports_bp.post("/supervisor/incident-reports/<int:report_id>/forward")
@roles_required("Supervisor")
def supervisor_forward_report(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id, supervisor_id=user.id).first_or_404()
    if report.forwarded_at:
        return jsonify({"error": "This report has already been forwarded to Admin."}), 409
    note = str((request.get_json(silent=True) or {}).get("supervisor_note") or "").strip()
    if not note:
        return jsonify({"error": "Add a note explaining why this report is being forwarded."}), 400
    admins = User.query.filter_by(organization_id=user.organization_id, role="Admin", account_status="active").all()
    if not admins:
        return jsonify({"error": "No active Admin account is available in this organisation."}), 409
    report.forwarded_at, report.forwarded_by_id, report.supervisor_note = _now(), user.id, note[:4000]
    report.status = "forwarded_to_admin"
    _event(report, user, "forwarded_to_admin", "Supervisor forwarded the original Staff report: " + note[:3500])
    for admin in admins:
        _notify(admin.id, user.organization_id, "Staff report forwarded",
            "Supervisor " + user.full_name + " forwarded " + report.title + " (INC-" + f"{report.id:06d}" + ").",
            report.id, "incident_forwarded")
    db.session.commit()
    return jsonify({"item": report.to_dict()}), 200

@incident_reports_bp.post("/staff/incident-reports/<int:report_id>/resolve")
@roles_required("Staff")
def staff_resolve_report(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id, reported_by_id=user.id).first_or_404()
    note = str((request.get_json(silent=True) or {}).get("resolution_note") or "").strip()
    if not note:
        return jsonify({"error": "Add a resolution note before submitting."}), 400
    if report.staff_resolved_at:
        return jsonify({"error": "A resolution has already been submitted for this report."}), 409
    report.staff_resolution_note, report.staff_resolved_at = note[:4000], _now()
    report.status = "resolution_pending_supervisor" if not report.forwarded_at else "resolution_pending_admin"
    _event(report, user, "staff_resolved", "Staff reported the issue resolved: " + note[:3500])
    _notify(report.supervisor_id, user.organization_id, "Staff reported an issue resolved",
        user.full_name + " submitted a resolution for " + report.title + ".", report.id, "incident_resolution")
    if report.forwarded_at:
        admins = User.query.filter_by(organization_id=user.organization_id, role="Admin", account_status="active").all()
        for admin in admins:
            _notify(admin.id, user.organization_id, "Staff resolution update",
                user.full_name + " reported " + report.title + " resolved. Admin acknowledgement is required.",
                report.id, "incident_resolution")
    db.session.commit()
    return jsonify({"item": report.to_dict()}), 200

@incident_reports_bp.get("/admin/incident-reports")
@roles_required("Admin")
def admin_incident_inbox():
    user = get_current_user()
    rows = _query(user).filter(
        IncidentReport.forwarded_at.isnot(None),
        db.or_(
            IncidentReport.admin_acknowledged_at.is_(None),
            db.and_(
                IncidentReport.staff_resolved_at.isnot(None),
                IncidentReport.admin_resolution_acknowledged_at.is_(None)
            )
        )
    ).order_by(IncidentReport.updated_at.desc()).limit(300).all()
    return jsonify({"items": [row.to_dict() for row in rows]}), 200


@incident_reports_bp.get("/admin/incident-reports/<int:report_id>/events")
@roles_required("Admin")
def admin_incident_events(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id).first_or_404()
    if not report.forwarded_at:
        return jsonify({"error": "This report has not been forwarded to Admin."}), 404
    events = IncidentReportEvent.query.filter_by(
        report_id=report.id, organization_id=user.organization_id
    ).order_by(IncidentReportEvent.created_at.asc()).all()
    return jsonify({"events": [event.to_dict() for event in events]}), 200


@incident_reports_bp.post("/admin/incident-reports/<int:report_id>/acknowledge")
@roles_required("Admin")
def admin_acknowledge_report(report_id):
    user = get_current_user()
    report = _query(user).filter_by(id=report_id).first_or_404()
    if not report.forwarded_at:
        return jsonify({"error": "Only reports forwarded to Admin can be acknowledged here."}), 409
    if report.staff_resolved_at:
        if not report.admin_resolution_acknowledged_at:
            report.admin_resolution_acknowledged_at = _now()
            report.admin_resolution_acknowledged_by_id = user.id
            _event(report, user, "admin_resolution_acknowledged", "Admin acknowledged the Staff resolution update.")
            report.supervisor_acknowledged_at = _now()
            report.status = "resolved"
            _event(report, user, "supervisor_copy_closed", "Admin acknowledged the Staff resolution; Supervisor active copy is now closed.")
            db.session.commit()
    elif not report.admin_acknowledged_at:
        report.admin_acknowledged_at, report.admin_acknowledged_by_id = _now(), user.id
        report.status = "forwarded_acknowledged"
        _event(report, user, "admin_acknowledged", "Admin acknowledged the forwarded report.")
        db.session.commit()
    return jsonify({"item": report.to_dict()}), 200
