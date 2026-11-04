from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from io import BytesIO, StringIO
import csv
import re
import zipfile
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, jsonify, make_response, request
from sqlalchemy import func

from app.extensions import db
from app.models.operation import Operation
from app.models.operation_execution import OperationExecution
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.location import Location
from app.models.zone import Zone
from app.models.audit_log import AuditLog
from app.services.permissions import get_current_user, roles_required
from app.models.subscription import OrganizationSubscription
from app.services.entitlements import subscription_has_premium


analytics_bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")

DONE_STATUSES = {"completed", "complete", "approved", "done", "closed"}
CANCELLED_STATUSES = {"cancelled", "canceled", "archived"}

def _analytics_access_error(admin):
    """Advanced analytics are available to active Premium subscriptions and trials."""
    if not admin or not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403
    subscription = OrganizationSubscription.query.filter_by(organization_id=admin.organization_id).first()
    if not subscription_has_premium(subscription):
        return jsonify({
            "error": "Advanced analytics require CleanTrack Premium. Start an eligible 14-day trial or subscribe to Premium.",
            "required_plan": "premium",
            "feature": "advanced_reports",
        }), 403
    return None




def _parse_period():
    """Use an explicit inclusive date range, defaulting to the last 30 calendar days."""
    today = datetime.now(timezone.utc).date()
    raw_start = (request.args.get("start_date") or "").strip()
    raw_end = (request.args.get("end_date") or "").strip()
    try:
        start_day = date.fromisoformat(raw_start) if raw_start else today - timedelta(days=29)
        end_day = date.fromisoformat(raw_end) if raw_end else today
    except ValueError:
        return None, None, ("start_date and end_date must use YYYY-MM-DD format.", 400)
    if start_day > end_day:
        return None, None, ("start_date must be on or before end_date.", 400)
    if (end_day - start_day).days > 3660:
        return None, None, ("The selected period cannot exceed 10 years.", 400)
    start_at = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return (start_day, end_day, start_at, end_exclusive), None, None


def _to_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_report(admin, start_at, end_exclusive):
    teams = Team.query.filter_by(organization_id=admin.organization_id).order_by(
        func.lower(Team.name), Team.id
    ).all()
    team_by_id = {row.id: row for row in teams}
    location_rows = Location.query.filter_by(organization_id=admin.organization_id).all()
    location_team = {row.id: row.team_id for row in location_rows if row.team_id in team_by_id}
    zone_rows = Zone.query.filter_by(organization_id=admin.organization_id).all()
    zone_team = {}
    for zone in zone_rows:
        assigned_team_id = zone.team_id or location_team.get(zone.location_id)
        if assigned_team_id in team_by_id:
            zone_team[zone.id] = assigned_team_id

    member_counts = {team.id: 0 for team in teams}
    member_rows = (
        db.session.query(TeamMember.team_id, User.id)
        .join(User, TeamMember.user_id == User.id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(
            Team.organization_id == admin.organization_id,
            User.organization_id == admin.organization_id,
            User.account_status == "active",
            TeamMember.team_id.in_(list(team_by_id) or [-1]),
        )
        .all()
    )
    for team_id, _user_id in member_rows:
        member_counts[team_id] = member_counts.get(team_id, 0) + 1

    operations = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.created_at >= start_at,
        Operation.created_at < end_exclusive,
    ).order_by(Operation.created_at, Operation.id).all()

    now = datetime.now(timezone.utc)
    totals = {team.id: {
        "team_id": team.id,
        "team_name": team.name,
        "team_status": team.status or "active",
        "member_count": member_counts.get(team.id, 0),
        "total_tasks": 0,
        "completed_tasks": 0,
        "open_tasks": 0,
        "overdue_tasks": 0,
        "reviewed_tasks": 0,
        "completed_with_due_date": 0,
        "on_time_completed_tasks": 0,
    } for team in teams}
    unassigned_operations = 0

    for operation in operations:
        # Prefer the explicit team assignment. If a task has no team selected,
        # resolve it through its zone or location, matching the team detail view.
        team_id = operation.team_id if operation.team_id in team_by_id else None
        if team_id is None and operation.zone_id:
            team_id = zone_team.get(operation.zone_id)
        if team_id is None and operation.location_id:
            team_id = location_team.get(operation.location_id)
        if team_id not in totals:
            unassigned_operations += 1
            continue

        row = totals[team_id]
        row["total_tasks"] += 1
        status = str(operation.status or "").strip().lower()
        is_done = status in DONE_STATUSES
        is_cancelled = status in CANCELLED_STATUSES
        due_at = _to_utc(operation.due_at)
        is_overdue = not is_done and not is_cancelled and (
            bool(operation.overdue) or status == "overdue" or (due_at is not None and due_at < now)
        )
        if is_done:
            row["completed_tasks"] += 1
            if operation.reviewed_at:
                row["reviewed_tasks"] += 1
            if due_at is not None:
                row["completed_with_due_date"] += 1
                completed_at = _to_utc(operation.reviewed_at or operation.submitted_at or operation.updated_at or operation.created_at)
                if completed_at is not None and completed_at <= due_at:
                    row["on_time_completed_tasks"] += 1
        elif not is_cancelled:
            row["open_tasks"] += 1
        if is_overdue:
            row["overdue_tasks"] += 1

    rows = []
    for team in teams:
        item = totals[team.id]
        task_total = item["total_tasks"]
        completed = item["completed_tasks"]
        due_completed = item["completed_with_due_date"]
        item["completion_rate"] = round(completed * 100 / task_total, 1) if task_total else None
        item["on_time_rate"] = round(item["on_time_completed_tasks"] * 100 / due_completed, 1) if due_completed else None
        item["overdue_rate"] = round(item["overdue_tasks"] * 100 / task_total, 1) if task_total else None
        # No synthetic score: rank by completion rate, then completed volume,
        # then fewer overdue tasks. Teams with no work in the period rank last.
        item["rank"] = 0
        rows.append(item)

    rows.sort(key=lambda item: (
        item["completion_rate"] is None,
        -(item["completion_rate"] if item["completion_rate"] is not None else -1),
        -item["completed_tasks"],
        item["overdue_tasks"],
        item["team_name"].casefold(),
        item["team_id"],
    ))
    for rank, item in enumerate(rows, start=1):
        item["rank"] = rank

    return {
        "period": {"start_date": start_at.date().isoformat(), "end_date": (end_exclusive - timedelta(days=1)).date().isoformat()},
        "methodology": {
            "period_basis": "Operations created during the selected date range; completion and overdue values reflect their current recorded state.",
            "completion_rate": "Completed tasks divided by all team tasks created in the selected period.",
            "on_time_rate": "Completed tasks with a due date completed by that due date, divided by completed tasks with a due date.",
            "ranking_order": "Higher completion rate, then more completed tasks, then fewer overdue tasks. Teams with no tasks in the period are ranked last.",
        },
        "summary": {
            "team_count": len(teams),
            "tasks_in_period": sum(item["total_tasks"] for item in rows),
            "unassigned_tasks": unassigned_operations,
            "completed_tasks": sum(item["completed_tasks"] for item in rows),
            "open_tasks": sum(item["open_tasks"] for item in rows),
            "overdue_tasks": sum(item["overdue_tasks"] for item in rows),
        },
        "teams": rows,
    }


