from datetime import datetime, timedelta, timezone
from io import StringIO, BytesIO
import csv
import re
import unicodedata

from flask import Blueprint, jsonify, request, make_response, send_file
from sqlalchemy import or_

from app.extensions import db
from app.models.operation import Operation
from app.models.operation_log import OperationLog
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.team import Team
from app.models.location import Location
from app.models.zone import Zone
from app.models.team_member import TeamMember
from app.models.report_artifact import ReportArtifact
from app.services.permissions import get_current_user, roles_required

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")
FORMATS = {"csv", "xlsx", "pdf"}
TYPES = {"operations", "logs", "staff", "teams", "locations", "zones", "performance", "approvals"}
MIME = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
ORG_QUOTA_BYTES = 100 * 1024 * 1024
RETENTION_DAYS = 30
MAX_ROWS = 10000


def _date_range(payload):
    today = datetime.now(timezone.utc).date()
    try:
        start = datetime.strptime(str(payload.get("start_date") or (today - timedelta(days=29)).isoformat()), "%Y-%m-%d").date()
        end = datetime.strptime(str(payload.get("end_date") or today.isoformat()), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("Dates must use YYYY-MM-DD.")
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    if (end - start).days > 3660:
        raise ValueError("The selected period cannot exceed 10 years.")
    return start, end


def _in_range(value, start, end):
    if not value:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) <= value < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


def _clean(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)


