from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO, StringIO
import csv
import re
import zipfile
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, jsonify, make_response, request
from sqlalchemy import func

from app.extensions import db
from app.models.operation import Operation
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.models.location import Location
from app.models.zone import Zone
from app.services.permissions import get_current_user, roles_required


analytics_bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")

DONE_STATUSES = {"completed", "complete", "approved", "done", "closed"}
CANCELLED_STATUSES = {"cancelled", "canceled", "archived"}


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
            bool(operation.overdue) or (due_at is not None and due_at < now)
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


def _make_xlsx(headers, rows):
    """Create a small, standards-compliant XLSX workbook using only the standard library."""
    def cell(col, row_number, value):
        ref = f"{col}{row_number}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = xml_escape("" if value is None else str(value))
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    def col_name(number):
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    all_rows = [headers] + rows
    xml_rows = []
    for index, row in enumerate(all_rows, start=1):
        cells = "".join(cell(col_name(col), index, value) for col, value in enumerate(row, start=1))
        xml_rows.append(f'<row r="{index}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Team Rankings" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_rels)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet)
    return stream.getvalue()


def _make_pdf(headers, rows, period):
    """Render a minimal multi-page PDF summary without relying on extra packages."""
    lines = [
        "CleanTrack - Team Analytics / Team Rankings",
        f"Period: {period['start_date']} to {period['end_date']}",
        "Ordered by completion rate, completed volume, then fewer overdue tasks.",
        "",
        "Rank | Team | Members | Tasks | Completed | Rate | Open | Overdue | On-time rate",
        "-" * 105,
    ]
    for row in rows:
        name = str(row[1]).replace("|", "/")
        rate = "N/A" if row[6] == "" else f"{row[6]}%"
        on_time_rate = "N/A" if row[12] == "" else f"{row[12]}%"
        lines.append(
            f"{row[0]:>4} | {name[:25]:25} | {row[3]:>7} | {row[4]:>5} | "
            f"{row[5]:>9} | {rate:>6} | {row[7]:>4} | {row[8]:>7} | {on_time_rate:>7}"
        )
    lines.extend(["", "Task totals are limited to operations created in the selected date range."])
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


@analytics_bp.get("/teams")
@roles_required("Admin")
def team_analytics():
    admin = get_current_user()
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
    if not admin.organization_id:
        return jsonify({"error": "Your account is not linked to an organization."}), 403
    parsed, _unused, error = _parse_period()
    if error:
        return jsonify({"error": error[0]}), error[1]
    start_day, end_day, start_at, end_exclusive = parsed
    report = _build_report(admin, start_at, end_exclusive)
    report["period"] = {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()}
    headers, rows = _export_rows(report)
    file_format = (request.args.get("format") or "csv").strip().lower()
    date_stamp = f"{start_day.isoformat()}_to_{end_day.isoformat()}"
    if file_format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
        response = make_response("\ufeff" + stream.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-rankings-{date_stamp}.csv"'
        return response
    if file_format in {"xlsx", "excel"}:
        payload = _make_xlsx(headers, rows)
        response = make_response(payload)
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-rankings-{date_stamp}.xlsx"'
        return response
    if file_format == "pdf":
        payload = _make_pdf(headers, rows, report["period"])
        response = make_response(payload)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="cleantrack-team-rankings-{date_stamp}.pdf"'
        return response
    return jsonify({"error": "Unsupported export format. Use csv, xlsx, or pdf."}), 400