def _comparison_rows(report, team_a_id, team_b_id):
    teams = {int(item["team_id"]): item for item in report["teams"]}
    if team_a_id not in teams or team_b_id not in teams:
        return None
    if team_a_id == team_b_id:
        return None
    team_a, team_b = teams[team_a_id], teams[team_b_id]
    metrics = [
        ("Members", "member_count", False),
        ("Tasks in period", "total_tasks", False),
        ("Completed tasks", "completed_tasks", False),
        ("Completion rate (%)", "completion_rate", True),
        ("Open tasks", "open_tasks", False),
        ("Overdue tasks", "overdue_tasks", False),
        ("Reviewed tasks", "reviewed_tasks", False),
        ("On-time rate (%)", "on_time_rate", True),
    ]
    rows = []
    for label, key, is_rate in metrics:
        a, b = team_a[key], team_b[key]
        delta = round(a - b, 1) if is_rate and a is not None and b is not None else (
            (a - b) if not is_rate else ""
        )
        rows.append([label, "" if a is None else a, "" if b is None else b, delta])
    return {
        "team_a_name": team_a["team_name"],
        "team_b_name": team_b["team_name"],
        "headers": ["Metric", team_a["team_name"], team_b["team_name"], "A - B (rate delta: percentage points)"],
        "rows": rows,
    }


def _export_rows(report):
    headers = [
        "Rank", "Team", "Team status", "Members", "Tasks in period",
        "Completed tasks", "Completion rate (%)", "Open tasks",
        "Overdue tasks", "Overdue rate (%)", "Reviewed tasks",
        "On-time completed tasks", "On-time rate (%)",
    ]
    rows = []
    for team in report["teams"]:
        rows.append([
            team["rank"], team["team_name"], team["team_status"], team["member_count"],
            team["total_tasks"], team["completed_tasks"],
            "" if team["completion_rate"] is None else team["completion_rate"],
            team["open_tasks"], team["overdue_tasks"],
            "" if team["overdue_rate"] is None else team["overdue_rate"],
            team["reviewed_tasks"], team["on_time_completed_tasks"],
            "" if team["on_time_rate"] is None else team["on_time_rate"],
        ])
    return headers, rows


def _make_xlsx(headers, rows, comparison=None, sheet_name="Team Rankings"):
    """Create an XLSX workbook with rankings and, when selected, a comparison sheet."""
    def col_name(number):
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def cell(col, row_number, value):
        ref = f"{col}{row_number}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = xml_escape("" if value is None else str(value))
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    def worksheet_xml(sheet_headers, sheet_rows):
        all_rows = [sheet_headers] + sheet_rows
        xml_rows = []
        for index, row in enumerate(all_rows, start=1):
            cells = "".join(cell(col_name(col), index, value) for col, value in enumerate(row, start=1))
            xml_rows.append(f'<row r="{index}">{cells}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>'
        )

    sheets = [(str(sheet_name)[:31] or "Analytics", headers, rows)]
    if comparison:
        sheets.append(("Team Comparison", comparison["headers"], comparison["rows"]))
    sheet_parts = [worksheet_xml(sheet_headers, sheet_rows) for _name, sheet_headers, sheet_rows in sheets]
    sheet_tags = "".join(
        f'<sheet name="{xml_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _sheet_headers, _sheet_rows) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + sheet_tags + '</sheets></workbook>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    relationship_tags = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + relationship_tags + '</Relationships>'
    )
    overrides = (
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
        )
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + overrides + '</Types>'
    )
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_rels)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, sheet_xml in enumerate(sheet_parts, start=1):
            workbook_zip.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)
    return stream.getvalue()

def _make_pdf(headers, rows, period, comparison=None):
    """Render a minimal multi-page PDF summary without relying on extra packages."""
    lines = [
        "CleanTrack - Team Analytics",
        f"Period: {period['start_date']} to {period['end_date']}",
        "Rank order: completion rate, completed-task volume, then fewer overdue tasks.",
    ]
    if comparison:
        lines.extend([
            "",
            f"TEAM COMPARISON: {comparison['team_a_name']} vs {comparison['team_b_name']}",
            "Metric | Team A | Team B | A minus B (rate differences are percentage points)",
        ])
        for row in comparison["rows"]:
            left = "N/A" if row[1] == "" else str(row[1])
            right = "N/A" if row[2] == "" else str(row[2])
            diff = "N/A" if row[3] == "" else str(row[3])
            lines.append(f"{str(row[0])[:33]:33} | {left[:14]:14} | {right[:14]:14} | {diff[:14]:14}")
    lines.extend([
        "",
        "TEAM RANKINGS",
        "Rank | Team | Members | Tasks | Completed | Rate | Open | Overdue | On-time rate",
        "-" * 105,
    ])
    for row in rows:
        name = str(row[1]).replace("|", "/")
        rate = "N/A" if row[6] == "" else f"{row[6]}%"
        on_time_rate = "N/A" if row[12] == "" else f"{row[12]}%"
        lines.append(
            f"{row[0]:>4} | {name[:25]:25} | {row[3]:>7} | {row[4]:>5} | "
            f"{row[5]:>9} | {rate:>6} | {row[7]:>4} | {row[8]:>7} | {on_time_rate:>7}"
        )
    lines.extend(["", "Period basis: operations created during the selected date range."])
    pages = [lines[i:i + 48] for i in range(0, len(lines), 48)] or [[]]
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids = []
    for page_index, page_lines in enumerate(pages):
        content_id = 4 + page_index * 2
        page_id = content_id + 1
        page_ids.append(page_id)
        commands = ["BT", "/F1 9 Tf", "42 755 Td", "12 TL"]
        for line in page_lines:
            ascii_line = line.encode("ascii", "replace").decode("ascii")
            escaped_line = ascii_line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped_line}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream.decode('ascii')}\nendstream"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    max_id = max(objects)
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_id + 1)
    for object_id in range(1, max_id + 1):
        offsets[object_id] = len(output)
        payload = objects[object_id].encode("latin-1")
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(payload)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_id + 1):
        output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )
    return bytes(output)