def _rows_for(admin, payload):
    kind = str(payload.get("report_type") or "operations").lower()
    if kind not in TYPES:
        raise ValueError("Choose a supported report data type.")
    start, end = _date_range(payload)
    org = admin.organization_id
    if not org:
        raise PermissionError("Your account is not linked to an organization.")

    team_id = _int_filter(payload, "team_id")
    employee_id = _int_filter(payload, "employee_id")
    supervisor_id = _int_filter(payload, "supervisor_id")
    location_id = _int_filter(payload, "location_id")
    zone_id = _int_filter(payload, "zone_id")
    status = str(payload.get("status") or "").strip().lower()
    operation_type = str(payload.get("operation_type") or "").strip().lower()

    if team_id and not Team.query.filter_by(id=team_id, organization_id=org).first():
        raise ValueError("Selected team is outside your organization.")
    if location_id and not Location.query.filter_by(id=location_id, organization_id=org).first():
        raise ValueError("Selected location is outside your organization.")
    if zone_id and not Zone.query.filter_by(id=zone_id, organization_id=org).first():
        raise ValueError("Selected zone is outside your organization.")
    if employee_id and not User.query.filter_by(id=employee_id, organization_id=org).first():
        raise ValueError("Selected employee is outside your organization.")
    if supervisor_id and not User.query.filter_by(id=supervisor_id, organization_id=org).first():
        raise ValueError("Selected supervisor is outside your organization.")

    headers, rows = [], []
    filters = {"report_type": kind, "start_date": start.isoformat(), "end_date": end.isoformat(),
               "team_id": team_id, "supervisor_id": supervisor_id, "employee_id": employee_id,
               "location_id": location_id, "zone_id": zone_id, "status": status or None,
               "operation_type": operation_type or None, "date_format": str(payload.get("date_format") or "iso")}

    if kind in {"operations", "performance", "approvals"}:
        query = Operation.query.filter(Operation.organization_id == org)
        if team_id: query = query.filter(Operation.team_id == team_id)
        if employee_id: query = query.filter(Operation.assigned_to_id == employee_id)
        if supervisor_id: query = query.filter(Operation.assigned_supervisor_id == supervisor_id)
        if location_id: query = query.filter(Operation.location_id == location_id)
        if zone_id: query = query.filter(Operation.zone_id == zone_id)
        if status == "overdue": query = query.filter(Operation.overdue.is_(True))\n        elif status: query = query.filter(db.func.lower(Operation.status) == status)
        if operation_type: query = query.filter(db.func.lower(Operation.task_type) == operation_type)
        query = query.filter(Operation.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                             Operation.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
        operations = query.order_by(Operation.created_at.desc(), Operation.id.desc()).limit(MAX_ROWS + 1).all()
        if kind == "approvals":
            headers = ["Operation ID", "Operation", "Status", "Assigned employee", "Supervisor", "Team", "Location", "Zone", "Submitted", "Reviewed", "Review feedback"]
            rows = [[o.id, o.title, o.status, o.assignee.full_name if o.assignee else "", o.assigned_supervisor.full_name if o.assigned_supervisor else "", o.team.name if o.team else "", o.location.name if o.location else "", o.zone.name if o.zone else "", o.submitted_at, o.reviewed_at, o.review_feedback] for o in operations if str(o.status or "").lower() in {"approved","rejected","completed","complete","done"}]
        else:
            headers = ["Operation ID", "Title", "Type", "Status", "Priority", "Team", "Assigned employee", "Supervisor", "Location", "Zone", "Due date", "Created", "Submitted", "Reviewed", "Overdue"]
            rows = [[o.id, o.title, o.task_type, o.status, o.priority, o.team.name if o.team else "", o.assignee.full_name if o.assignee else "", o.assigned_supervisor.full_name if o.assigned_supervisor else "", o.location.name if o.location else "", o.zone.name if o.zone else "", o.due_at, o.created_at, o.submitted_at, o.reviewed_at, "Yes" if o.overdue else "No"] for o in operations]
    elif kind == "logs":
        query = OperationLog.query.join(Operation, OperationLog.operation_id == Operation.id).filter(Operation.organization_id == org)
        if team_id: query = query.filter(Operation.team_id == team_id)
        if employee_id: query = query.filter(Operation.assigned_to_id == employee_id)
        if location_id: query = query.filter(Operation.location_id == location_id)
        if zone_id: query = query.filter(Operation.zone_id == zone_id)
        logs = query.order_by(OperationLog.created_at.desc()).limit(MAX_ROWS + 1).all()
        logs = [l for l in logs if _in_range(l.created_at, start, end) and (not status or status in (l.new_status or "").lower())]
        headers = ["Log ID", "Operation ID", "Operation", "Actor", "Action", "Previous status", "New status", "Message", "Date"]
        rows = [[l.id, l.operation_id, l.operation.title if l.operation else "", l.actor.full_name if l.actor else "", l.action, l.previous_status, l.new_status, l.message, l.created_at] for l in logs]
    elif kind == "staff":
        query = User.query.filter_by(organization_id=org)
        if employee_id: query = query.filter(User.id == employee_id)
        if status: query = query.filter(db.func.lower(User.account_status) == status)
        if supervisor_id: query = query.filter(User.id == supervisor_id)
        staff = query.order_by(User.full_name).limit(MAX_ROWS + 1).all()
        if team_id:
            member_ids = {r[0] for r in db.session.query(TeamMember.user_id).filter_by(team_id=team_id).all()}
            staff = [u for u in staff if u.id in member_ids]
        headers = ["Staff ID", "Name", "Email", "Role", "Account status", "Email verified", "Teams", "Joined"]
        rows = [[u.id, u.full_name, u.email, u.role, u.account_status, "Yes" if u.email_verified else "No", ", ".join(t.name for t in Team.query.join(TeamMember, TeamMember.team_id == Team.id).filter(TeamMember.user_id == u.id, Team.organization_id == org).all()), u.created_at] for u in staff]
    elif kind == "teams":
        query = Team.query.filter_by(organization_id=org)
        if team_id: query = query.filter(Team.id == team_id)
        if status: query = query.filter(db.func.lower(Team.status) == status)
        teams = query.order_by(Team.name).limit(MAX_ROWS + 1).all()
        headers = ["Team ID", "Team", "Status", "Capacity", "Members", "Locations", "Created"]
        rows = [[t.id, t.name, t.status, t.capacity, db.session.query(TeamMember.user_id).filter_by(team_id=t.id).count(), ", ".join(l.name for l in Location.query.filter_by(organization_id=org, team_id=t.id).all()), t.created_at] for t in teams]
    elif kind == "locations":
        query = Location.query.filter_by(organization_id=org)
        if location_id: query = query.filter(Location.id == location_id)
        if team_id: query = query.filter(Location.team_id == team_id)
        if status: query = query.filter(db.func.lower(Location.status) == status)
        locations = query.order_by(Location.name).limit(MAX_ROWS + 1).all()
        headers = ["Location ID", "Location", "Address", "Team", "Status", "Latitude", "Longitude", "Created"]
        rows = [[l.id, l.name, l.address, l.team.name if l.team else "", l.status, l.latitude, l.longitude, l.created_at] for l in locations]
    else:
        query = Zone.query.filter_by(organization_id=org)
        if zone_id: query = query.filter(Zone.id == zone_id)
        if location_id: query = query.filter(Zone.location_id == location_id)
        if team_id: query = query.filter(Zone.team_id == team_id)
        if status: query = query.filter(db.func.lower(Zone.status) == status)
        zones = query.order_by(Zone.name).limit(MAX_ROWS + 1).all()
        headers = ["Zone ID", "Zone", "Location", "Team", "Supervisor", "Status", "Created"]
        rows = [[z.id, z.name, z.location.name if z.location else "", z.team.name if z.team else "", z.supervisor.full_name if z.supervisor else "", z.status, z.created_at] for z in zones]

    if len(rows) > MAX_ROWS:
        raise ValueError("This report contains more than 10,000 rows. Narrow the date range or filters.")
    return {"report_type": kind, "headers": headers, "rows": rows, "filters": filters,
            "row_count": len(rows), "start_date": start.isoformat(), "end_date": end.isoformat()}


def _int_filter(payload, key):
    raw = payload.get(key)
    if raw in (None, ""): return None
    try:
        value = int(raw)
        if value < 1: raise ValueError()
        return value
    except (TypeError, ValueError):
        raise ValueError("Invalid filter: " + key.replace("_", " ") + ".")


def _format_export_value(value, date_format):
    text = _clean(value)
    match = re.match(r"^(\\d{4})-(\\d{2})-(\\d{2})(.*)$", text)
    if not match:
        return text
    year, month, day, suffix = match.groups()
    if date_format == "day-first":
        return f"{day}/{month}/{year}{suffix}"
    if date_format == "month-first":
        return f"{month}/{day}/{year}{suffix}"
    return text


def _render_file(report, file_format, report_name):
    headers, rows = report["headers"], report["rows"]
    date_format = report["filters"].get("date_format", "iso")
    if date_format not in {"iso", "day-first", "month-first"}:
        date_format = "iso"
    rows = [[_format_export_value(v, date_format) for v in row] for row in rows]
    if file_format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
        return ("\ufeff" + stream.getvalue()).encode("utf-8")
    if file_format == "xlsx":
        from app.routes.analytics import _make_xlsx
        return _make_xlsx(headers, rows, sheet_name="CleanTrack Report")
    from app.routes.analytics import _make_pdf
    return _make_pdf(headers, rows,
                     {"start_date": report["start_date"], "end_date": report["end_date"]})


def _audit(admin, action, artifact, details=None):
    db.session.add(AuditLog(actor_id=admin.id, organization_id=admin.organization_id,
        action=action, resource_type="report", resource_id=str(artifact.id) if artifact else None,
        details=details or {}))


@reports_bp.get("/options")
@roles_required("Admin")
def report_options():
    admin = get_current_user()
    org = admin.organization_id
    if not org: return jsonify({"error": "Your account is not linked to an organization."}), 403
    return jsonify({
        "teams": [{"id": x.id, "name": x.name} for x in Team.query.filter_by(organization_id=org).order_by(Team.name).all()],
        "staff": [{"id": x.id, "name": x.full_name, "role": x.role} for x in User.query.filter_by(organization_id=org).order_by(User.full_name).all()],
        "locations": [{"id": x.id, "name": x.name} for x in Location.query.filter_by(organization_id=org).order_by(Location.name).all()],
        "zones": [{"id": x.id, "name": x.name, "location_id": x.location_id} for x in Zone.query.filter_by(organization_id=org).order_by(Zone.name).all()],
        "operation_types": [{"id": name, "name": name} for name in db.session.query(Operation.task_type).filter(Operation.organization_id == org).distinct().order_by(Operation.task_type).all() if name[0]],
        "report_types": sorted(TYPES), "formats": sorted(FORMATS),
        "quota_bytes": ORG_QUOTA_BYTES, "retention_days": RETENTION_DAYS,
    }), 200


@reports_bp.post("/preview")
@roles_required("Admin")
def preview_report():
    admin = get_current_user()
    try:
        report = _rows_for(admin, request.get_json(silent=True) or {})
    except PermissionError as exc: return jsonify({"error": str(exc)}), 403
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    return jsonify({k: v for k, v in report.items() if k != "rows"} | {
        "headers": report["headers"], "rows": [[_clean(v) for v in row] for row in report["rows"][:100]],
        "preview_truncated": report["row_count"] > 100,
    }), 200


@reports_bp.post("/generate")
@roles_required("Admin")
def generate_report():
    admin = get_current_user()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 180:
        return jsonify({"error": "Enter a report name up to 180 characters."}), 400
    file_format = str(payload.get("format") or "csv").lower()
    if file_format == "excel": file_format = "xlsx"
    if file_format not in FORMATS: return jsonify({"error": "Choose PDF, CSV or XLSX."}), 400
    try:
        report = _rows_for(admin, payload)
        file_bytes = _render_file(report, file_format, name)
    except PermissionError as exc: return jsonify({"error": str(exc)}), 403
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    db.session.query(ReportArtifact).filter(ReportArtifact.expires_at <= datetime.now(timezone.utc)).delete(synchronize_session=False)
    used = db.session.query(db.func.coalesce(db.func.sum(ReportArtifact.file_size), 0)).filter(
        ReportArtifact.organization_id == admin.organization_id,
        ReportArtifact.expires_at > datetime.now(timezone.utc)).scalar() or 0
    if used + len(file_bytes) > ORG_QUOTA_BYTES:
        return jsonify({"error": "Your organization's 100 MB report storage allowance is full. Download important reports and allow expired files to be cleaned up before generating more."}), 413
    now = datetime.now(timezone.utc)
    artifact = ReportArtifact(organization_id=admin.organization_id, created_by_id=admin.id,
        name=name, report_type=report["report_type"], file_format=file_format,
        mime_type=MIME[file_format], file_size=len(file_bytes), filters=report["filters"],
        file_bytes=file_bytes, created_at=now, expires_at=now + timedelta(days=RETENTION_DAYS))
    db.session.add(artifact)
    db.session.flush()
    _audit(admin, "report.generated", artifact, {"name": name, "format": file_format, "rows": report["row_count"], "filters": report["filters"]})
    db.session.commit()
    return jsonify({"report": artifact.to_dict(), "row_count": report["row_count"],
                    "download_url": "/api/reports/" + str(artifact.id) + "/download",
                    "retention_days": RETENTION_DAYS}), 201


@reports_bp.get("/history")
@roles_required("Admin")
def report_history():
    admin = get_current_user()
    now = datetime.now(timezone.utc)
    db.session.query(ReportArtifact).filter(ReportArtifact.expires_at <= now).delete(synchronize_session=False)
    db.session.commit()
    rows = ReportArtifact.query.filter_by(organization_id=admin.organization_id).order_by(ReportArtifact.created_at.desc()).limit(100).all()
    used = db.session.query(db.func.coalesce(db.func.sum(ReportArtifact.file_size), 0)).filter(
        ReportArtifact.organization_id == admin.organization_id, ReportArtifact.expires_at > now).scalar() or 0
    return jsonify({"reports": [r.to_dict() for r in rows], "storage_used_bytes": used,
                    "storage_quota_bytes": ORG_QUOTA_BYTES, "retention_days": RETENTION_DAYS}), 200


@reports_bp.get("/<int:report_id>/download")
@roles_required("Admin")
def download_report(report_id):
    admin = get_current_user()
    artifact = ReportArtifact.query.filter_by(id=report_id, organization_id=admin.organization_id).first()
    if not artifact: return jsonify({"error": "Report not found or you do not have access."}), 404
    if artifact.expires_at <= datetime.now(timezone.utc):
        db.session.delete(artifact); db.session.commit()
        return jsonify({"error": "This report has expired. Generate it again if you still need it."}), 410
    _audit(admin, "report.downloaded", artifact, {"format": artifact.file_format})
    db.session.commit()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", unicodedata.normalize("NFKD", artifact.name)).strip("-._")[:100] or "cleantrack-report"
    suffix = {"xlsx": ".xlsx", "csv": ".csv", "pdf": ".pdf"}[artifact.file_format]
    response = make_response(artifact.file_bytes)
    response.headers["Content-Type"] = artifact.mime_type
    response.headers["Content-Disposition"] = 'attachment; filename="' + safe_name + suffix + '"'
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@reports_bp.post("/<int:report_id>/share")
@roles_required("Admin")
def share_report(report_id):
    admin = get_current_user()
    artifact = ReportArtifact.query.filter_by(id=report_id, organization_id=admin.organization_id).first()
    if not artifact: return jsonify({"error": "Report not found or you do not have access."}), 404
    if artifact.expires_at <= datetime.now(timezone.utc):
        return jsonify({"error": "This report has expired."}), 410
    payload = request.get_json(silent=True) or {}
    method = str(payload.get("method") or "").lower()
    if method not in {"email", "whatsapp"}:
        return jsonify({"error": "Choose email or WhatsApp."}), 400
    # Do not expose an authenticated artifact URL as a public link or claim delivery.
    _audit(admin, "report.share_requested", artifact, {"method": method, "delivery": "not_configured"})
    db.session.commit()
    return jsonify({"status": "pending", "method": method,
        "message": "CleanTrack has not configured a secure delivery provider. No report was sent. Download the report and attach it in your approved email or WhatsApp workflow.",
        "download_url": "/api/reports/" + str(artifact.id) + "/download"}), 202