@analytics_bp.get("/workspace")
@roles_required("Admin")
def analytics_workspace():
    """Return organization-scoped analytics dimensions, trend events and breakdowns.

    Trends for approvals/rejections are sourced from recorded operation audit
    events; they are not inferred from a task's current status.
    """
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    parsed, _unused, error = _parse_period()
    if error:
        return jsonify({"error": error[0]}), error[1]
    start_day, end_day, start_at, end_exclusive = parsed
    time_zone_name = (request.args.get("time_zone") or "UTC").strip()
    try:
        report_timezone = ZoneInfo(time_zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": "time_zone must be a valid IANA time-zone name."}), 400
    start_at = datetime.combine(start_day, time.min, tzinfo=report_timezone).astimezone(timezone.utc)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=report_timezone).astimezone(timezone.utc)

    def parse_ids(parameter):
        raw_values = request.args.getlist(parameter)
        values = []
        for raw in raw_values:
            values.extend(part.strip() for part in str(raw).split(",") if part.strip())
        if not values:
            return [], None
        parsed_values = []
        for raw in values:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None, f"{parameter} must contain numeric IDs."
            if value <= 0:
                return None, f"{parameter} must contain positive IDs."
            if value not in parsed_values:
                parsed_values.append(value)
        return parsed_values, None

    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        ids, message = parse_ids(key)
        if message:
            return jsonify({"error": message}), 400
        selected[key] = set(ids)

    bucket = (request.args.get("bucket") or "day").strip().lower()
    if bucket not in {"day", "week", "month", "quarter", "year"}:
        return jsonify({"error": "bucket must be day, week, month, quarter, or year."}), 400
    task_type_filter = (request.args.get("task_type") or "").strip()

    teams = Team.query.filter_by(organization_id=admin.organization_id).order_by(
        func.lower(Team.name), Team.id
    ).all()
    locations = Location.query.filter_by(organization_id=admin.organization_id).order_by(
        func.lower(Location.name), Location.id
    ).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).order_by(
        func.lower(Zone.name), Zone.id
    ).all()
    employees = User.query.filter(
        User.organization_id == admin.organization_id,
        User.account_status == "active",
        User.role.in_(["Staff", "Supervisor"]),
    ).order_by(func.lower(User.full_name), User.id).all()

    team_by_id = {row.id: row for row in teams}
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    employee_by_id = {row.id: row for row in employees}

    operations = Operation.query.filter_by(
        organization_id=admin.organization_id
    ).order_by(Operation.created_at, Operation.id).all()

    def dimension_ids(operation):
        location_id = operation.location_id
        zone = zone_by_id.get(operation.zone_id)
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_by_id else None
        if team_id is None and zone:
            candidate = zone.team_id or (location_by_id.get(zone.location_id).team_id if location_by_id.get(zone.location_id) else None)
            if candidate in team_by_id:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_by_id:
                team_id = location.team_id
        return team_id, location_id, operation.zone_id, operation.assigned_to_id

    eligible_ops = []
    resolved_dimensions = {}
    for operation in operations:
        team_id, location_id, zone_id, employee_id = dimension_ids(operation)
        resolved_dimensions[operation.id] = {
            "team_id": team_id,
            "location_id": location_id,
            "zone_id": zone_id,
            "employee_id": employee_id,
        }
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            continue
        eligible_ops.append(operation)

    def as_utc(value):
        return _to_utc(value)

    def in_creation_period(operation):
        created = as_utc(operation.created_at)
        return created is not None and start_at <= created < end_exclusive

    done_statuses = {"completed", "complete", "approved", "verified", "done", "closed"}
    cancelled_statuses = {"cancelled", "canceled", "archived"}
    now = datetime.now(timezone.utc)

    def canonical_status(operation):
        status = str(operation.status or "Pending").strip().lower()
        if status in done_statuses:
            return "Completed"
        if status in cancelled_statuses:
            return "Cancelled"
        if status == "rejected":
            return "Rejected"
        return {
            "pending": "Pending",
            "assigned": "Assigned",
            "in progress": "In Progress",
            "in_progress": "In Progress",
            "submitted": "Submitted",
            "overdue": "Overdue",
        }.get(status, (status.title() if status else "Unknown"))

    def is_overdue_now(operation):
        status = str(operation.status or "").strip().lower()
        if status in done_statuses or status in cancelled_statuses:
            return False
        due = as_utc(operation.due_at)
        return bool(operation.overdue) or status == "overdue" or (due is not None and due < now)

    def completion_time(operation):
        return as_utc(operation.reviewed_at or operation.submitted_at)

    def metrics(items):
        total = len(items)
        completed = [item for item in items if canonical_status(item) == "Completed"]
        open_items = [item for item in items if canonical_status(item) not in {"Completed", "Cancelled"}]
        overdue_items = [item for item in open_items if is_overdue_now(item)]
        due_completed = [item for item in completed if as_utc(item.due_at) is not None and completion_time(item) is not None]
        on_time = [
            item for item in due_completed
            if completion_time(item) is not None and completion_time(item) <= as_utc(item.due_at)
        ]
        durations = []
        for item in completed:
            created = as_utc(item.created_at)
            finished = completion_time(item)
            if created is not None and finished is not None and finished >= created:
                durations.append((finished - created).total_seconds() / 3600.0)
        return {
            "total_tasks": total,
            "completed_tasks": len(completed),
            "open_tasks": len(open_items),
            "overdue_tasks": len(overdue_items),
            "completion_rate": round(len(completed) * 100 / total, 1) if total else None,
            "overdue_rate": round(len(overdue_items) * 100 / total, 1) if total else None,
            "on_time_rate": round(len(on_time) * 100 / len(due_completed), 1) if due_completed else None,
            "on_time_completed_tasks": len(on_time),
            "tasks_with_due_date_completed": len(due_completed),
            "average_completion_hours": round(sum(durations) / len(durations), 1) if durations else None,
        }

    cohort = [item for item in eligible_ops if in_creation_period(item)]
    summary_metrics = metrics(cohort)
    current_statuses = {}
    for item in cohort:
        label = canonical_status(item)
        current_statuses[label] = current_statuses.get(label, 0) + 1

    eligible_ids = [str(item.id) for item in eligible_ops]
    audit_events = []
    if eligible_ids:
        audit_events = AuditLog.query.filter(
            AuditLog.organization_id == admin.organization_id,
            AuditLog.resource_type == "Operation",
            AuditLog.resource_id.in_(eligible_ids),
            AuditLog.created_at >= start_at,
            AuditLog.created_at < end_exclusive,
            AuditLog.action.ilike("operation.%"),
        ).order_by(AuditLog.created_at, AuditLog.id).all()

    def bucket_start(day):
        if bucket == "week":
            return day - timedelta(days=day.weekday())
        if bucket == "month":
            return day.replace(day=1)
        if bucket == "quarter":
            month = ((day.month - 1) // 3) * 3 + 1
            return day.replace(month=month, day=1)
        if bucket == "year":
            return day.replace(month=1, day=1)
        return day

    def bucket_key(value):
        utc_value = as_utc(value)
        return bucket_start(utc_value.astimezone(report_timezone).date()).isoformat() if utc_value is not None else None

    def bucket_label(key):
        day = date.fromisoformat(key)
        if bucket == "week":
            return "Week of " + day.strftime("%d %b %Y")
        if bucket == "month":
            return day.strftime("%b %Y")
        if bucket == "quarter":
            return f"{day.year} Q{((day.month - 1) // 3) + 1}"
        if bucket == "year":
            return str(day.year)
        return day.strftime("%d %b")

    bucket_dates = {}
    day = start_day
    while day <= end_day:
        key = bucket_start(day).isoformat()
        bucket_dates.setdefault(key, bucket_label(key))
        day += timedelta(days=1)

    trends_by_key = {
        key: {"period_key": key, "label": label, "created": 0, "completed": 0, "approved": 0, "rejected": 0, "submitted": 0}
        for key, label in bucket_dates.items()
    }
    for operation in eligible_ops:
        created = as_utc(operation.created_at)
        if created is not None and start_at <= created < end_exclusive:
            key = bucket_key(created)
            if key in trends_by_key:
                trends_by_key[key]["created"] += 1

    event_ids = {"created": set(), "completed": set(), "approved": set(), "rejected": set(), "submitted": set()}
    for event in audit_events:
        occurred = as_utc(event.created_at)
        if occurred is None:
            continue
        key = bucket_key(occurred)
        if key not in trends_by_key:
            continue
        action = str(event.action or "").strip().lower()
        resource_id = str(event.resource_id or "")
        if action.endswith(".submitted"):
            trends_by_key[key]["submitted"] += 1
            event_ids["submitted"].add(event.id)
        elif action.endswith(".verified"):
            trends_by_key[key]["approved"] += 1
            trends_by_key[key]["completed"] += 1
            event_ids["approved"].add(event.id)
            event_ids["completed"].add(event.id)
        elif action.endswith(".completed"):
            trends_by_key[key]["completed"] += 1
            event_ids["completed"].add(event.id)
        elif action.endswith(".rejected"):
            trends_by_key[key]["rejected"] += 1
            event_ids["rejected"].add(event.id)

    trend_rows = [trends_by_key[key] for key in sorted(trends_by_key)]

    def make_breakdown(attribute, name_lookup, all_ids, label_lookup=None):
        grouped = {item_id: [] for item_id in all_ids}
        for operation in cohort:
            dimensions = resolved_dimensions[operation.id]
            item_id = dimensions[attribute]
            if item_id in grouped:
                grouped[item_id].append(operation)
        rows = []
        for item_id in all_ids:
            name = name_lookup.get(item_id, "Unknown")
            row = {"id": item_id, "name": name, **metrics(grouped.get(item_id, []))}
            rows.append(row)
        rows.sort(key=lambda row: (-row["total_tasks"], row["name"].casefold(), row["id"]))
        return rows

    team_breakdown = make_breakdown(
        "team_id", {item.id: item.name for item in teams}, [item.id for item in teams]
    )
    location_breakdown = make_breakdown(
        "location_id", {item.id: item.name for item in locations}, [item.id for item in locations]
    )
    zone_breakdown = make_breakdown(
        "zone_id", {item.id: item.name for item in zones}, [item.id for item in zones]
    )
    employee_breakdown = make_breakdown(
        "employee_id", {item.id: item.full_name for item in employees}, [item.id for item in employees]
    )

    type_names = sorted(
        {str(item.task_type or "General").strip() or "General" for item in cohort},
        key=str.casefold,
    )
    type_breakdown = []
    for name in type_names:
        items = [item for item in cohort if (str(item.task_type or "General").strip() or "General") == name]
        type_breakdown.append({"name": name, **metrics(items)})
    type_breakdown.sort(key=lambda row: (-row["total_tasks"], row["name"].casefold()))

    status_rows = [
        {"name": name, "value": count}
        for name, count in sorted(current_statuses.items(), key=lambda pair: (-pair[1], pair[0]))
    ]

    created_cohort_ids = {item.id for item in cohort}
    completion_event_count = sum(row["completed"] for row in trends_by_key.values())
    approval_event_count = sum(row["approved"] for row in trends_by_key.values())
    rejection_event_count = sum(row["rejected"] for row in trends_by_key.values())
    submitted_event_count = sum(row["submitted"] for row in trends_by_key.values())

    review_decision_count = approval_event_count + rejection_event_count
    approval_rate = round(approval_event_count * 100 / review_decision_count, 1) if review_decision_count else None
    rejection_rate = round(rejection_event_count * 100 / review_decision_count, 1) if review_decision_count else None

    response = {
        "period": {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()},
        "bucket": bucket,
        "time_zone": time_zone_name,
        "summary": {
            **summary_metrics,
            "tasks_created_in_period": summary_metrics["total_tasks"],
            "approval_events": approval_event_count,
            "rejection_events": rejection_event_count,
            "approval_rate": approval_rate,
            "rejection_rate": rejection_rate,
            "completion_events": completion_event_count,
            "submission_events": submitted_event_count,
            "team_count": len(teams),
            "employee_count": len(employees),
        },
        "trends": trend_rows,
        "breakdowns": {
            "teams": team_breakdown,
            "employees": employee_breakdown,
            "locations": location_breakdown,
            "zones": zone_breakdown,
            "operation_types": type_breakdown,
            "statuses": status_rows,
        },
        "filters": {
            "teams": [{"id": item.id, "name": item.name, "status": item.status} for item in teams],
            "locations": [{"id": item.id, "name": item.name} for item in locations],
            "zones": [{"id": item.id, "name": item.name, "location_id": item.location_id} for item in zones],
            "employees": [{"id": item.id, "name": item.full_name, "role": item.role} for item in employees],
            "operation_types": sorted({str(item.task_type or "General").strip() or "General" for item in operations}, key=str.casefold),
        },
        "methodology": {
            "cohort": "Summary and group breakdown metrics describe work created during the selected date period, using its current recorded state.",
            "trends": "Created work is grouped by operations.created_at. Completed, submitted, approved and rejected trends are grouped by recorded operation audit events occurring during the selected period and matching the current filters. Approved verification events also count as completed events; approval is therefore a subset of completions. Approval/rejection histories require those events to exist in the audit trail.",
            "approval_rate": "Recorded approvals divided by recorded approval plus rejection decisions during the selected period. This is based on decision events, not current task status.",
            "rejection_rate": "Recorded rejections divided by recorded approval plus rejection decisions during the selected period. This is based on decision events, not current task status.",
            "completion_rate": "Currently completed tasks created during the selected period divided by tasks created during that period.",
            "on_time_rate": "Completed tasks with a due date whose recorded completion/submission/review time is at or before the due date, divided by completed tasks with a due date.",
            "average_completion_hours": "Average elapsed hours from operation creation to a recorded submission or review timestamp for tasks currently completed in the selected creation cohort. Records without a submission or review timestamp are excluded rather than estimating completion time from an unrelated update.",
            "filters": "Team assignment uses the explicit team, then the task zone's team, then the task location's team where available.",
            "time_zone": "Calendar-day boundaries and trend buckets use the selected IANA time zone: " + time_zone_name + ".",
        },
    }
    export_format = (request.args.get("format") or "").strip().lower()
    if export_format:
        view_name = (request.args.get("view") or "trends").strip().lower()
        if export_format not in {"csv", "xlsx", "excel"}:
            return jsonify({"error": "Unsupported export format. Use csv or xlsx."}), 400
        allowed_views = {"trends", "teams", "employees", "locations", "zones", "operation_types", "statuses"}
        if view_name not in allowed_views:
            return jsonify({"error": "Unsupported analytics view."}), 400

        if view_name == "trends":
            headers = ["Period", "Tasks created", "Completion events", "Approval events", "Rejection events", "Submission events"]
            rows = [
                [item["label"], item["created"], item["completed"], item["approved"], item["rejected"], item["submitted"]]
                for item in response["trends"]
            ]
        elif view_name == "statuses":
            headers = ["Current status", "Operations"]
            rows = [[item["name"], item["value"]] for item in response["breakdowns"]["statuses"]]
        else:
            headers = [
                "Name", "Tasks created", "Completed tasks", "Completion rate (%)",
                "Open tasks", "Overdue tasks", "Overdue rate (%)", "On-time rate (%)",
                "Average completion time (hours)",
            ]
            rows = [
                [
                    item["name"], item["total_tasks"], item["completed_tasks"], item["completion_rate"],
                    item["open_tasks"], item["overdue_tasks"], item["overdue_rate"], item["on_time_rate"],
                    item["average_completion_hours"],
                ]
                for item in response["breakdowns"][view_name]
            ]

        stamp = f'{start_day.isoformat()}_to_{end_day.isoformat()}'
        if export_format == "csv":
            stream = StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(rows)
            file_response = make_response("\ufeff" + stream.getvalue())
            file_response.headers["Content-Type"] = "text/csv; charset=utf-8"
            file_response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-analytics-{view_name}-{stamp}.csv"'
            return file_response

        payload = _make_xlsx(headers, rows, sheet_name=view_name.replace("_", " ").title())
        file_response = make_response(payload)
        file_response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        file_response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-analytics-{view_name}-{stamp}.xlsx"'
        return file_response

    return jsonify(response), 200


@analytics_bp.get("/teams")
@roles_required("Admin")
def team_analytics():
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403
    parsed, _unused, error = _parse_period()
    if error:
        return jsonify({"error": error[0]}), error[1]
    start_day, end_day, start_at, end_exclusive = parsed
    report = _build_report(admin, start_at, end_exclusive)
    report["period"] = {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()}
    return jsonify(report), 200


@analytics_bp.get("/teams/export")
@roles_required("Admin")
def export_team_analytics():
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403
    parsed, _unused, error = _parse_period()
    if error:
        return jsonify({"error": error[0]}), error[1]
    start_day, end_day, start_at, end_exclusive = parsed
    report = _build_report(admin, start_at, end_exclusive)
    report["period"] = {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()}
    headers, rows = _export_rows(report)
    comparison = None
    raw_team_a = (request.args.get("team_a") or "").strip()
    raw_team_b = (request.args.get("team_b") or "").strip()
    if raw_team_a or raw_team_b:
        try:
            team_a_id, team_b_id = int(raw_team_a), int(raw_team_b)
        except (TypeError, ValueError):
            return jsonify({"error": "team_a and team_b must be valid team IDs."}), 400
        comparison = _comparison_rows(report, team_a_id, team_b_id)
        if comparison is None:
            return jsonify({"error": "Choose two different teams in your organization to export a comparison."}), 400
    file_format = (request.args.get("format") or "csv").strip().lower()
    date_stamp = f"{start_day.isoformat()}_to_{end_day.isoformat()}"
    if file_format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
        if comparison:
            writer.writerow([])
            writer.writerow(["Team Comparison", comparison["team_a_name"], comparison["team_b_name"]])
            writer.writerow(comparison["headers"])
            writer.writerows(comparison["rows"])
        response = make_response("\ufeff" + stream.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-analytics-{date_stamp}.csv"'
        return response
    if file_format in {"xlsx", "excel"}:
        payload = _make_xlsx(headers, rows, comparison)
        response = make_response(payload)
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-analytics-{date_stamp}.xlsx"'
        return response
    if file_format == "pdf":
        payload = _make_pdf(headers, rows, report["period"], comparison)
        response = make_response(payload)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-analytics-{date_stamp}.pdf"'
        return response
    return jsonify({"error": "Unsupported export format. Use csv, xlsx, or pdf."}), 400

@analytics_bp.get("/predictions")
@roles_required("Admin")
def analytics_predictions():
    """Forecast near-term operation demand from this organisation's own history."""
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    # Keep prediction scope identical to the Analytics filters, but use a fixed
    # historical training window rather than the currently selected report dates.
    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        raw = request.args.get(key, "")
        values = []
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                return jsonify({"error": key + " must contain numeric IDs."}), 400
            if value <= 0:
                return jsonify({"error": key + " must contain positive IDs."}), 400
            if value not in values:
                values.append(value)
        selected[key] = set(values)

    task_type_filter = (request.args.get("task_type") or "").strip()
    time_zone_name = (request.args.get("time_zone") or "UTC").strip()
    try:
        report_timezone = ZoneInfo(time_zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": "time_zone must be a valid IANA time-zone name."}), 400

    teams = Team.query.filter_by(organization_id=admin.organization_id).all()
    locations = Location.query.filter_by(organization_id=admin.organization_id).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).all()
    employees = User.query.filter(
        User.organization_id == admin.organization_id,
        User.account_status == "active",
        User.role.in_(["Staff", "Supervisor"]),
    ).all()
    team_ids = {row.id for row in teams}
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    employee_ids = {row.id for row in employees}

    def resolved_dimensions(operation):
        zone = zone_by_id.get(operation.zone_id)
        location_id = operation.location_id
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_ids else None
        if team_id is None and zone:
            candidate = zone.team_id or (
                location_by_id.get(zone.location_id).team_id
                if location_by_id.get(zone.location_id) else None
            )
            if candidate in team_ids:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_ids:
                team_id = location.team_id
        return team_id, location_id, operation.zone_id, operation.assigned_to_id

    now_local = datetime.now(report_timezone)
    today = now_local.date()
    history_start = today - timedelta(days=56)
    history_start_at = datetime.combine(history_start, time.min, tzinfo=report_timezone).astimezone(timezone.utc)
    today_start_at = datetime.combine(today, time.min, tzinfo=report_timezone).astimezone(timezone.utc)

    # Explicit organisation predicate is mandatory: no cross-tenant training data.
    operations = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.created_at >= history_start_at,
        Operation.created_at < today_start_at,
    ).all()

    daily_counts = {(history_start + timedelta(days=i)).isoformat(): 0 for i in range(56)}
    for operation in operations:
        team_id, location_id, zone_id, employee_id = resolved_dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if employee_id is not None and employee_id not in employee_ids:
            continue
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            continue
        created_at = _to_utc(operation.created_at)
        if created_at is None:
            continue
        local_day = created_at.astimezone(report_timezone).date()
        key = local_day.isoformat()
        if key in daily_counts:
            daily_counts[key] += 1

    history = [
        {"date": (history_start + timedelta(days=i)).isoformat(),
         "actual": daily_counts[(history_start + timedelta(days=i)).isoformat()]}
        for i in range(56)
    ]
    total_operations = sum(row["actual"] for row in history)
    weekday_samples = {weekday: [] for weekday in range(7)}
    for row in history:
        weekday_samples[date.fromisoformat(row["date"]).weekday()].append(row["actual"])

    base_response = {
        "scope": "This organisation only",
        "history_days": 56,
        "history_start": history_start.isoformat(),
        "history_end": (today - timedelta(days=1)).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": "Weekday seasonal baseline with capped recent-trend adjustment",
        "sample_count": total_operations,
        "notice": "AI-generated estimates may be wrong. They are based only on this organisation's historical operational data and are not guaranteed outcomes.",
    }
    recent_days = history[-28:]
    earlier_days = history[:28]
    recent_mean = sum(row["actual"] for row in recent_days) / 28
    earlier_mean = sum(row["actual"] for row in earlier_days) / 28
    trend_factor = 1.0 if earlier_mean <= 0 else max(0.75, min(1.25, recent_mean / earlier_mean))

    forecast = []
    for offset in range(7):
        target_day = today + timedelta(days=offset)
        samples = weekday_samples[target_day.weekday()]
        # If this weekday has no observations, fall back to the organisation-wide
        # daily average. With an empty history, report zero rather than fabricate data.
        mean = (sum(samples) / len(samples)) if samples else (
            total_operations / len(history) if history else 0.0
        )
        adjusted = mean * trend_factor
        variance = sum((value - mean) ** 2 for value in samples) / max(1, len(samples) - 1)
        spread = (variance ** 0.5) * 1.28
        forecast.append({
            "date": target_day.isoformat(),
            "predicted_operations": round(max(0, adjusted), 1),
            "lower_estimate": round(max(0, adjusted - spread), 1),
            "upper_estimate": round(max(0, adjusted + spread), 1),
            "weekday_sample_count": len(samples),
        })

    # Rolling holdout: fit on the first 42 days and evaluate on the final
    # 14 days. This is historical validation, not a guarantee of future accuracy.
    train = history[:42]
    holdout = history[42:]
    train_weekdays = {weekday: [] for weekday in range(7)}
    for row in train:
        train_weekdays[date.fromisoformat(row["date"]).weekday()].append(row["actual"])
    first_half_mean = sum(row["actual"] for row in train[:21]) / 21
    second_half_mean = sum(row["actual"] for row in train[21:]) / 21
    validation_trend = 1.0 if first_half_mean <= 0 else max(
        0.75, min(1.25, second_half_mean / first_half_mean)
    )
    naive_mean = sum(row["actual"] for row in train) / len(train)
    model_errors = []
    baseline_errors = []
    for row in holdout:
        samples = train_weekdays[date.fromisoformat(row["date"]).weekday()]
        prediction = (sum(samples) / len(samples)) * validation_trend if samples else naive_mean
        model_errors.append(abs(row["actual"] - prediction))
        baseline_errors.append(abs(row["actual"] - naive_mean))
    actual_total = sum(row["actual"] for row in holdout)
    model_mae = sum(model_errors) / len(model_errors)
    baseline_mae = sum(baseline_errors) / len(baseline_errors)
    model_wape = (sum(model_errors) / actual_total * 100) if actual_total else None
    baseline_wape = (sum(baseline_errors) / actual_total * 100) if actual_total else None
    validation = {
        "method": "42-day training window; following 14 days held out",
        "holdout_days": len(holdout),
        "actual_operations": actual_total,
        "model_mae": round(model_mae, 2),
        "baseline_mae": round(baseline_mae, 2),
        "model_wape_percent": round(model_wape, 1) if model_wape is not None else None,
        "baseline_wape_percent": round(baseline_wape, 1) if baseline_wape is not None else None,
        "beats_baseline": model_mae < baseline_mae,
    }

    return jsonify({
        **base_response,
        "status": "available",
        "message": "Forecast generated from 56 days of organisation-specific operation history.",
        "trend_adjustment": round(trend_factor, 3),
        "validation": validation,
        "forecast": forecast,
        "history": history,
    }), 200

@analytics_bp.get("/predictions/overdue-risk")
@roles_required("Admin")
def analytics_overdue_risk():
    """Estimate overdue likelihood from this organisation's matured history."""
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        values = set()
        for part in (request.args.get(key) or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                return jsonify({"error": key + " must contain numeric IDs."}), 400
            if value <= 0:
                return jsonify({"error": key + " must contain positive IDs."}), 400
            values.add(value)
        selected[key] = values
    task_type_filter = (request.args.get("task_type") or "").strip()

    teams = Team.query.filter_by(organization_id=admin.organization_id).all()
    locations = Location.query.filter_by(organization_id=admin.organization_id).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).all()
    team_ids = {row.id for row in teams}
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    active_people = {
        row.id for row in User.query.filter(
            User.organization_id == admin.organization_id,
            User.account_status == "active",
        ).all()
    }

    def dimensions(operation):
        zone = zone_by_id.get(operation.zone_id)
        location_id = operation.location_id
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_ids else None
        if team_id is None and zone:
            candidate = zone.team_id
            location = location_by_id.get(zone.location_id)
            if candidate not in team_ids and location:
                candidate = location.team_id
            if candidate in team_ids:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_ids:
                team_id = location.team_id
        return team_id, location_id, operation.zone_id, operation.assigned_to_id

    now = datetime.now(timezone.utc)
    history_start = now - timedelta(days=180)
    history_rows = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.created_at >= history_start,
        Operation.due_at.isnot(None),
        Operation.due_at < now,
    ).all()
    # Only matured, non-cancelled records with an observable outcome are labels.
    matured = []
    for operation in history_rows:
        status = str(operation.status or "").strip().lower()
        if status in CANCELLED_STATUSES:
            continue
        submitted = _to_utc(operation.submitted_at)
        due = _to_utc(operation.due_at)
        if due is None:
            continue
        if submitted is None and status in DONE_STATUSES:
            continue
        late = submitted > due if submitted is not None else True
        team_id, location_id, zone_id, employee_id = dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if employee_id is not None and employee_id not in active_people:
            continue
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            continue
        matured.append({
            "late": bool(late),
            "task_type": str(operation.task_type or "General").casefold(),
            "priority": str(operation.priority or "Normal").casefold(),
        })

    total = len(matured)
    late_total = sum(1 for row in matured if row["late"])
    global_rate = late_total / total if total else 0.0
    cohorts = {}
    for row in matured:
        key = (row["task_type"], row["priority"])
        cohort = cohorts.setdefault(key, {"count": 0, "late": 0})
        cohort["count"] += 1
        cohort["late"] += int(row["late"])

    base = {
        "scope": "This organisation only",
        "history_days": 180,
        "historical_sample_count": total,
        "historical_late_count": late_total,
        "historical_late_rate_percent": round(global_rate * 100, 1) if total else None,
        "generated_at": now.isoformat(),
        "method": "Smoothed empirical late-rate by operation type and priority, with organisation-wide fallback",
        "notice": "These estimates may be wrong. They use only this organisation's historical records and are not guarantees or individual performance assessments.",
    }
    if total == 0:
        return jsonify({
            **base,
            "status": "no_history",
            "message": "No matured overdue outcomes are available in the selected historical scope yet.",
            "at_risk_operations": [],
        }), 200

    current_rows = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.due_at.isnot(None),
        Operation.due_at > now,
    ).all()
    at_risk = []
    for operation in current_rows:
        status = str(operation.status or "").strip().lower()
        if status in DONE_STATUSES or status in CANCELLED_STATUSES or status in {"submitted", "verified"}:
            continue
        team_id, location_id, zone_id, employee_id = dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if employee_id is not None and employee_id not in active_people:
            continue
        task_type = str(operation.task_type or "General").casefold()
        if task_type_filter and task_type != task_type_filter.casefold():
            continue
        priority = str(operation.priority or "Normal").casefold()
        cohort = cohorts.get((task_type, priority))
        cohort_count = cohort["count"] if cohort else 0
        if cohort_count >= 8:
            observed_rate = cohort["late"] / cohort_count
            sample_size = cohort_count
            basis = "Matching operation type and priority"
        else:
            observed_rate = global_rate
            sample_size = total
            basis = "Organisation-wide history (fallback)"
        # Empirical-Bayes style shrinkage avoids extreme rates from small cohorts.
        smoothing_weight = 8
        risk = (observed_rate * sample_size + global_rate * smoothing_weight) / (sample_size + smoothing_weight)
        due = _to_utc(operation.due_at)
        hours_remaining = max(0, (due - now).total_seconds() / 3600) if due else None
        at_risk.append({
            "operation_id": operation.id,
            "title": operation.title,
            "task_type": operation.task_type or "General",
            "priority": operation.priority or "Normal",
            "due_at": due.isoformat() if due else None,
            "hours_until_due": round(hours_remaining, 1) if hours_remaining is not None else None,
            "estimated_late_risk_percent": round(risk * 100, 1),
            "historical_comparable_sample": sample_size,
            "basis": basis,
        })

    at_risk.sort(key=lambda row: (
        -row["estimated_late_risk_percent"],
        row["hours_until_due"] if row["hours_until_due"] is not None else float("inf"),
    ))
    return jsonify({
        **base,
        "status": "available",
        "message": "Risk estimates are historical cohort rates, not a trained individual-operation classifier.",
        "at_risk_operations": at_risk[:20],
        "evaluated_open_operations": len(at_risk),
    }), 200

@analytics_bp.get("/predictions/workforce-demand")
@roles_required("Admin")
def analytics_workforce_demand():
    """Estimate team-level incoming workload and staffing distribution."""
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        values = set()
        for part in (request.args.get(key) or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                return jsonify({"error": key + " must contain numeric IDs."}), 400
            if value <= 0:
                return jsonify({"error": key + " must contain positive IDs."}), 400
            values.add(value)
        selected[key] = values
    task_type_filter = (request.args.get("task_type") or "").strip()
    try:
        zone_name = (request.args.get("time_zone") or "UTC").strip()
        report_timezone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": "time_zone must be a valid IANA time-zone name."}), 400

    teams = Team.query.filter_by(
        organization_id=admin.organization_id, status="active"
    ).order_by(Team.name.asc()).all()
    team_by_id = {team.id: team for team in teams}
    locations = Location.query.filter_by(organization_id=admin.organization_id).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).all()
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    memberships = db.session.query(TeamMember.team_id, TeamMember.user_id).join(
        Team, Team.id == TeamMember.team_id
    ).join(User, User.id == TeamMember.user_id).filter(
        Team.organization_id == admin.organization_id,
        User.organization_id == admin.organization_id,
        User.account_status == "active",
        User.role.in_(["Staff", "Supervisor"]),
    ).all()
    staff_by_team = {team.id: set() for team in teams}
    for team_id, user_id in memberships:
        if team_id in staff_by_team:
            staff_by_team[team_id].add(user_id)

    def team_for(operation):
        zone = zone_by_id.get(operation.zone_id)
        location_id = operation.location_id
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_by_id else None
        if team_id is None and zone:
            candidate = zone.team_id
            location = location_by_id.get(zone.location_id)
            if candidate not in team_by_id and location:
                candidate = location.team_id
            if candidate in team_by_id:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_by_id:
                team_id = location.team_id
        return team_id, location_id

    now_local = datetime.now(report_timezone)
    today = now_local.date()
    start_day = today - timedelta(days=56)
    start_at = datetime.combine(start_day, time.min, tzinfo=report_timezone).astimezone(timezone.utc)
    end_at = datetime.combine(today, time.min, tzinfo=report_timezone).astimezone(timezone.utc)
    historical = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.created_at >= start_at,
        Operation.created_at < end_at,
    ).all()
    team_daily = {
        team.id: {
            (start_day + timedelta(days=i)).isoformat(): 0 for i in range(56)
        } for team in teams
    }
    team_effort = {team.id: [] for team in teams}
    team_samples = {team.id: 0 for team in teams}
    team_assigned = {team.id: set() for team in teams}
    included_total = 0
    for operation in historical:
        team_id, location_id = team_for(operation)
        if team_id not in team_daily:
            continue
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and operation.zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and operation.assigned_to_id not in selected["employee_ids"]:
            continue
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            continue
        created = _to_utc(operation.created_at)
        if not created:
            continue
        local_day = created.astimezone(report_timezone).date().isoformat()
        if local_day in team_daily[team_id]:
            team_daily[team_id][local_day] += 1
            included_total += 1
            team_samples[team_id] += 1
            if operation.estimated_minutes and operation.estimated_minutes > 0:
                team_effort[team_id].append(operation.estimated_minutes)
            if operation.assigned_to_id in staff_by_team[team_id]:
                team_assigned[team_id].add(operation.assigned_to_id)

    results = []
    for team in teams:
        if selected["team_ids"] and team.id not in selected["team_ids"]:
            continue
        if not team_samples[team.id]:
            continue
        daily = team_daily[team.id]
        weekday_values = {weekday: [] for weekday in range(7)}
        for day_text, count in daily.items():
            weekday_values[date.fromisoformat(day_text).weekday()].append(count)
        recent = list(daily.values())[-28:]
        earlier = list(daily.values())[:28]
        recent_mean = sum(recent) / 28
        earlier_mean = sum(earlier) / 28
        trend = 1.0 if earlier_mean <= 0 else max(0.75, min(1.25, recent_mean / earlier_mean))
        next_seven = []
        for offset in range(7):
            target = today + timedelta(days=offset)
            weekday_history = weekday_values[target.weekday()]
            expected = (sum(weekday_history) / len(weekday_history)) * trend
            next_seven.append(expected)
        forecast_operations = sum(next_seven)
        staff_count = len(staff_by_team.get(team.id, set()))
        avg_minutes = (
            sum(team_effort[team.id]) / len(team_effort[team.id])
            if team_effort[team.id] else None
        )
        estimated_hours = (
            forecast_operations * avg_minutes / 60
            if avg_minutes is not None else None
        )
        results.append({
            "team_id": team.id,
            "team_name": team.name,
            "historical_operations": team_samples[team.id],
            "forecast_operations_next_7_days": round(forecast_operations, 1),
            "forecast_by_day": [
                {"date": (today + timedelta(days=i)).isoformat(), "expected_operations": round(next_seven[i], 1)}
                for i in range(7)
            ],
            "active_team_members": staff_count,
            "currently_assigned_historical_members": len(team_assigned[team.id]),
            "expected_operations_per_active_member": round(forecast_operations / staff_count, 1) if staff_count else None,
            "average_recorded_estimate_minutes": round(avg_minutes, 1) if avg_minutes is not None else None,
            "estimated_effort_hours_next_7_days": round(estimated_hours, 1) if estimated_hours is not None else None,
            "effort_estimate_sample_count": len(team_effort[team.id]),
            "staffing_capacity_status": "not_assessed",
        })

    return jsonify({
        "status": "available",
        "scope": "This organisation only",
        "history_days": 56,
        "history_start": start_day.isoformat(),
        "history_end": (today - timedelta(days=1)).isoformat(),
        "historical_sample_count": included_total,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Weekday team-level workload baseline with capped recent-trend adjustment",
        "teams": results,
        "notice": "Workload estimates may be wrong. They use only this organisation's historical operational data. Active membership is not proof of shift availability; OMNIX does not claim staffing capacity without working-hour and leave data. Effort hours are derived from recorded estimates, not measured actual duration.",
    }), 200

@analytics_bp.get("/predictions/completion-time")
@roles_required("Admin")
def analytics_completion_time():
    """Estimate execution duration from this organisation's recorded attempts."""
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        values = set()
        for part in (request.args.get(key) or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                return jsonify({"error": key + " must contain numeric IDs."}), 400
            if value <= 0:
                return jsonify({"error": key + " must contain positive IDs."}), 400
            values.add(value)
        selected[key] = values
    task_type_filter = (request.args.get("task_type") or "").strip()

    teams = Team.query.filter_by(organization_id=admin.organization_id).all()
    team_ids = {row.id for row in teams}
    locations = Location.query.filter_by(organization_id=admin.organization_id).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).all()
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    active_people = {
        row.id for row in User.query.filter(
            User.organization_id == admin.organization_id,
            User.account_status == "active",
        ).all()
    }

    def dimensions(operation):
        zone = zone_by_id.get(operation.zone_id)
        location_id = operation.location_id
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_ids else None
        if team_id is None and zone:
            candidate = zone.team_id
            location = location_by_id.get(zone.location_id)
            if candidate not in team_ids and location:
                candidate = location.team_id
            if candidate in team_ids:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_ids:
                team_id = location.team_id
        return team_id, location_id, operation.zone_id, operation.assigned_to_id

    now = datetime.now(timezone.utc)
    history_start = now - timedelta(days=180)
    executions = db.session.query(OperationExecution, Operation).join(
        Operation, Operation.id == OperationExecution.operation_id
    ).filter(
        Operation.organization_id == admin.organization_id,
        OperationExecution.started_at >= history_start,
        OperationExecution.submitted_at.isnot(None),
    ).all()

    observations = []
    for execution, operation in executions:
        started = _to_utc(execution.started_at)
        submitted = _to_utc(execution.submitted_at)
        if not started or not submitted:
            continue
        minutes = (submitted - started).total_seconds() / 60
        # Exclude malformed timestamps and extreme idle sessions from the
        # operational-duration estimate; do not substitute planned estimates.
        if minutes < 1 or minutes > 72 * 60:
            continue
        team_id, location_id, zone_id, employee_id = dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if employee_id is not None and employee_id not in active_people:
            continue
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            continue
        observations.append({
            "minutes": minutes,
            "task_type": str(operation.task_type or "General").casefold(),
            "priority": str(operation.priority or "Normal").casefold(),
        })

    def quantile(values, fraction):
        ordered = sorted(values)
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    all_minutes = [row["minutes"] for row in observations]
    organisation_median = quantile(all_minutes, 0.5)
    type_values = {}
    cohort_values = {}
    for row in observations:
        type_values.setdefault(row["task_type"], []).append(row["minutes"])
        cohort_values.setdefault((row["task_type"], row["priority"]), []).append(row["minutes"])

    open_operations = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.submitted_at.is_(None),
    ).all()
    predictions = []
    for operation in open_operations:
        status = str(operation.status or "").strip().lower()
        if status in DONE_STATUSES or status in CANCELLED_STATUSES or status in {"submitted", "verified"}:
            continue
        team_id, location_id, zone_id, employee_id = dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            continue
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            continue
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            continue
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            continue
        if employee_id is not None and employee_id not in active_people:
            continue
        task_type = str(operation.task_type or "General").casefold()
        priority = str(operation.priority or "Normal").casefold()
        if task_type_filter and task_type != task_type_filter.casefold():
            continue

        exact = cohort_values.get((task_type, priority), [])
        by_type = type_values.get(task_type, [])
        if exact:
            source, basis = exact, "Matching operation type and priority"
        elif by_type:
            source, basis = by_type, "Matching operation type"
        else:
            source, basis = all_minutes, "Organisation-wide execution history"
        if not source:
            continue
        predictions.append({
            "operation_id": operation.id,
            "title": operation.title,
            "task_type": operation.task_type or "General",
            "priority": operation.priority or "Normal",
            "estimated_minutes": round(quantile(source, 0.5), 1),
            "lower_estimate_minutes": round(quantile(source, 0.25), 1),
            "upper_estimate_minutes": round(quantile(source, 0.75), 1),
            "historical_comparable_sample": len(source),
            "basis": basis,
        })

    predictions.sort(key=lambda row: row["estimated_minutes"], reverse=True)
    return jsonify({
        "status": "available" if observations else "no_history",
        "message": (
            "Completion-time estimates use recorded execution durations."
            if observations else
            "No usable submitted execution durations are available in this organisation's history yet."
        ),
        "scope": "This organisation only",
        "history_days": 180,
        "historical_execution_sample_count": len(observations),
        "history_start": history_start.isoformat(),
        "generated_at": now.isoformat(),
        "organisation_median_minutes": round(organisation_median, 1) if organisation_median is not None else None,
        "predicted_open_operations": len(predictions),
        "predictions": predictions[:30],
        "method": "Median and interquartile range of recorded execution durations, matched by operation type and priority with organisation-wide fallback",
        "notice": "Estimates may be wrong. They use only this organisation's historical execution records and are not guaranteed completion times.",
    }), 200

@analytics_bp.get("/predictions/anomalies")
@roles_required("Admin")
def analytics_anomalies():
    """Detect recurring operational patterns from this organisation's own history."""
    admin = get_current_user()
    access_error = _analytics_access_error(admin)
    if access_error:
        return access_error
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403

    selected = {}
    for key in ("team_ids", "location_ids", "zone_ids", "employee_ids"):
        values = set()
        for part in (request.args.get(key) or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                return jsonify({"error": key + " must contain numeric IDs."}), 400
            if value <= 0:
                return jsonify({"error": key + " must contain positive IDs."}), 400
            values.add(value)
        selected[key] = values
    task_type_filter = (request.args.get("task_type") or "").strip()

    teams = Team.query.filter_by(organization_id=admin.organization_id).all()
    team_ids = {row.id for row in teams}
    locations = Location.query.filter_by(organization_id=admin.organization_id).all()
    zones = Zone.query.filter_by(organization_id=admin.organization_id).all()
    location_by_id = {row.id: row for row in locations}
    zone_by_id = {row.id: row for row in zones}
    active_people = {
        row.id for row in User.query.filter(
            User.organization_id == admin.organization_id,
            User.account_status == "active",
        ).all()
    }

    def dimensions(operation):
        zone = zone_by_id.get(operation.zone_id)
        location_id = operation.location_id
        if location_id not in location_by_id and zone and zone.location_id in location_by_id:
            location_id = zone.location_id
        team_id = operation.team_id if operation.team_id in team_ids else None
        if team_id is None and zone:
            candidate = zone.team_id
            location = location_by_id.get(zone.location_id)
            if candidate not in team_ids and location:
                candidate = location.team_id
            if candidate in team_ids:
                team_id = candidate
        if team_id is None:
            location = location_by_id.get(location_id)
            if location and location.team_id in team_ids:
                team_id = location.team_id
        return team_id, location_id, operation.zone_id, operation.assigned_to_id

    now = datetime.now(timezone.utc)
    history_start = now - timedelta(days=90)
    operations = Operation.query.filter(
        Operation.organization_id == admin.organization_id,
        Operation.created_at >= history_start,
    ).all()

    def included(operation):
        team_id, location_id, zone_id, employee_id = dimensions(operation)
        if selected["team_ids"] and team_id not in selected["team_ids"]:
            return False
        if selected["location_ids"] and location_id not in selected["location_ids"]:
            return False
        if selected["zone_ids"] and zone_id not in selected["zone_ids"]:
            return False
        if selected["employee_ids"] and employee_id not in selected["employee_ids"]:
            return False
        if employee_id is not None and employee_id not in active_people:
            return False
        if task_type_filter and str(operation.task_type or "General").casefold() != task_type_filter.casefold():
            return False
        return True

    scoped = [row for row in operations if included(row)]
    daily_counts = {}
    type_outcomes = {}
    late_total = 0
    matured_total = 0
    for operation in scoped:
        created = _to_utc(operation.created_at)
        if created:
            day_key = created.date().isoformat()
            daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        due = _to_utc(operation.due_at)
        submitted = _to_utc(operation.submitted_at)
        status = str(operation.status or "").strip().lower()
        if due and due < now and status not in CANCELLED_STATUSES:
            # An overdue, still-open operation is a late outcome; submitted work
            # is late only when its submission timestamp exceeded its deadline.
            late = submitted > due if submitted else status not in DONE_STATUSES
            key = str(operation.task_type or "General")
            group = type_outcomes.setdefault(key, {"count": 0, "late": 0})
            group["count"] += 1
            group["late"] += int(late)
            matured_total += 1
            late_total += int(late)

    findings = []
    baseline_days = []
    for offset in range(8, 90):
        day = (now.date() - timedelta(days=offset)).isoformat()
        baseline_days.append(daily_counts.get(day, 0))
    baseline_mean = sum(baseline_days) / len(baseline_days) if baseline_days else 0
    recent_days = [
        (now.date() - timedelta(days=offset)).isoformat()
        for offset in range(7)
    ]
    recent_total = sum(daily_counts.get(day, 0) for day in recent_days)
    recent_daily_mean = recent_total / 7
    if baseline_mean > 0 and recent_daily_mean > baseline_mean * 1.8:
        findings.append({
            "kind": "workload_spike",
            "severity": "elevated",
            "title": "Recent operation volume is unusually high",
            "description": "The last seven days averaged more incoming operations per day than the preceding historical baseline.",
            "observed": round(recent_daily_mean, 2),
            "baseline": round(baseline_mean, 2),
            "change_percent": round((recent_daily_mean / baseline_mean - 1) * 100, 1),
            "evidence_count": recent_total,
        })

    organisation_late_rate = late_total / matured_total if matured_total else None
    for task_type, outcome in type_outcomes.items():
        if not outcome["count"]:
            continue
        rate = outcome["late"] / outcome["count"]
        # Highlight a materially higher rate than the organisation-wide
        # baseline. No finding is produced if there is no comparison baseline.
        if organisation_late_rate is not None and rate > organisation_late_rate + 0.15 and outcome["count"] >= 3:
            findings.append({
                "kind": "recurring_overdue_pattern",
                "severity": "elevated",
                "title": "Repeated overdue pattern: " + task_type,
                "description": "This operation type has a higher historical late rate than the organisation-wide rate.",
                "observed_late_rate_percent": round(rate * 100, 1),
                "organisation_late_rate_percent": round(organisation_late_rate * 100, 1),
                "evidence_count": outcome["count"],
                "late_count": outcome["late"],
            })

    execution_rows = db.session.query(
        OperationExecution.operation_id,
        func.count(OperationExecution.id),
    ).join(
        Operation, Operation.id == OperationExecution.operation_id
    ).filter(
        Operation.organization_id == admin.organization_id,
        OperationExecution.created_at >= history_start,
    ).group_by(OperationExecution.operation_id).all()
    operation_by_id = {row.id: row for row in scoped}
    repeat_counts = []
    for operation_id, attempts in execution_rows:
        operation = operation_by_id.get(operation_id)
        if not operation or attempts < 2:
            continue
        repeat_counts.append({
            "operation_id": operation.id,
            "title": operation.title,
            "task_type": operation.task_type or "General",
            "attempts": attempts,
        })
    repeat_counts.sort(key=lambda row: (-row["attempts"], row["title"] or ""))
    if repeat_counts:
        findings.append({
            "kind": "repeat_execution",
            "severity": "watch",
            "title": "Operations requiring repeated execution",
            "description": "These operations have multiple recorded execution attempts. This can indicate rework or a retry, but does not by itself prove a verification failure.",
            "evidence_count": len(repeat_counts),
            "examples": repeat_counts[:8],
        })

    findings.sort(key=lambda row: (0 if row["severity"] == "elevated" else 1, row["kind"]))
    return jsonify({
        "status": "available",
        "scope": "This organisation only",
        "history_days": 90,
        "historical_operation_count": len(scoped),
        "matured_deadline_outcomes": matured_total,
        "generated_at": now.isoformat(),
        "findings": findings,
        "summary": (
            "No unusual recurring patterns were detected in the selected historical scope."
            if not findings else
            str(len(findings)) + " operational pattern(s) identified for review."
        ),
        "method": "Historical volume comparison, operation-type overdue-rate comparison, and repeated execution-attempt analysis",
        "notice": "Detected patterns may be coincidental or incomplete. They are based only on this organisation's historical operational data and should be reviewed by an administrator.",
    }), 200

