import os, uuid, random, io, base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import qrcode
import resend
from werkzeug.security import check_password_hash, generate_password_hash

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity, get_jwt
)
import smtplib
from email.message import EmailMessage
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from database import get_db, init_db
from io import BytesIO
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
load_dotenv("back.env")
resend.api_key = os.getenv("RESEND_API_KEY")

GMAIL_SENDER = os.getenv("GMAIL_SENDER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

app = Flask(__name__, static_folder="../frontend", static_url_path="")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET", "dev_secret_change_me")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=12)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
jwt = JWTManager(app)

# Explicitly trust both localhost and 127.0.0.1 variants of your dashboard
CORS(
    app,
    origins=[
        "https://cleantrack-frontend-ry0o.onrender.com",
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    ],
    supports_credentials=True
)
def send_gmail(to_email, subject, body):
    try:
        token_json = os.getenv("GMAIL_TOKEN_JSON")

        if token_json:
            import json
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json),
                SCOPES
            )
        else:
            creds = Credentials.from_authorized_user_file(
                "token.json",
                SCOPES
            )

        service = build(
            "gmail",
            "v1",
            credentials=creds
        )

        message = EmailMessage()
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)

        encoded_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        service.users().messages().send(
            userId="me",
            body={"raw": encoded_message}
        ).execute()

        print(f"Email sent successfully to {to_email}")
        return True

    except Exception as e:
        print(f"Email failed: {e}")
        return False
    
    print("SEND EMAIL FUNCTION LOADED")
    
@app.route("/api/send-email", methods=["POST"])
@jwt_required()
def send_email_route():
    data = request.get_json() or {}

    to_email = data.get("to", "").strip()
    subject = data.get("subject", "").strip()
    message = data.get("message", "").strip()

    if not to_email or not subject or not message:
        return jsonify(error="To, subject, and message are required"), 400

    success = send_gmail(to_email, subject, message)

    if not success:
        return jsonify(error="Failed to send email"), 500

    return jsonify(
        success=True,
        message="Email sent successfully"
    ), 200
    
@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        res = jsonify({})
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return res, 200

UPLOAD_FOLDER = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ─── helpers ────────────────────────────────────────────────────────────────

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ============================================================
# CLEANTRACK IDENTITY & AUTHORIZATION CONTEXT
# ============================================================

def get_current_user():
    """
    Return the complete database record for the currently
    authenticated user.
    """
    user_id = get_jwt_identity()

    if not user_id:
        return None

    conn = get_db()

    user = conn.execute(
        """
        SELECT
            u.*,
            o.name AS organization_name,
            o.organization_code
        FROM users u
        LEFT JOIN organizations o
            ON o.id = u.organization_id
        WHERE u.id = ?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    return row_to_dict(user)


def get_current_user_id():
    """
    Return the authenticated user's ID.
    """
    return get_jwt_identity()

@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def get_current_user_route():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if user.get("account_status") != "active":
        return jsonify(error="Your account is not active"), 403

    user_id = user["id"]
    role = user["role"]

    conn = get_db()

    if role == "supervisor":
        team_rows = conn.execute(
            """
            SELECT team_id
            FROM team_supervisors
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    elif role == "employee":
        team_rows = conn.execute(
            """
            SELECT team_id
            FROM team_members
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    else:
        team_rows = []

    conn.close()

    team_ids = [row["team_id"] for row in team_rows]

    return jsonify(
        user={
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "location_id": user.get("location_id"),
            "organization_id": user.get("organization_id"),
            "organization_name": user.get("organization_name"),
            "organization_code": user.get("organization_code"),
            "account_status": user.get("account_status"),
            "team_ids": team_ids
        }
    ), 200



def get_current_org_id():
    """
    Return the organization ID of the authenticated user.
    """
    user = get_current_user()

    if not user:
        return None

    return user.get("organization_id")


def get_current_role():
    """
    Return the authenticated user's role.
    """
    user = get_current_user()

    if not user:
        return None

    return user.get("role")

def get_current_team_ids():
    """
    Return all team IDs associated with the current user.

    Supervisors:
        team_supervisors

    Employees:
        team_members

    Admins:
        empty list because admins have organization-wide scope.
    """
    user = get_current_user()

    if not user:
        return []

    role = user.get("role")
    user_id = user.get("id")

    conn = get_db()

    if role == "supervisor":
        rows = conn.execute(
            """
            SELECT team_id
            FROM team_supervisors
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    elif role == "employee":
        rows = conn.execute(
            """
            SELECT team_id
            FROM team_members
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    else:
        rows = []

    conn.close()

    return [row["team_id"] for row in rows]

def user_belongs_to_organization(user_id, organization_id):
    """
    Check whether a user belongs to an organization.
    """
    if not user_id or not organization_id:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT 1
        FROM users
        WHERE id = ?
          AND organization_id = ?
        """,
        (user_id, organization_id)
    ).fetchone()

    conn.close()

    return row is not None


def zone_belongs_to_organization(zone_id, organization_id):
    """
    Check whether a zone belongs to the current organization.

    Zones currently inherit organization ownership through
    their location.
    """
    if not zone_id or not organization_id:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT 1
        FROM zones z
        JOIN locations l
            ON l.id = z.location_id
        WHERE z.id = ?
          AND l.organization_id = ?
        """,
        (zone_id, organization_id)
    ).fetchone()

    conn.close()

    return row is not None


def zone_belongs_to_user_team(zone_id, user_id):
    """
    Check whether a zone is assigned to at least one team
    belonging to the specified user.

    This is the team-level authorization boundary for
    supervisors and employees.
    """
    if not zone_id or not user_id:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT 1
        FROM team_zones tz
        JOIN team_supervisors ts
            ON ts.team_id = tz.team_id
        WHERE tz.zone_id = ?
          AND ts.user_id = ?

        UNION

        SELECT 1
        FROM team_zones tz
        JOIN team_members tm
            ON tm.team_id = tz.team_id
        WHERE tz.zone_id = ?
          AND tm.user_id = ?
        LIMIT 1
        """,
        (zone_id, user_id, zone_id, user_id)
    ).fetchone()

    conn.close()

    return row is not None



def user_belongs_to_team(user_id, team_id):
    """
    Check whether a user belongs to a specific team.

    Works for both supervisors and employees.
    """
    if not user_id or not team_id:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT 1
        FROM team_supervisors
        WHERE user_id = ?
          AND team_id = ?

        UNION

        SELECT 1
        FROM team_members
        WHERE user_id = ?
          AND team_id = ?

        LIMIT 1
        """,
        (user_id, team_id, user_id, team_id)
    ).fetchone()

    conn.close()

    return row is not None


def require_account_status(user):
    """
    Verify that the account is active.

    Pending, rejected and suspended accounts must not be
    allowed to operate inside the application.
    """
    if not user:
        return False

    return user.get("account_status") == "active"


def organization_scope_required():
    """
    Return the current organization ID.

    This helper exists so every future organization-scoped
    route has one consistent source of truth.
    """
    user = get_current_user()

    if not user:
        return None

    return user.get("organization_id")

def require_roles(*roles):
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            if claims.get("role") not in roles:
                return jsonify(error="Insufficient permissions"), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def gen_qr_b64(data: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def simulate_ai_score():
    score = round(70 + random.random() * 30, 1)
    if score >= 90:   feedback = "Excellent — bathroom is spotlessly clean."
    elif score >= 80: feedback = "Good — minor improvements possible near sink area."
    elif score >= 70: feedback = "Acceptable — floor and fixtures need more attention."
    else:             feedback = "Needs re-cleaning — multiple areas below standard."
    return score, feedback

# ─── auth ────────────────────────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify(
            error="Email and password required"
        ), 400

    conn = get_db()

    user = row_to_dict(
        conn.execute(
            """
            SELECT
                u.*,
                o.name AS organization_name,
                o.organization_code
            FROM users u
            LEFT JOIN organizations o
                ON o.id = u.organization_id
            WHERE LOWER(u.email) = ?
            """,
            (email,)
        ).fetchone()
    )

    conn.close()

    # Do not reveal whether the email exists.
    if not user:
        return jsonify(
            error="Invalid credentials"
        ), 401

    # Account status check.
    if user.get("account_status") != "active":
        status = user.get("account_status") or "pending"

        if status == "pending":
            return jsonify(
                error="Your account is awaiting approval."
            ), 403

        if status == "rejected":
            return jsonify(
                error="Your account request was rejected."
            ), 403

        if status == "suspended":
            return jsonify(
                error="Your account has been suspended."
            ), 403

        return jsonify(
            error="Your account is not active."
        ), 403

    # Keep the old is_active flag as an additional safety check.
    if not user.get("is_active"):
        return jsonify(
            error="Your account is not active."
        ), 403

    # Password must exist.
    if not user.get("password_hash"):
        return jsonify(
            error="Invalid credentials"
        ), 401

    stored_hash = user["password_hash"]
    password_valid = False
    legacy_hash = False

    # Try the current bcrypt format first.
    try:
        password_valid = bcrypt.checkpw(
            password.encode(),
            stored_hash.encode()
        )
    except (ValueError, TypeError):
        password_valid = False

    # If bcrypt fails, try an older Werkzeug hash.
    if not password_valid:
        try:
            password_valid = check_password_hash(
                stored_hash,
                password
            )
            legacy_hash = password_valid
        except (ValueError, TypeError):
            password_valid = False

    # Password is wrong.
    if not password_valid:
        return jsonify(
            error="Invalid credentials"
        ), 401

    # Convert an old Werkzeug hash to bcrypt after successful login.
    if legacy_hash:
        new_hash = bcrypt.hashpw(
            password.encode(),
            bcrypt.gensalt()
        ).decode()

        conn = get_db()

        conn.execute(
            """
            UPDATE users
            SET password_hash = ?
            WHERE id = ?
            """,
            (new_hash, user["id"])
        )

        conn.commit()
        conn.close()

    # Get team scope for the JWT.
    user_id = user["id"]
    role = user["role"]

    conn = get_db()

    if role == "supervisor":
        team_rows = conn.execute(
            """
            SELECT team_id
            FROM team_supervisors
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    elif role == "employee":
        team_rows = conn.execute(
            """
            SELECT team_id
            FROM team_members
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    else:
        team_rows = []

    conn.close()

    team_ids = [
        row["team_id"]
        for row in team_rows
    ]

    # JWT contains identity + authorization context.
    token = create_access_token(
        identity=user_id,
        additional_claims={
            "user_id": user_id,
            "organization_id": user.get("organization_id"),
            "role": role,
            "name": user.get("name"),
            "email": user.get("email"),
            "location_id": user.get("location_id"),
            "team_ids": team_ids
        }
    )

    return jsonify(
        token=token,
        user={
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "location_id": user.get("location_id"),
            "organization_id": user.get("organization_id"),
            "organization_name": user.get("organization_name"),
            "organization_code": user.get("organization_code"),
            "account_status": user.get("account_status"),
            "team_ids": team_ids
        }
    )


@jwt_required()
def me():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    team_ids = get_current_team_ids()

    return jsonify({
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "role": user.get("role"),
        "location_id": user.get("location_id"),
        "organization_id": user.get("organization_id"),
        "organization_name": user.get("organization_name"),
        "organization_code": user.get("organization_code"),
        "account_status": user.get("account_status"),
        "employee_id": user.get("employee_id"),
        "notification_email": user.get("notification_email"),
        "team_ids": team_ids,
        "created_at": user.get("created_at")
    })
@app.route("/api/auth/register", methods=["POST"])
def register():
    """
    Unified registration endpoint.

    Admin:
        Creates a new organization and an active admin account.

    Supervisor / Employee:
        Joins an existing organization using organization_code
        and creates a pending account request.
    """

    data = request.get_json() or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    phone = (data.get("phone") or "").strip() or None
    notification_email = (
        (data.get("notification_email") or "").strip().lower()
        or None
    )

    role = (data.get("role") or "").strip().lower()

    if not name or not email or not password or not role:
        return jsonify(
            error="name, email, password and role are required"
        ), 400

    if role not in ("admin", "supervisor", "employee"):
        return jsonify(
            error="Invalid role. Choose admin, supervisor or employee."
        ), 400

    if len(password) < 8:
        return jsonify(
            error="Password must be at least 8 characters."
        ), 400

    # ---------------------------------------------------------
    # COMMON EMAIL CHECK
    # ---------------------------------------------------------

    conn = get_db()

    existing_user = conn.execute(
        """
        SELECT id
        FROM users
        WHERE LOWER(email) = ?
        """,
        (email,)
    ).fetchone()

    if existing_user:
        conn.close()
        return jsonify(
            error="Email is already registered."
        ), 409

    existing_request = conn.execute(
        """
        SELECT id
        FROM account_requests
        WHERE LOWER(email) = ?
          AND status = 'pending'
        """,
        (email,)
    ).fetchone()

    if existing_request:
        conn.close()
        return jsonify(
            error="A pending account request already exists for this email."
        ), 409

    # ---------------------------------------------------------
    # PASSWORD HASH
    # ---------------------------------------------------------

    password_hash = bcrypt.hashpw(
        password.encode(),
        bcrypt.gensalt()
    ).decode()

    # =========================================================
    # ADMIN REGISTRATION
    # =========================================================

    if role == "admin":

        organization_name = (
            data.get("organization_name") or ""
        ).strip()

        if not organization_name:
            conn.close()
            return jsonify(
                error="organization_name is required for admin registration."
            ), 400

        organization_email = (
            (data.get("organization_email") or "").strip().lower()
            or email
        )

        organization_phone = (
            (data.get("organization_phone") or "").strip()
            or phone
        )

        organization_address = (
            (data.get("organization_address") or "").strip()
            or None
        )

        organization_city = (
            (data.get("organization_city") or "").strip()
            or None
        )

        organization_country = (
            (data.get("organization_country") or "").strip()
            or None
        )

        # Generate a unique organization code.
        for _ in range(20):
            organization_code = (
                "CT-"
                + uuid.uuid4().hex[:8].upper()
            )

            exists = conn.execute(
                """
                SELECT id
                FROM organizations
                WHERE organization_code = ?
                """,
                (organization_code,)
            ).fetchone()

            if not exists:
                break
        else:
            conn.close()
            return jsonify(
                error="Could not generate a unique organization code."
            ), 500

        organization_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        try:
            conn.execute("BEGIN")

            # Create organization.
            conn.execute(
                """
                INSERT INTO organizations (
                    id,
                    name,
                    email,
                    phone,
                    address,
                    city,
                    country,
                    organization_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    organization_id,
                    organization_name,
                    organization_email,
                    organization_phone,
                    organization_address,
                    organization_city,
                    organization_country,
                    organization_code
                )
            )

            # Create active administrator.
            conn.execute(
                """
                INSERT INTO users (
                    id,
                    name,
                    email,
                    notification_email,
                    phone,
                    password_hash,
                    role,
                    location_id,
                    is_active,
                    organization_id,
                    account_status,
                    approved_by,
                    approved_at,
                    employee_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    user_id,
                    name,
                    email,
                    notification_email,
                    phone,
                    password_hash,
                    "admin",
                    None,
                    1,
                    organization_id,
                    "active",
                    user_id,
                    None
                )
            )

            conn.commit()

        except Exception:
            conn.rollback()
            conn.close()
            raise

        conn.close()

        # Create login token immediately.
        token = create_access_token(
            identity=user_id,
            additional_claims={
                "user_id": user_id,
                "organization_id": organization_id,
                "role": "admin",
                "name": name,
                "email": email,
                "location_id": None,
                "team_ids": []
            }
        )

        return jsonify(
            message="Admin account created successfully.",
            token=token,
            organization={
                "id": organization_id,
                "name": organization_name,
                "organization_code": organization_code
            },
            user={
                "id": user_id,
                "name": name,
                "email": email,
                "role": "admin",
                "organization_id": organization_id,
                "organization_name": organization_name,
                "organization_code": organization_code,
                "account_status": "active",
                "team_ids": []
            }
        ), 201

    # =========================================================
    # SUPERVISOR / EMPLOYEE JOIN REQUEST
    # =========================================================

    organization_code = (
        data.get("organization_code") or ""
    ).strip().upper()

    if not organization_code:
        conn.close()
        return jsonify(
            error="organization_code is required."
        ), 400

    organization = conn.execute(
        """
        SELECT *
        FROM organizations
        WHERE UPPER(organization_code) = ?
        """,
        (organization_code,)
    ).fetchone()

    if not organization:
        conn.close()
        return jsonify(
            error="Invalid organization code."
        ), 404

    organization = row_to_dict(organization)
    organization_id = organization["id"]

    requested_team_id = (
        data.get("requested_team_id") or ""
    ).strip() or None

    employee_id = (
        (data.get("employee_id") or "").strip()
        or None
    )

       # ---------------------------------------------------------
    # TEAM VALIDATION
    # ---------------------------------------------------------
    if requested_team_id:

        team = conn.execute(
            """
            SELECT id, name
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                requested_team_id,
                organization_id
            )
        ).fetchone()

        if not team:
            conn.close()
            return jsonify(
                error="Requested team does not exist in this organization."
            ), 400
    # Employee ID must be unique inside the organization.
    if employee_id:

        existing_employee_id = conn.execute(
            """
            SELECT id
            FROM users
            WHERE organization_id = ?
              AND employee_id = ?
            """,
            (
                organization_id,
                employee_id
            )
        ).fetchone()

        if existing_employee_id:
            conn.close()
            return jsonify(
                error="Employee ID is already in use."
            ), 409

        existing_request_employee_id = conn.execute(
            """
            SELECT id
            FROM account_requests
            WHERE organization_id = ?
              AND employee_id = ?
              AND status = 'pending'
            """,
            (
                organization_id,
                employee_id
            )
        ).fetchone()

        if existing_request_employee_id:
            conn.close()
            return jsonify(
                error="A pending request already exists for this employee ID."
            ), 409

    request_id = str(uuid.uuid4())

    try:
        conn.execute(
            """
            INSERT INTO account_requests (
                id,
                organization_id,
                name,
                email,
                phone,
                notification_email,
                password_hash,
                requested_role,
                employee_id,
                requested_team_id,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                request_id,
                organization_id,
                name,
                email,
                phone,
                notification_email,
                password_hash,
                role,
                employee_id,
                requested_team_id
            )
        )

        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()

    return jsonify(
        message="Account request submitted successfully.",
        status="pending",
        request_id=request_id,
        organization={
            "id": organization_id,
            "name": organization["name"],
            "organization_code": organization["organization_code"]
        },
        role=role
    ), 201


# ============================================================
# ADMIN ACCOUNT REQUEST MANAGEMENT
# ============================================================

@app.route("/api/admin/account-requests", methods=["GET"])
@jwt_required()
@require_roles("admin")
def get_account_requests():
    """
    Return account requests belonging only to the current
    administrator's organization.
    """

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    organization_id = admin.get("organization_id")

    if not organization_id:
        return jsonify(error="Administrator has no organization"), 400

    status = (request.args.get("status") or "").strip().lower()

    conn = get_db()

    query = """
        SELECT
            ar.id,
            ar.organization_id,
            ar.name,
            ar.email,
            ar.phone,
            ar.notification_email,
            ar.requested_role,
            ar.employee_id,
            ar.requested_team_id,
            t.name AS requested_team_name,
            ar.status,
            ar.reviewed_by,
            ar.reviewed_at,
            ar.rejection_reason,
            ar.created_at
        FROM account_requests ar
        LEFT JOIN teams t
            ON t.id = ar.requested_team_id
        WHERE ar.organization_id = ?
    """

    params = [organization_id]

    if status:
        if status not in (
            "pending",
            "waiting",
            "approved",
            "rejected"
        ):
            conn.close()
            return jsonify(
                error="Invalid request status"
            ), 400

        query += " AND ar.status = ?"
        params.append(status)


    query += " ORDER BY ar.created_at DESC"

    rows = rows_to_list(
        conn.execute(
            query,
            params
        ).fetchall()
    )

    conn.close()

    return jsonify(rows)

@app.route(
    "/api/admin/account-requests/<request_id>/approve",
    methods=["POST"]
)
@jwt_required()
@require_roles("admin")
def approve_account_request(request_id):

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    organization_id = admin.get("organization_id")
    admin_id = admin.get("id")

    if not organization_id:
        return jsonify(
            error="Administrator has no organization"
        ), 400

    data = request.get_json() or {}

    selected_team_id = (
        str(data.get("team_id") or "").strip()
        or None
    )

    selected_role = (
        str(data.get("role") or "").strip().lower()
        or None
    )

    selected_location_id = (
        str(data.get("location_id") or "").strip()
        or None
    )

    if selected_role and selected_role not in (
        "supervisor",
        "employee"
    ):
        return jsonify(
            error="Approved role must be supervisor or employee"
        ), 400

    conn = get_db()

    try:

        # -----------------------------------------------------
        # Find request inside admin's organization
        # -----------------------------------------------------

        request_row = conn.execute(
            """
            SELECT *
            FROM account_requests
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                request_id,
                organization_id
            )
        ).fetchone()

        if not request_row:
            return jsonify(
                error="Account request not found"
            ), 404

        account_request = row_to_dict(
            request_row
        )

        # -----------------------------------------------------
        # Request must still be pending or waiting
        # -----------------------------------------------------

        if account_request["status"] not in (
            "pending",
            "waiting"
        ):
            return jsonify(
                error="This account request has already been processed."
            ), 409

        # -----------------------------------------------------
        # Determine final role
        # -----------------------------------------------------

        requested_role = (
            account_request["requested_role"]
        )

        final_role = (
            selected_role
            if selected_role
            else requested_role
        )

        if final_role not in (
            "supervisor",
            "employee"
        ):
            return jsonify(
                error="Invalid requested role."
            ), 400

        # -----------------------------------------------------
        # Validate selected location
        # -----------------------------------------------------

        if selected_location_id:

            location = conn.execute(
                """
                SELECT id
                FROM locations
                WHERE id = ?
                  AND organization_id = ?
                """,
                (
                    selected_location_id,
                    organization_id
                )
            ).fetchone()

            if not location:
                return jsonify(
                    error="Selected location does not exist in this organization."
                ), 400

        # -----------------------------------------------------
        # Determine final team
        # -----------------------------------------------------

        effective_team_id = (
            selected_team_id
            if selected_team_id
            else account_request["requested_team_id"]
        )

        # -----------------------------------------------------
        # Validate final team
        # -----------------------------------------------------

        if effective_team_id:

            team = conn.execute(
                """
                SELECT id
                FROM teams
                WHERE id = ?
                  AND organization_id = ?
                """,
                (
                    effective_team_id,
                    organization_id
                )
            ).fetchone()

            if not team:
                return jsonify(
                    error="Selected team does not exist in this organization."
                ), 400

        # -----------------------------------------------------
        # Prevent duplicate email
        # -----------------------------------------------------

        existing_user = conn.execute(
            """
            SELECT id
            FROM users
            WHERE LOWER(email) = ?
            """,
            (
                account_request["email"].lower(),
            )
        ).fetchone()

        if existing_user:
            return jsonify(
                error="A user with this email already exists."
            ), 409

        # -----------------------------------------------------
        # Prevent duplicate employee ID
        # -----------------------------------------------------

        employee_id = account_request[
            "employee_id"
        ]

        if employee_id:

            existing_employee = conn.execute(
                """
                SELECT id
                FROM users
                WHERE organization_id = ?
                  AND employee_id = ?
                """,
                (
                    organization_id,
                    employee_id
                )
            ).fetchone()

            if existing_employee:
                return jsonify(
                    error="Employee ID is already in use."
                ), 409

        # -----------------------------------------------------
        # Create user
        # -----------------------------------------------------

        user_id = str(uuid.uuid4())

        conn.execute("BEGIN")

        conn.execute(
            """
            INSERT INTO users (
                id,
                name,
                email,
                notification_email,
                phone,
                password_hash,
                role,
                location_id,
                is_active,
                organization_id,
                account_status,
                approved_by,
                approved_at,
                employee_id
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?
            )
            """,
            (
                user_id,
                account_request["name"],
                account_request["email"],
                account_request["notification_email"],
                account_request["phone"],
                account_request["password_hash"],
                final_role,
                selected_location_id,
                1,
                organization_id,
                "active",
                admin_id,
                employee_id
            )
        )

        # -----------------------------------------------------
        # Assign final team
        # -----------------------------------------------------

        if effective_team_id:

            relationship_id = str(
                uuid.uuid4()
            )

            if final_role == "supervisor":

                conn.execute(
                    """
                    INSERT INTO team_supervisors (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        relationship_id,
                        effective_team_id,
                        user_id
                    )
                )

            elif final_role == "employee":

                conn.execute(
                    """
                    INSERT INTO team_members (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        relationship_id,
                        effective_team_id,
                        user_id
                    )
                )

        # -----------------------------------------------------
        # Mark request approved
        # -----------------------------------------------------

        conn.execute(
            """
            UPDATE account_requests
            SET status = 'approved',
                reviewed_by = ?,
                reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                admin_id,
                request_id,
                organization_id
            )
        )

        conn.commit()

        return jsonify(
            message="Account request approved successfully.",
            user={
                "id": user_id,
                "name": account_request["name"],
                "email": account_request["email"],
                "role": final_role,
                "organization_id": organization_id,
                "account_status": "active",
                "team_id": effective_team_id,
                "location_id": selected_location_id
            }
        ), 201

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
#wait--------------------------------------------------------------------------

@app.route(
    "/api/admin/account-requests/<request_id>/wait",
    methods=["POST"]
)
@jwt_required()
@require_roles("admin")
def wait_account_request(request_id):

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    organization_id = admin.get("organization_id")
    admin_id = admin.get("id")

    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT id, status
            FROM account_requests
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                request_id,
                organization_id
            )
        ).fetchone()

        if not row:
            return jsonify(
                error="Account request not found"
            ), 404

        if row["status"] != "pending":
            return jsonify(
                error="Only pending requests can be moved to waiting"
            ), 409

        conn.execute(
            """
            UPDATE account_requests
            SET status = 'waiting',
                reviewed_by = ?,
                reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                admin_id,
                request_id,
                organization_id
            )
        )

        conn.commit()

        return jsonify(
            message="Account request moved to waiting."
        )

    except Exception as e:
        conn.rollback()
        raise

    finally:
        conn.close()
#reject------------------------------------------------------------------------
@app.route(
    "/api/admin/account-requests/<request_id>/reject",
    methods=["POST"]
)
@jwt_required()
@require_roles("admin")
def reject_account_request(request_id):
    """
    Reject a pending account request belonging to the
    current administrator's organization.
    """

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    organization_id = admin.get("organization_id")
    admin_id = admin.get("id")

    if not organization_id:
        return jsonify(error="Administrator has no organization"), 400

    data = request.get_json() or {}

    rejection_reason = (
        data.get("reason") or ""
    ).strip() or None

    conn = get_db()

    request_row = conn.execute(
        """
        SELECT id, status
        FROM account_requests
        WHERE id = ?
          AND organization_id = ?
        """,
        (
            request_id,
            organization_id
        )
    ).fetchone()

    if not request_row:
        conn.close()
        return jsonify(error="Account request not found"), 404

    if request_row["status"] != "pending":
        conn.close()
        return jsonify(
            error="This account request has already been processed."
        ), 409

    conn.execute(
        """
        UPDATE account_requests
        SET status = 'rejected',
            reviewed_by = ?,
            reviewed_at = CURRENT_TIMESTAMP,
            rejection_reason = ?
        WHERE id = ?
          AND organization_id = ?
        """,
        (
            admin_id,
            rejection_reason,
            request_id,
            organization_id
        )
    )

    conn.commit()
    conn.close()

    return jsonify(
        message="Account request rejected successfully."
    )

# ============================================================
# TEAM MANAGEMENT
# ============================================================

@app.route("/api/teams", methods=["GET"])
@jwt_required()
def get_teams():
    """
    Return teams visible to the current user.

    Admin:
        All teams in their organization.

    Supervisor:
        Teams they supervise.

    Employee:
        Teams they belong to.
    """

    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    organization_id = user.get("organization_id")
    user_id = user.get("id")
    role = user.get("role")

    if not organization_id:
        return jsonify(error="User has no organization"), 400

    conn = get_db()

    if role == "admin":

        rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.description,
                    t.organization_id,
                    t.created_at,
                    COUNT(DISTINCT tm.user_id) AS member_count,
                    COUNT(DISTINCT ts.user_id) AS supervisor_count
                FROM teams t
                LEFT JOIN team_members tm
                    ON tm.team_id = t.id
                LEFT JOIN team_supervisors ts
                    ON ts.team_id = t.id
                WHERE t.organization_id = ?
                GROUP BY t.id
                ORDER BY t.name
                """,
                (organization_id,)
            ).fetchall()
        )

    elif role == "supervisor":

        rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.description,
                    t.organization_id,
                    t.created_at,
                    COUNT(DISTINCT tm.user_id) AS member_count,
                    COUNT(DISTINCT ts.user_id) AS supervisor_count
                FROM teams t
                JOIN team_supervisors ts_current
                    ON ts_current.team_id = t.id
                   AND ts_current.user_id = ?
                LEFT JOIN team_members tm
                    ON tm.team_id = t.id
                LEFT JOIN team_supervisors ts
                    ON ts.team_id = t.id
                WHERE t.organization_id = ?
                GROUP BY t.id
                ORDER BY t.name
                """,
                (
                    user_id,
                    organization_id
                )
            ).fetchall()
        )

    elif role == "employee":

        rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.description,
                    t.organization_id,
                    t.created_at,
                    COUNT(DISTINCT tm.user_id) AS member_count,
                    COUNT(DISTINCT ts.user_id) AS supervisor_count
                FROM teams t
                JOIN team_members tm_current
                    ON tm_current.team_id = t.id
                   AND tm_current.user_id = ?
                LEFT JOIN team_members tm
                    ON tm.team_id = t.id
                LEFT JOIN team_supervisors ts
                    ON ts.team_id = t.id
                WHERE t.organization_id = ?
                GROUP BY t.id
                ORDER BY t.name
                """,
                (
                    user_id,
                    organization_id
                )
            ).fetchall()
        )

    else:
        conn.close()
        return jsonify(error="Invalid role"), 403

    conn.close()

    return jsonify(rows)


@app.route("/api/teams", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_team():
    """
    Create a team inside the current user's organization.

    Admin:
        Can create any team in their organization.

    Supervisor:
        Can create a team in their organization.
        The creating supervisor is automatically assigned
        as a supervisor of the new team.
    """

    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    organization_id = user.get("organization_id")
    user_id = user.get("id")
    role = user.get("role")

    if not organization_id:
        return jsonify(
            error="User has no organization"
        ), 400

    data = request.get_json() or {}

    name = (data.get("name") or "").strip()
    description = (
        (data.get("description") or "").strip()
        or None
    )

    if not name:
        return jsonify(
            error="Team name is required."
        ), 400

    conn = get_db()

    existing = conn.execute(
        """
        SELECT id
        FROM teams
        WHERE organization_id = ?
          AND LOWER(name) = LOWER(?)
        """,
        (
            organization_id,
            name
        )
    ).fetchone()

    if existing:
        conn.close()
        return jsonify(
            error="A team with this name already exists."
        ), 409

    team_id = str(uuid.uuid4())

    try:
        conn.execute("BEGIN")

        conn.execute(
            """
            INSERT INTO teams (
                id,
                organization_id,
                name,
                description
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                team_id,
                organization_id,
                name,
                description
            )
        )

        # A supervisor who creates a team automatically
        # becomes a supervisor of that team.
        if role == "supervisor":

            relationship_id = str(uuid.uuid4())

            conn.execute(
                """
                INSERT INTO team_supervisors (
                    id,
                    team_id,
                    user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    relationship_id,
                    team_id,
                    user_id
                )
            )

        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()

    return jsonify(
        message="Team created successfully.",
        team={
            "id": team_id,
            "name": name,
            "description": description,
            "organization_id": organization_id
        }
    ), 201

# ============================================================
# ADMIN TEAM SEARCH
# ============================================================
@app.route(
    "/api/admin/teams/<team_id>",
    methods=["PUT"]
)
@jwt_required()
@require_roles("admin")
def update_admin_team(team_id):

    admin = get_current_user()

    if not admin:
        return jsonify(
            error="User not found"
        ), 404

    if not require_account_status(admin):
        return jsonify(
            error="Account is not active.",
            account_status=admin.get("account_status")
        ), 403

    organization_id = admin.get("organization_id")

    if not organization_id:
        return jsonify(
            error="Administrator has no organization"
        ), 403

    data = request.get_json() or {}

    name = (
        data.get("name") or ""
    ).strip()

    description = (
        data.get("description") or ""
    ).strip()

    if not name:
        return jsonify(
            error="Team name is required."
        ), 400

    if not description:
        description = None

    conn = get_db()

    try:

        team = conn.execute(
            """
            SELECT
                id,
                name,
                description,
                organization_id,
                created_at
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                team_id,
                organization_id
            )
        ).fetchone()

        if not team:
            return jsonify(
                error="Team not found"
            ), 404

        duplicate = conn.execute(
            """
            SELECT id
            FROM teams
            WHERE organization_id = ?
              AND LOWER(name) = LOWER(?)
              AND id != ?
            """,
            (
                organization_id,
                name,
                team_id
            )
        ).fetchone()

        if duplicate:
            return jsonify(
                error="A team with this name already exists."
            ), 409

        conn.execute(
            """
            UPDATE teams
            SET name = ?,
                description = ?
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                name,
                description,
                team_id,
                organization_id
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT
                id,
                name,
                description,
                organization_id,
                created_at
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                team_id,
                organization_id
            )
        ).fetchone()

        return jsonify(
            team=row_to_dict(updated)
        ), 200

    except Exception as error:

        conn.rollback()

        return jsonify(
            error=str(error)
        ), 500

    finally:

        conn.close()


@app.route(
    "/api/admin/teams/<team_id>/people",
    methods=["PUT"]
)
@jwt_required()
@require_roles("admin")
def update_admin_team_people(team_id):

    admin = get_current_user()

    if not admin:
        return jsonify(
            error="User not found"
        ), 404

    if not require_account_status(admin):
        return jsonify(
            error="Account is not active.",
            account_status=admin.get("account_status")
        ), 403

    organization_id = admin.get("organization_id")

    if not organization_id:
        return jsonify(
            error="Administrator has no organization"
        ), 403

    data = request.get_json() or {}

    supervisor_ids = data.get(
        "supervisor_ids",
        []
    )

    employee_ids = data.get(
        "employee_ids",
        []
    )

    if not isinstance(supervisor_ids, list):
        return jsonify(
            error="supervisor_ids must be an array."
        ), 400

    if not isinstance(employee_ids, list):
        return jsonify(
            error="employee_ids must be an array."
        ), 400

    supervisor_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in supervisor_ids
            if str(value).strip()
        )
    )

    employee_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in employee_ids
            if str(value).strip()
        )
    )

    overlapping_ids = (
        set(supervisor_ids)
        & set(employee_ids)
    )

    if overlapping_ids:
        return jsonify(
            error=(
                "A user cannot be both a supervisor "
                "and an employee on the same team."
            )
        ), 400

    conn = get_db()

    try:

        team = conn.execute(
            """
            SELECT id
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                team_id,
                organization_id
            )
        ).fetchone()

        if not team:
            return jsonify(
                error="Team not found"
            ), 404

        all_ids = [
            *supervisor_ids,
            *employee_ids
        ]

        users = []

        if all_ids:

            placeholders = ",".join(
                ["?"] * len(all_ids)
            )

            users = conn.execute(
                f"""
                SELECT
                    id,
                    role,
                    is_active,
                    account_status
                FROM users
                WHERE organization_id = ?
                  AND id IN ({placeholders})
                """,
                (
                    organization_id,
                    *all_ids
                )
            ).fetchall()

        found_ids = {
            str(row["id"])
            for row in users
        }

        missing_ids = [
            user_id
            for user_id in all_ids
            if user_id not in found_ids
        ]

        if missing_ids:
            return jsonify(
                error=(
                    "One or more selected users do not "
                    "belong to your organization."
                )
            ), 400

        users_by_id = {
            str(row["id"]): row
            for row in users
        }

        invalid_supervisors = [
            user_id
            for user_id in supervisor_ids
            if users_by_id[user_id]["role"] != "supervisor"
        ]

        if invalid_supervisors:
            return jsonify(
                error=(
                    "Only supervisor accounts can be "
                    "assigned as team supervisors."
                )
            ), 400

        invalid_employees = [
            user_id
            for user_id in employee_ids
            if users_by_id[user_id]["role"] != "employee"
        ]

        if invalid_employees:
            return jsonify(
                error=(
                    "Only employee accounts can be "
                    "assigned as team employees."
                )
            ), 400

        conn.execute("BEGIN")

        conn.execute(
            """
            DELETE FROM team_supervisors
            WHERE team_id = ?
            """,
            (
                team_id,
            )
        )

        conn.execute(
            """
            DELETE FROM team_members
            WHERE team_id = ?
            """,
            (
                team_id,
            )
        )

        for user_id in supervisor_ids:

            conn.execute(
                """
                INSERT INTO team_supervisors (
                    id,
                    team_id,
                    user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    team_id,
                    user_id
                )
            )

        for user_id in employee_ids:

            conn.execute(
                """
                INSERT INTO team_members (
                    id,
                    team_id,
                    user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    team_id,
                    user_id
                )
            )

        conn.commit()

        return jsonify(
            message="Team people updated successfully.",
            team_id=team_id,
            supervisor_ids=supervisor_ids,
            employee_ids=employee_ids
        ), 200

    except Exception as error:

        conn.rollback()

        return jsonify(
            error=str(error)
        ), 500

    finally:

        conn.close()

@app.route(
    "/api/admin/teams/search",
    methods=["GET"]
)
@jwt_required()
@require_roles("admin")
def search_admin_teams():

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    if not require_account_status(admin):
        return jsonify(
            error="Account is not active"
        ), 403

    organization_id = admin.get("organization_id")

    if not organization_id:
        return jsonify(
            error="Administrator has no organization"
        ), 400

    query_text = (
        request.args.get("q") or ""
    ).strip()

    if not query_text:
        return jsonify([])

    try:
        limit = int(
            request.args.get("limit") or 8
        )
    except ValueError:
        limit = 8

    limit = max(1, min(limit, 20))

    search_value = f"%{query_text.lower()}%"

    conn = get_db()

    try:

        rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.description,
                    t.organization_id,
                    t.created_at,

                    (
                        SELECT COUNT(*)
                        FROM team_supervisors ts_count
                        WHERE ts_count.team_id = t.id
                    ) AS supervisor_count,

                    (
                        SELECT COUNT(*)
                        FROM team_members tm_count
                        WHERE tm_count.team_id = t.id
                    ) AS employee_count,

                    (
                        SELECT COUNT(*)
                        FROM team_locations tl_count
                        WHERE tl_count.team_id = t.id
                    ) AS location_count,

                    (
                        SELECT COUNT(*)
                        FROM team_zones tz_count
                        WHERE tz_count.team_id = t.id
                    ) AS zone_count

                FROM teams t

                WHERE t.organization_id = ?

                  AND (
                        LOWER(
                            COALESCE(t.name, '')
                        ) LIKE ?

                        OR

                        LOWER(
                            COALESCE(t.description, '')
                        ) LIKE ?

                        OR EXISTS (
                            SELECT 1
                            FROM team_supervisors ts
                            JOIN users u
                                ON u.id = ts.user_id
                            WHERE ts.team_id = t.id
                              AND u.organization_id = ?
                              AND LOWER(
                                  COALESCE(u.name, '')
                              ) LIKE ?
                        )

                        OR EXISTS (
                            SELECT 1
                            FROM team_members tm
                            JOIN users u
                                ON u.id = tm.user_id
                            WHERE tm.team_id = t.id
                              AND u.organization_id = ?
                              AND LOWER(
                                  COALESCE(u.name, '')
                              ) LIKE ?
                        )

                        OR EXISTS (
                            SELECT 1
                            FROM team_locations tl
                            JOIN locations l
                                ON l.id = tl.location_id
                            WHERE tl.team_id = t.id
                              AND l.organization_id = ?
                              AND LOWER(
                                  COALESCE(l.name, '')
                              ) LIKE ?
                        )

                        OR EXISTS (
                            SELECT 1
                            FROM team_zones tz
                            JOIN zones z
                                ON z.id = tz.zone_id
                            JOIN locations l
                                ON l.id = z.location_id
                            WHERE tz.team_id = t.id
                              AND l.organization_id = ?
                              AND LOWER(
                                  COALESCE(z.name, '')
                              ) LIKE ?
                        )
                  )

                ORDER BY
                    CASE
                        WHEN LOWER(t.name) = ?
                            THEN 0
                        WHEN LOWER(t.name) LIKE ?
                            THEN 1
                        ELSE 2
                    END,
                    LOWER(t.name)

                LIMIT ?
                """,
                (
                    organization_id,

                    search_value,
                    search_value,

                    organization_id,
                    search_value,

                    organization_id,
                    search_value,

                    organization_id,
                    search_value,

                    organization_id,
                    search_value,

                    query_text.lower(),
                    f"{query_text.lower()}%",

                    limit
                )
            ).fetchall()
        )

        return jsonify(rows)

    finally:
        conn.close()

# ============================================================
# ADMIN TEAM DETAIL
# ============================================================

@app.route(
    "/api/admin/teams/<team_id>",
    methods=["GET"]
)
@jwt_required()
@require_roles("admin")
def get_admin_team_detail(team_id):

    admin = get_current_user()

    if not admin:
        return jsonify(error="User not found"), 404

    if not require_account_status(admin):
        return jsonify(
            error="Account is not active"
        ), 403

    organization_id = admin.get(
        "organization_id"
    )

    if not organization_id:
        return jsonify(
            error="Administrator has no organization"
        ), 400

    team_id = str(team_id).strip()

    conn = get_db()

    try:

        team = conn.execute(
            """
            SELECT
                id,
                name,
                description,
                organization_id,
                created_at
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                team_id,
                organization_id
            )
        ).fetchone()

        if not team:
            return jsonify(
                error="Team not found"
            ), 404

        team = row_to_dict(team)

        supervisors = rows_to_list(
            conn.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.phone,
                    u.employee_id,
                    u.role,
                    u.is_active,
                    u.account_status,
                    u.created_at
                FROM team_supervisors ts
                JOIN users u
                    ON u.id = ts.user_id
                WHERE ts.team_id = ?
                  AND u.organization_id = ?
                  AND u.role = 'supervisor'
                ORDER BY u.name
                """,
                (
                    team_id,
                    organization_id
                )
            ).fetchall()
        )

        employees = rows_to_list(
            conn.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.phone,
                    u.employee_id,
                    u.role,
                    u.is_active,
                    u.account_status,
                    u.created_at
                FROM team_members tm
                JOIN users u
                    ON u.id = tm.user_id
                WHERE tm.team_id = ?
                  AND u.organization_id = ?
                  AND u.role = 'employee'
                ORDER BY u.name
                """,
                (
                    team_id,
                    organization_id
                )
            ).fetchall()
        )

        locations = rows_to_list(
            conn.execute(
                """
                SELECT
                    l.*
                FROM team_locations tl
                JOIN locations l
                    ON l.id = tl.location_id
                WHERE tl.team_id = ?
                  AND l.organization_id = ?
                ORDER BY l.name
                """,
                (
                    team_id,
                    organization_id
                )
            ).fetchall()
        )

        zones = rows_to_list(
            conn.execute(
                """
                SELECT
                    z.*,
                    l.name AS location_name
                FROM team_zones tz
                JOIN zones z
                    ON z.id = tz.zone_id
                JOIN locations l
                    ON l.id = z.location_id
                WHERE tz.team_id = ?
                  AND l.organization_id = ?
                ORDER BY z.name
                """,
                (
                    team_id,
                    organization_id
                )
            ).fetchall()
        )

        return jsonify(
            team=team,
            supervisors=supervisors,
            employees=employees,
            locations=locations,
            zones=zones
        )

    finally:
        conn.close()
# ─── locations ────────────────────────────────────────────────────────────────
@app.route("/api/locations", methods=["GET"])
@jwt_required()
def get_locations():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        if role == "admin":
            rows = conn.execute("""
                SELECT *
                FROM locations
                WHERE organization_id = ?
                ORDER BY name
            """, (organization_id,)).fetchall()

        elif role in ("supervisor", "employee"):
            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            rows = conn.execute(f"""
                SELECT DISTINCT l.*
                FROM locations l
                JOIN team_locations tl
                    ON tl.location_id = l.id
                JOIN teams t
                    ON t.id = tl.team_id
                WHERE l.organization_id = ?
                  AND t.organization_id = ?
                  AND tl.team_id IN ({placeholders})
                ORDER BY l.name
            """, (
                organization_id,
                organization_id,
                *team_ids
            )).fetchall()

        else:
            return jsonify(error="Invalid role"), 403

        return jsonify(rows_to_list(rows))

    finally:
        conn.close()
#create-locations**************************************************************
@app.route("/api/locations", methods=["POST"])
@jwt_required()
def create_location():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if role not in ("admin", "supervisor"):
        return jsonify(error="Only admins and supervisors can create locations"), 403

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    d = request.get_json() or {}

    if not d.get("name"):
        return jsonify(error="name required"), 400

    team_id = d.get("team_id")

    conn = get_db()

    try:
        # -------------------------------------------------
        # Validate team assignment
        # -------------------------------------------------
        if team_id:
            team = conn.execute("""
                SELECT id
                FROM teams
                WHERE id = ?
                  AND organization_id = ?
            """, (team_id, organization_id)).fetchone()

            if not team:
                return jsonify(error="Invalid team for this organization"), 400

            # Supervisor can only assign locations to their own teams
            if role == "supervisor" and team_id not in team_ids:
                return jsonify(
                    error="You can only assign locations to your own team"
                ), 403

        # -------------------------------------------------
        # Create location
        # -------------------------------------------------
        location_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO locations (
                id,
                name,
                address,
                city,
                country,
                organization_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            location_id,
            d["name"],
            d.get("address"),
            d.get("city"),
            d.get("country"),
            organization_id
        ))

        # -------------------------------------------------
        # Associate location with team
        # -------------------------------------------------
        if team_id:
            conn.execute("""
                INSERT INTO team_locations (
                    team_id,
                    location_id
                )
                VALUES (?, ?)
            """, (team_id, location_id))

        conn.commit()

        return jsonify({
            "id": location_id,
            "name": d["name"],
            "organization_id": organization_id,
            "team_id": team_id
        }), 201

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
#update-locations**************************************************************
@app.route("/api/locations/<lid>", methods=["PUT"])
@jwt_required()
def update_location(lid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if role not in ("admin", "supervisor"):
        return jsonify(
            error="Only admins and supervisors can update locations"
        ), 403

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    d = request.get_json() or {}

    conn = get_db()

    try:
        # -------------------------------------------------
        # Find location inside the user's organization
        # -------------------------------------------------
        location = conn.execute("""
            SELECT id
            FROM locations
            WHERE id = ?
              AND organization_id = ?
        """, (lid, organization_id)).fetchone()

        if not location:
            return jsonify(error="Location not found"), 404

        # -------------------------------------------------
        # Supervisor can only update team locations
        # -------------------------------------------------
        if role == "supervisor":
            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_locations
                WHERE location_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                lid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only update locations assigned to your team"
                ), 403

        # -------------------------------------------------
        # Update location
        # -------------------------------------------------
        conn.execute("""
            UPDATE locations
            SET name = COALESCE(?, name),
                address = COALESCE(?, address),
                city = COALESCE(?, city),
                country = COALESCE(?, country)
            WHERE id = ?
              AND organization_id = ?
        """, (
            d.get("name"),
            d.get("address"),
            d.get("city"),
            d.get("country"),
            lid,
            organization_id
        ))

        conn.commit()

        return jsonify(message="Updated")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
#delete-locations**************************************************************
@app.route("/api/locations/<lid>", methods=["DELETE"])
@jwt_required()
def delete_location(lid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if role not in ("admin", "supervisor"):
        return jsonify(
            error="Only admins and supervisors can delete locations"
        ), 403

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        # -------------------------------------------------
        # Find location inside user's organization
        # -------------------------------------------------
        location = conn.execute("""
            SELECT id
            FROM locations
            WHERE id = ?
              AND organization_id = ?
        """, (lid, organization_id)).fetchone()

        if not location:
            return jsonify(error="Location not found"), 404

        # -------------------------------------------------
        # Supervisor can only delete team locations
        # -------------------------------------------------
        if role == "supervisor":
            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_locations
                WHERE location_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                lid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only delete locations assigned to your team"
                ), 403

        # -------------------------------------------------
        # Delete location
        # team_locations will cascade-delete its
        # location associations because of the FK.
        # -------------------------------------------------
        conn.execute("""
            DELETE FROM locations
            WHERE id = ?
              AND organization_id = ?
        """, (lid, organization_id))

        conn.commit()

        return jsonify(message="Deleted")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
# ─── zones ────────────────────────────────────────────────────────────────────
# ─── zones ────────────────────────────────────────────────────────────────────

@app.route("/api/zones", methods=["GET"])
@jwt_required()
def get_zones():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    team_ids = get_current_team_ids()
    requested_loc = request.args.get("location_id")

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        base_select = """
            SELECT
                z.*,
                l.name AS location_name,

                (
                    SELECT COUNT(*)
                    FROM tasks t
                    WHERE t.zone_id = z.id
                      AND t.status = 'pending'
                ) AS pending_tasks,

                (
                    SELECT COUNT(*)
                    FROM tasks t
                    WHERE t.zone_id = z.id
                      AND t.is_overdue = 1
                ) AS overdue_tasks

            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
        """

        # ---------------------------------------------------------
        # ADMIN
        # All zones belonging to the admin's organization.
        # ---------------------------------------------------------
        if role == "admin":

            query = base_select + """
                WHERE l.organization_id = ?
            """

            params = [organization_id]

            if requested_loc:
                query += """
                    AND z.location_id = ?
                """
                params.append(requested_loc)

            query += " ORDER BY z.name"

        # ---------------------------------------------------------
        # SUPERVISOR
        # Only zones belonging to locations assigned to
        # the supervisor's team(s).
        # ---------------------------------------------------------
        elif role == "supervisor":

            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            query = base_select + f"""
                JOIN team_locations tl
                    ON tl.location_id = z.location_id

                WHERE l.organization_id = ?
                  AND tl.team_id IN ({placeholders})
            """

            params = [organization_id, *team_ids]

            if requested_loc:
                query += """
                    AND z.location_id = ?
                """
                params.append(requested_loc)

            query += """
                GROUP BY z.id
                ORDER BY z.name
            """

        # ---------------------------------------------------------
        # EMPLOYEE
        # Only zones explicitly assigned to the employee.
        # ---------------------------------------------------------
        elif role == "employee":

            query = base_select + """
                JOIN staff_zones sz
                    ON sz.zone_id = z.id
                   AND sz.user_id = ?

                WHERE l.organization_id = ?
                ORDER BY z.name
            """

            params = [
                user.get("id"),
                organization_id
            ]

        else:
            return jsonify(error="Invalid role"), 403

        rows = conn.execute(query, params).fetchall()

        return jsonify(rows_to_list(rows))

    finally:
        conn.close()


@app.route("/api/zones/<zid>", methods=["GET"])
@jwt_required()
def get_zone(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    user_id = user.get("id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        zone = conn.execute("""
            SELECT
                z.*,
                l.name AS location_name,
                l.organization_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (zid, organization_id)).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        # ---------------------------------------------------------
        # ADMIN
        # ---------------------------------------------------------
        if role == "admin":
            return jsonify(row_to_dict(zone))

        # ---------------------------------------------------------
        # SUPERVISOR
        # Zone must belong to one of supervisor's team locations.
        # ---------------------------------------------------------
        if role == "supervisor":

            if not team_ids:
                return jsonify(error="Access denied"), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_locations
                WHERE location_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zone["location_id"],
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(error="Access denied"), 403

            return jsonify(row_to_dict(zone))

        # ---------------------------------------------------------
        # EMPLOYEE
        # Employee must have explicit zone assignment.
        # ---------------------------------------------------------
        if role == "employee":

            assigned = conn.execute("""
                SELECT 1
                FROM staff_zones
                WHERE user_id = ?
                  AND zone_id = ?
            """, (user_id, zid)).fetchone()

            if not assigned:
                return jsonify(error="Access denied"), 403

            return jsonify(row_to_dict(zone))

        return jsonify(error="Invalid role"), 403

    finally:
        conn.close()


@app.route("/api/zones", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_zone():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    d = request.get_json() or {}

    if not d.get("location_id") or not d.get("name"):
        return jsonify(
            error="location_id and name required"
        ), 400

    conn = get_db()

    try:
        # ---------------------------------------------------------
        # Location must belong to current organization.
        # ---------------------------------------------------------
        location = conn.execute("""
            SELECT
                id,
                organization_id
            FROM locations
            WHERE id = ?
              AND organization_id = ?
        """, (
            d["location_id"],
            organization_id
        )).fetchone()

        if not location:
            return jsonify(error="Location not found"), 404

        # ---------------------------------------------------------
        # Supervisor can only create zones in locations
        # assigned to their own team(s).
        # ---------------------------------------------------------
        if role == "supervisor":

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_locations
                WHERE location_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                d["location_id"],
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only create zones in locations assigned to your team"
                ), 403

        zid = str(uuid.uuid4())

        qr = gen_qr_b64(
            f'{{"zoneId":"{zid}","name":"{d["name"]}"}}'
        )

        conn.execute(
            """
            INSERT INTO zones VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                CURRENT_TIMESTAMP
            )
            """,
            (
                zid,
                d["location_id"],
                d["name"],
                d.get("floor"),
                d.get("type", "bathroom"),
                qr,
                d.get("cleaning_interval_minutes", 60),
                "pending",
                None
            )
        )

        # ---------------------------------------------------------
        # Every zone created in a team location becomes visible
        # to that team through team_zones.
        #
        # For admin-created zones, associate the zone with every
        # team already assigned to that location.
        # ---------------------------------------------------------
        team_rows = conn.execute("""
            SELECT team_id
            FROM team_locations
            WHERE location_id = ?
        """, (d["location_id"],)).fetchall()

        for team_row in team_rows:
            conn.execute("""
                INSERT OR IGNORE INTO team_zones (
                    team_id,
                    zone_id
                )
                VALUES (?, ?)
            """, (
                team_row["team_id"],
                zid
            ))

        conn.commit()

        return jsonify(
            id=zid,
            qr_code=qr
        ), 201

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()


@app.route("/api/zones/<zid>", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def update_zone(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    d = request.get_json() or {}

    conn = get_db()

    try:
        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            zid,
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        # ---------------------------------------------------------
        # Supervisor authorization.
        # ---------------------------------------------------------
        if role == "supervisor":

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only update zones assigned to your team"
                ), 403

        # ---------------------------------------------------------
        # Prevent changing zone to a location outside the
        # permitted organization/team scope.
        # ---------------------------------------------------------
        new_location_id = d.get("location_id")

        if new_location_id:

            new_location = conn.execute("""
                SELECT id
                FROM locations
                WHERE id = ?
                  AND organization_id = ?
            """, (
                new_location_id,
                organization_id
            )).fetchone()

            if not new_location:
                return jsonify(
                    error="Invalid location for this organization"
                ), 400

            if role == "supervisor":

                allowed_location = conn.execute(f"""
                    SELECT 1
                    FROM team_locations
                    WHERE location_id = ?
                      AND team_id IN ({placeholders})
                    LIMIT 1
                """, (
                    new_location_id,
                    *team_ids
                )).fetchone()

                if not allowed_location:
                    return jsonify(
                        error="You can only move zones to locations assigned to your team"
                    ), 403

        conn.execute(
            """
            UPDATE zones
            SET
                location_id = COALESCE(?, location_id),
                name = COALESCE(?, name),
                floor = COALESCE(?, floor),
                type = COALESCE(?, type),
                cleaning_interval_minutes =
                    COALESCE(?, cleaning_interval_minutes),
                status = COALESCE(?, status)
            WHERE id = ?
            """,
            (
                d.get("location_id"),
                d.get("name"),
                d.get("floor"),
                d.get("type"),
                d.get("cleaning_interval_minutes"),
                d.get("status"),
                zid
            )
        )

        # ---------------------------------------------------------
        # Rebuild team-zone associations when the location changes.
        # ---------------------------------------------------------
        if new_location_id:

            conn.execute("""
                DELETE FROM team_zones
                WHERE zone_id = ?
            """, (zid,))

            team_rows = conn.execute("""
                SELECT team_id
                FROM team_locations
                WHERE location_id = ?
            """, (new_location_id,)).fetchall()

            for team_row in team_rows:
                conn.execute("""
                    INSERT OR IGNORE INTO team_zones (
                        team_id,
                        zone_id
                    )
                    VALUES (?, ?)
                """, (
                    team_row["team_id"],
                    zid
                ))

        conn.commit()

        return jsonify(message="Updated")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()


@app.route("/api/zones/<zid>", methods=["DELETE"])
@jwt_required()
@require_roles("admin", "supervisor")
def delete_zone(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            zid,
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        if role == "supervisor":

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only delete zones assigned to your team"
                ), 403

        conn.execute("""
            DELETE FROM team_zones
            WHERE zone_id = ?
        """, (zid,))

        conn.execute("""
            DELETE FROM staff_zones
            WHERE zone_id = ?
        """, (zid,))

        conn.execute("""
            DELETE FROM zones
            WHERE id = ?
        """, (zid,))

        conn.commit()

        return jsonify(message="Deleted")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()


@app.route("/api/zones/<zid>/qr")
@jwt_required()
def zone_qr(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    user_id = user.get("id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id,
                z.qr_code
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            zid,
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        if role == "admin":
            pass

        elif role == "supervisor":

            if not team_ids:
                return jsonify(error="Access denied"), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(error="Access denied"), 403

        elif role == "employee":

            assigned = conn.execute("""
                SELECT 1
                FROM staff_zones
                WHERE user_id = ?
                  AND zone_id = ?
            """, (
                user_id,
                zid
            )).fetchone()

            if not assigned:
                return jsonify(error="Access denied"), 403

        else:
            return jsonify(error="Invalid role"), 403

        return jsonify(
            qr_code=zone["qr_code"]
        )

    finally:
        conn.close()


@app.route("/api/zones/<zid>/staff")
@jwt_required()
def zone_staff(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    user_id = user.get("id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            zid,
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        # ---------------------------------------------------------
        # EMPLOYEE
        # Only their own assignment is returned.
        # ---------------------------------------------------------
        if role == "employee":

            assigned = conn.execute("""
                SELECT 1
                FROM staff_zones
                WHERE user_id = ?
                  AND zone_id = ?
            """, (
                user_id,
                zid
            )).fetchone()

            if not assigned:
                return jsonify(error="Access denied"), 403

            rows = conn.execute("""
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.phone,
                    sz.shift,
                    sz.assigned_at
                FROM staff_zones sz
                JOIN users u
                    ON u.id = sz.user_id
                WHERE sz.zone_id = ?
                  AND sz.user_id = ?
                  AND u.organization_id = ?
            """, (
                zid,
                user_id,
                organization_id
            )).fetchall()

            return jsonify(rows_to_list(rows))

        # ---------------------------------------------------------
        # ADMIN
        # ---------------------------------------------------------
        if role == "admin":
            pass

        # ---------------------------------------------------------
        # SUPERVISOR
        # Must control the zone through team_zones.
        # ---------------------------------------------------------
        elif role == "supervisor":

            if not team_ids:
                return jsonify(error="Access denied"), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zid,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(error="Access denied"), 403

        else:
            return jsonify(error="Invalid role"), 403

        rows = conn.execute("""
            SELECT
                u.id,
                u.name,
                u.email,
                u.phone,
                sz.shift,
                sz.assigned_at
            FROM staff_zones sz
            JOIN users u
                ON u.id = sz.user_id
            WHERE sz.zone_id = ?
              AND u.organization_id = ?
              AND u.role = 'employee'
            ORDER BY u.name
        """, (
            zid,
            organization_id
        )).fetchall()

        # ---------------------------------------------------------
        # Supervisor should only see employees belonging to one
        # of the supervisor's teams.
        # ---------------------------------------------------------
        if role == "supervisor":

            placeholders = ",".join("?" for _ in team_ids)

            rows = conn.execute(f"""
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.phone,
                    sz.shift,
                    sz.assigned_at
                FROM staff_zones sz
                JOIN users u
                    ON u.id = sz.user_id
                JOIN team_members tm
                    ON tm.user_id = u.id
                WHERE sz.zone_id = ?
                  AND u.organization_id = ?
                  AND u.role = 'employee'
                  AND tm.team_id IN ({placeholders})
                ORDER BY u.name
            """, (
                zid,
                organization_id,
                *team_ids
            )).fetchall()

        return jsonify(rows_to_list(rows))

    finally:
        conn.close()


@app.route("/api/zones/<zid>/assign", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def assign_zone(zid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    d = request.get_json() or {}

    if not d.get("user_id"):
        return jsonify(error="user_id required"), 400

    target_user_id = d["user_id"]

    conn = get_db()

    try:
        # ---------------------------------------------------------
        # Zone must belong to current organization.
        # ---------------------------------------------------------
        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            zid,
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        # ---------------------------------------------------------
        # Supervisor must control this zone through their team.
        # ---------------------------------------------------------
        if role == "supervisor":

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed_zone = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                zid,
                *team_ids
            )).fetchone()

            if not allowed_zone:
                return jsonify(
                    error="You can only manage zones assigned to your team"
                ), 403

        # ---------------------------------------------------------
        # Target must be an employee in the same organization.
        # ---------------------------------------------------------
        target = conn.execute("""
            SELECT
                id,
                role,
                organization_id
            FROM users
            WHERE id = ?
              AND organization_id = ?
              AND account_status = 'active'
              AND is_active = 1
        """, (
            target_user_id,
            organization_id
        )).fetchone()

        if not target:
            return jsonify(
                error="Employee not found in your organization"
            ), 404

        if target["role"] != "employee":
            return jsonify(
                error="Only employees can be assigned to zones"
            ), 400

        # ---------------------------------------------------------
        # Target employee must belong to a team that controls
        # this zone.
        # ---------------------------------------------------------
        zone_team_rows = conn.execute("""
            SELECT team_id
            FROM team_zones
            WHERE zone_id = ?
        """, (zid,)).fetchall()

        zone_team_ids = [
            row["team_id"]
            for row in zone_team_rows
        ]

        if not zone_team_ids:
            return jsonify(
                error="Zone is not assigned to any team"
            ), 400

        team_placeholders = ",".join(
            "?" for _ in zone_team_ids
        )

        employee_team = conn.execute(f"""
            SELECT 1
            FROM team_members
            WHERE user_id = ?
              AND team_id IN ({team_placeholders})
            LIMIT 1
        """, (
            target_user_id,
            *zone_team_ids
        )).fetchone()

        if not employee_team:
            return jsonify(
                error="Employee does not belong to a team assigned to this zone"
            ), 400

        # ---------------------------------------------------------
        # Supervisor cannot assign an employee outside their own
        # team scope.
        # ---------------------------------------------------------
        if role == "supervisor":

            supervisor_team_placeholders = ",".join(
                "?" for _ in team_ids
            )

            allowed_employee = conn.execute(f"""
                SELECT 1
                FROM team_members
                WHERE user_id = ?
                  AND team_id IN ({supervisor_team_placeholders})
                LIMIT 1
            """, (
                target_user_id,
                *team_ids
            )).fetchone()

            if not allowed_employee:
                return jsonify(
                    error="You can only assign employees from your team"
                ), 403

        # ---------------------------------------------------------
        # Replace existing assignment.
        # ---------------------------------------------------------
        conn.execute("""
            DELETE FROM staff_zones
            WHERE user_id = ?
              AND zone_id = ?
        """, (
            target_user_id,
            zid
        ))

        conn.execute(
            """
            INSERT INTO staff_zones (
                id,
                user_id,
                zone_id,
                shift,
                assigned_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid.uuid4()),
                target_user_id,
                zid,
                d.get("shift", "morning")
            )
        )

        conn.commit()

        return jsonify(message="Assigned"), 201

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
# ─── users ────────────────────────────────────────────────────────────────────

def user_in_supervisor_scope(conn, supervisor_id, target_user_id):
    """
    Return True when target_user_id belongs to a team supervised
    by supervisor_id.
    """

    row = conn.execute("""
        SELECT 1
        FROM team_supervisors ts
        JOIN team_members tm
            ON ts.team_id = tm.team_id
        WHERE ts.user_id = ?
          AND tm.user_id = ?

        UNION

        SELECT 1
        FROM team_supervisors ts
        JOIN team_supervisors target_ts
            ON ts.team_id = target_ts.team_id
        WHERE ts.user_id = ?
          AND target_ts.user_id = ?

        LIMIT 1
    """, (
        supervisor_id,
        target_user_id,
        supervisor_id,
        target_user_id
    )).fetchone()

    return bool(row)


@app.route("/api/users", methods=["GET"])
@jwt_required()
def get_users():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    organization_id = user.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    if role == "employee":
        return jsonify(error="Access denied"), 403

    conn = get_db()

    try:
        requested_role = (
            request.args.get("role") or ""
        ).strip().lower()

        if requested_role and requested_role not in (
            "admin",
            "supervisor",
            "employee"
        ):
            return jsonify(error="Invalid role"), 400

        if role == "admin":

            query = """
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.notification_email,
                    u.phone,
                    u.role,
                    u.location_id,
                    u.is_active,
                    u.account_status,
                    u.employee_id,
                    u.organization_id,
                    u.created_at
                FROM users u
                WHERE u.organization_id = ?
            """

            params = [organization_id]

            if requested_role:
                query += " AND u.role = ?"
                params.append(requested_role)

        elif role == "supervisor":

            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            query = f"""
                SELECT DISTINCT
                    u.id,
                    u.name,
                    u.email,
                    u.notification_email,
                    u.phone,
                    u.role,
                    u.location_id,
                    u.is_active,
                    u.account_status,
                    u.employee_id,
                    u.organization_id,
                    u.created_at
                FROM users u
                WHERE u.organization_id = ?
                  AND (
                      u.id IN (
                          SELECT tm.user_id
                          FROM team_members tm
                          WHERE tm.team_id IN ({placeholders})
                      )
                      OR
                      u.id IN (
                          SELECT ts.user_id
                          FROM team_supervisors ts
                          WHERE ts.team_id IN ({placeholders})
                      )
                  )
            """

            params = [
                organization_id,
                *team_ids,
                *team_ids
            ]

            if requested_role:
                query += " AND u.role = ?"
                params.append(requested_role)

        else:
            return jsonify(error="Invalid role"), 403

        query += " ORDER BY u.name"

        rows = rows_to_list(
            conn.execute(query, params).fetchall()
        )

        return jsonify(rows)

    finally:
        conn.close()


@app.route("/api/users", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_user():

    creator = get_current_user()

    if not creator:
        return jsonify(
            error="User not found"
        ), 404

    if not require_account_status(creator):
        return jsonify(
            error="Account is not active.",
            account_status=creator.get("account_status")
        ), 403

    creator_role = creator.get("role")
    organization_id = creator.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    data = request.get_json() or {}

    name = (
        data.get("name") or ""
    ).strip()

    email = (
        data.get("email") or ""
    ).strip().lower()

    password = (
        data.get("password") or ""
    )

    requested_role = (
        data.get("role") or "employee"
    ).strip().lower()

    team_id = data.get("team_id")

    employee_id = (
        data.get("employee_id") or ""
    ).strip() or None

    if not name or not email or not password:
        return jsonify(
            error="name, email, password required"
        ), 400

    if len(password) < 8:
        return jsonify(
            error="Password must be at least 8 characters"
        ), 400

    if requested_role not in (
        "supervisor",
        "employee"
    ):
        return jsonify(
            error="Only supervisor or employee accounts can be created here"
        ), 400

    # Supervisors can only create employees.
    if (
        creator_role == "supervisor"
        and requested_role != "employee"
    ):
        return jsonify(
            error="Supervisors can only create employee accounts"
        ), 403

    conn = get_db()

    try:

        # -----------------------------------------------------
        # Prevent duplicate email
        # -----------------------------------------------------

        existing = conn.execute(
            """
            SELECT id
            FROM users
            WHERE LOWER(email) = ?
            """,
            (
                email,
            )
        ).fetchone()

        if existing:
            return jsonify(
                error="Email already exists"
            ), 409

        # -----------------------------------------------------
        # Prevent duplicate employee ID
        # -----------------------------------------------------

        if employee_id:

            existing_employee = conn.execute(
                """
                SELECT id
                FROM users
                WHERE organization_id = ?
                  AND employee_id = ?
                """,
                (
                    organization_id,
                    employee_id
                )
            ).fetchone()

            if existing_employee:
                return jsonify(
                    error="Employee ID is already in use"
                ), 409

        # -----------------------------------------------------
        # Team is required
        # -----------------------------------------------------

        if not team_id:
            return jsonify(
                error="team_id is required"
            ), 400

        team = conn.execute(
            """
            SELECT id
            FROM teams
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                team_id,
                organization_id
            )
        ).fetchone()

        if not team:
            return jsonify(
                error="Team does not exist in your organization"
            ), 400

        # -----------------------------------------------------
        # Supervisor may only add employees
        # to teams they supervise.
        # -----------------------------------------------------

        if creator_role == "supervisor":

            supervised = conn.execute(
                """
                SELECT 1
                FROM team_supervisors
                WHERE team_id = ?
                  AND user_id = ?
                """,
                (
                    team_id,
                    creator.get("id")
                )
            ).fetchone()

            if not supervised:
                return jsonify(
                    error="You can only add employees to your own teams"
                ), 403

        # -----------------------------------------------------
        # Create user
        # -----------------------------------------------------

        user_id = str(
            uuid.uuid4()
        )

        password_hash = bcrypt.hashpw(
            password.encode(),
            bcrypt.gensalt()
        ).decode()

        conn.execute("BEGIN")

        conn.execute(
            """
            INSERT INTO users (
                id,
                name,
                email,
                notification_email,
                phone,
                password_hash,
                role,
                location_id,
                is_active,
                organization_id,
                account_status,
                employee_id
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                user_id,
                name,
                email,
                data.get("notification_email"),
                data.get("phone"),
                password_hash,
                requested_role,
                None,
                1,
                organization_id,
                "active",
                employee_id
            )
        )

        # -----------------------------------------------------
        # Assign team according to role
        # -----------------------------------------------------

        relationship_id = str(
            uuid.uuid4()
        )

        if requested_role == "employee":

            conn.execute(
                """
                INSERT INTO team_members (
                    id,
                    team_id,
                    user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    relationship_id,
                    team_id,
                    user_id
                )
            )

        elif requested_role == "supervisor":

            conn.execute(
                """
                INSERT INTO team_supervisors (
                    id,
                    team_id,
                    user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    relationship_id,
                    team_id,
                    user_id
                )
            )

        conn.commit()

        return jsonify(
            id=user_id,
            name=name,
            email=email,
            role=requested_role,
            organization_id=organization_id,
            team_id=team_id
        ), 201

    except Exception as e:

        conn.rollback()

        return jsonify(
            error=str(e)
        ), 500

    finally:

        conn.close()

@app.route("/api/users/<uid>", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def update_user(uid):
    editor = get_current_user()

    if not editor:
        return jsonify(error="User not found"), 404

    if not require_account_status(editor):
        return jsonify(
            error="Account is not active.",
            account_status=editor.get("account_status")
        ), 403

    editor_role = editor.get("role")
    organization_id = editor.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        target = conn.execute(
            """
            SELECT *
            FROM users
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                uid,
                organization_id
            )
        ).fetchone()

        if not target:
            return jsonify(
                error="User not found"
            ), 404

        target = row_to_dict(target)

        # Supervisors may only manage users in their own teams.
        if editor_role == "supervisor":

            if not user_in_supervisor_scope(
                conn,
                editor.get("id"),
                uid
            ):
                return jsonify(
                    error="You can only manage users in your own teams"
                ), 403

            # Supervisors cannot modify role.
            if "role" in request.get_json(silent=True) or {}:
                return jsonify(
                    error="Supervisors cannot change user roles"
                ), 403

            # Supervisors cannot modify account activation.
            if "is_active" in request.get_json(silent=True) or {}:
                return jsonify(
                    error="Supervisors cannot change account activation"
                ), 403

        d = request.get_json() or {}

        # Prevent duplicate email.
        if d.get("email"):

            email = d["email"].strip().lower()

            existing = conn.execute(
                """
                SELECT id
                FROM users
                WHERE LOWER(email) = ?
                  AND id != ?
                """,
                (
                    email,
                    uid
                )
            ).fetchone()

            if existing:
                return jsonify(
                    error="Email already in use"
                ), 409

        # Role changes are admin-only.
        new_role = d.get("role")

        if new_role is not None:

            new_role = str(new_role).strip().lower()

            if new_role not in (
                "admin",
                "supervisor",
                "employee"
            ):
                return jsonify(
                    error="Invalid role"
                ), 400

        # Employee ID uniqueness.
        new_employee_id = d.get("employee_id")

        if new_employee_id:

            existing_employee = conn.execute(
                """
                SELECT id
                FROM users
                WHERE organization_id = ?
                  AND employee_id = ?
                  AND id != ?
                """,
                (
                    organization_id,
                    new_employee_id,
                    uid
                )
            ).fetchone()

            if existing_employee:
                return jsonify(
                    error="Employee ID is already in use"
                ), 409
            
            # Location assignment — Admin only.
            new_location_id = d.get("location_id")

            if new_location_id:

             location = conn.execute(
        """
        SELECT id
        FROM locations
        WHERE id = ?
          AND organization_id = ?
        """,
        (
            new_location_id,
            organization_id
        )
    ).fetchone()

        if not location:
          return jsonify(
            error="Location does not exist in your organization"
        ), 400

        # Basic user fields.
        conn.execute(
            """
  UPDATE users
SET
    name = COALESCE(?, name),
    email = COALESCE(?, email),
    notification_email = COALESCE(?, notification_email),
    phone = COALESCE(?, phone),
    role = COALESCE(?, role),
    employee_id = COALESCE(?, employee_id),
    location_id = COALESCE(?, location_id),
    is_active = COALESCE(?, is_active)
            WHERE id = ?
              AND organization_id = ?
            """,
            (
                d.get("name"),
                d.get("email", "").strip().lower()
                if d.get("email")
                else None,
                d.get("notification_email"),
                d.get("phone"),
                new_role if editor_role == "admin" else None,
                new_employee_id,

new_location_id
if editor_role == "admin"
else None,

d.get("is_active")
if editor_role == "admin"
else None
                if editor_role == "admin"
                else None,
                uid,
                organization_id
            )
        )

        # Optional team reassignment.
        if "team_id" in d:

            new_team_id = d.get("team_id")

            if not new_team_id:
                return jsonify(
                    error="team_id cannot be empty"
                ), 400

            team = conn.execute(
                """
                SELECT id
                FROM teams
                WHERE id = ?
                  AND organization_id = ?
                """,
                (
                    new_team_id,
                    organization_id
                )
            ).fetchone()

            if not team:
                return jsonify(
                    error="Team does not exist in your organization"
                ), 400

            effective_role = (
                new_role
                if editor_role == "admin" and new_role
                else target["role"]
            )

            if editor_role == "supervisor":

                supervised = conn.execute(
                    """
                    SELECT 1
                    FROM team_supervisors
                    WHERE team_id = ?
                      AND user_id = ?
                    """,
                    (
                        new_team_id,
                        editor.get("id")
                    )
                ).fetchone()

                if not supervised:
                    return jsonify(
                        error="You can only assign users to your own teams"
                    ), 403

            # Remove existing team relationships.
            conn.execute(
                """
                DELETE FROM team_members
                WHERE user_id = ?
                """,
                (uid,)
            )

            conn.execute(
                """
                DELETE FROM team_supervisors
                WHERE user_id = ?
                """,
                (uid,)
            )

            relationship_id = str(uuid.uuid4())

            if effective_role == "employee":

                conn.execute(
                    """
                    INSERT INTO team_members (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        relationship_id,
                        new_team_id,
                        uid
                    )
                )

            elif effective_role == "supervisor":

                conn.execute(
                    """
                    INSERT INTO team_supervisors (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        relationship_id,
                        new_team_id,
                        uid
                    )
                )

        conn.commit()

        return jsonify(
            message="Updated"
        )

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
        
        # ============================================================
# ADMIN STAFF — ADMIN ONLY
# ============================================================

@app.route("/api/admin/staff", methods=["GET"])
@jwt_required()
def admin_staff():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    if user.get("role") != "admin":
        return jsonify(error="Admin access required"), 403

    organization_id = user.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        user_rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.notification_email,
                    u.phone,
                    u.role,
                    u.location_id,
                    u.is_active,
                    u.account_status,
                    u.employee_id,
                    u.created_at
                FROM users u
                WHERE u.organization_id = ?
                ORDER BY u.name
                """,
                (organization_id,)
            ).fetchall()
        )

        location_rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    id,
                    name
                FROM locations
                WHERE organization_id = ?
                ORDER BY name
                """,
                (organization_id,)
            ).fetchall()
        )

        team_rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    id,
                    name
                FROM teams
                WHERE organization_id = ?
                ORDER BY name
                """,
                (organization_id,)
            ).fetchall()
        )

        supervisor_team_rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    ts.user_id AS supervisor_id,
                    ts.team_id AS team_id,
                    t.name AS team_name
                FROM team_supervisors ts
                JOIN teams t
                    ON t.id = ts.team_id
                WHERE t.organization_id = ?
                ORDER BY t.name
                """,
                (organization_id,)
            ).fetchall()
        )

        employee_team_rows = rows_to_list(
            conn.execute(
                """
                SELECT
                    tm.user_id AS employee_id,
                    tm.team_id AS team_id
                FROM team_members tm
                JOIN teams t
                    ON t.id = tm.team_id
                WHERE t.organization_id = ?
                """
            ).fetchall()
        )

        locations = {
            str(row["id"]): row["name"]
            for row in location_rows
        }

        teams = {
            str(row["id"]): row["name"]
            for row in team_rows
        }

        users = {
            str(row["id"]): row
            for row in user_rows
        }

        supervisor_teams = {}

        for row in supervisor_team_rows:
            supervisor_id = str(
                row["supervisor_id"]
            )

            supervisor_teams.setdefault(
                supervisor_id,
                []
            ).append(
                {
                    "id": row["team_id"],
                    "name": row["team_name"]
                }
            )

        employee_teams = {}

        for row in employee_team_rows:
            employee_id = str(
                row["employee_id"]
            )

            employee_teams.setdefault(
                employee_id,
                []
            ).append(row["team_id"])

        def build_employee(user_row):
            user_id = str(user_row["id"])

            team_ids = employee_teams.get(
                user_id,
                []
            )

            return {
                "id": user_row["id"],
                "name": user_row["name"],
                "email": user_row["email"],
                "phone": user_row["phone"],
                "employee_id": user_row["employee_id"],
                "role": user_row["role"],
                "is_active": bool(
                    user_row["is_active"]
                ),
                "account_status": user_row[
                    "account_status"
                ],
                "location_id": user_row[
                    "location_id"
                ],
                "location_name": locations.get(
                    str(user_row["location_id"])
                ),
                "team_ids": team_ids,
                "team_names": [
                    teams.get(str(team_id))
                    for team_id in team_ids
                    if teams.get(str(team_id))
                ],
                "created_at": user_row[
                    "created_at"
                ],
                "last_activity": None
            }

        employees = {
            str(row["id"]): build_employee(row)
            for row in user_rows
            if row["role"] == "employee"
        }

        supervisors = []

        for row in user_rows:

            if row["role"] != "supervisor":
                continue

            supervisor_id = str(
                row["id"]
            )

            supervisor_teams_for_user = supervisor_teams.get(
    supervisor_id,
    []
)

            team_ids = {
                str(team["id"])
                for team in supervisor_teams_for_user
            }

            employee_list = []

            for employee in employees.values():

                employee_team_ids = {
                    str(team_id)
                    for team_id in employee[
                        "team_ids"
                    ]
                }

                if employee_team_ids.intersection(
                    team_ids
                ):
                    employee_list.append(
                        employee
                    )

            location_names = sorted(
                {
                    employee["location_name"]
                    for employee in employee_list
                    if employee["location_name"]
                }
            )

            supervisors.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "email": row["email"],
                    "phone": row["phone"],
                    "employee_id": row[
                        "employee_id"
                    ],
                    "role": row["role"],
                    "is_active": bool(
                        row["is_active"]
                    ),
                    "account_status": row[
                        "account_status"
                    ],
                    "created_at": row[
                        "created_at"
                    ],
                    "last_activity": None,
                    "teams": supervisor_teams_for_user,
                    "employees": employee_list,
                    "employee_count": len(
                        employee_list
                    ),
                    "location_names": location_names
                }
            )

        assigned_employee_ids = set(
            employee_teams.keys()
        )

        unassigned_employees = [
            employee
            for employee_id, employee
            in employees.items()
            if employee_id not in assigned_employee_ids
        ]

        active_count = 0
        inactive_count = 0
        pending_count = 0

        for employee in employees.values():

            if employee["account_status"] == "pending":
                pending_count += 1

            elif employee["is_active"]:
                active_count += 1

            else:
                inactive_count += 1

        for supervisor in supervisors:

            if supervisor["account_status"] == "pending":
                pending_count += 1

            elif supervisor["is_active"]:
                active_count += 1

            else:
                inactive_count += 1

        return jsonify(
            summary={
                "total_staff":
                    len(supervisors) +
                    len(employees),

                "supervisors":
                    len(supervisors),

                "employees":
                    len(employees),

                "active":
                    active_count,

                "pending":
                    pending_count,

                "inactive":
                    inactive_count
            },
            supervisors=supervisors,
            unassigned_employees=
                unassigned_employees,
            teams=team_rows,
            locations=location_rows
        )

    finally:
        conn.close()
# ─── tasks ────────────────────────────────────────────────────────────────────

def task_scope_allowed(conn, task_id, user):
    """
    Returns the task row when the current user is allowed to access it.
    Returns None when the task does not exist or is outside the user's scope.
    """

    if not user:
        return None

    organization_id = user.get("organization_id")
    role = user.get("role")
    user_id = user.get("id")

    if not organization_id:
        return None

    task = conn.execute("""
        SELECT
            t.*,
            z.name AS zone_name,
            z.floor,
            z.location_id,
            l.name AS location_name,
            l.organization_id AS location_organization_id,
            u.name AS staff_name,
            u.email AS staff_email
        FROM tasks t
        JOIN zones z
            ON z.id = t.zone_id
        JOIN locations l
            ON l.id = z.location_id
        LEFT JOIN users u
            ON u.id = t.assigned_to
        WHERE t.id = ?
          AND l.organization_id = ?
    """, (task_id, organization_id)).fetchone()

    if not task:
        return None

    if role == "admin":
        return task

    if role == "supervisor":
        team_ids = get_current_team_ids()

        if not team_ids:
            return None

        placeholders = ",".join("?" for _ in team_ids)

        allowed = conn.execute(f"""
            SELECT 1
            FROM team_zones tz
            WHERE tz.zone_id = ?
              AND tz.team_id IN ({placeholders})
            LIMIT 1
        """, (
            task["zone_id"],
            *team_ids
        )).fetchone()

        return task if allowed else None

    if role == "employee":
        if task["assigned_to"] != user_id:
            return None

        return task

    return None


@app.route("/api/tasks", methods=["GET"])
@jwt_required()
def get_tasks():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    args = request.args

    if not organization_id:
        return jsonify(error="User has no organization assigned"), 403

    conn = get_db()

    try:
        q = """
            SELECT
                t.*,
                z.name AS zone_name,
                z.floor,
                l.name AS location_name,
                u.name AS staff_name,
                u.email AS staff_email
            FROM tasks t
            JOIN zones z
                ON t.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            LEFT JOIN users u
                ON t.assigned_to = u.id
            WHERE l.organization_id = ?
        """

        params = [organization_id]

        # ---------------------------------------------------------
        # ADMIN — every task in their organization
        # ---------------------------------------------------------

        if role == "admin":
            pass

        # ---------------------------------------------------------
        # SUPERVISOR — only tasks belonging to their team zones
        # ---------------------------------------------------------

        elif role == "supervisor":

            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            q += f"""
                AND EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    WHERE tz.zone_id = t.zone_id
                      AND tz.team_id IN ({placeholders})
                )
            """

            params.extend(team_ids)

        # ---------------------------------------------------------
        # EMPLOYEE — only their assigned tasks
        # ---------------------------------------------------------

        elif role == "employee":

            q += " AND t.assigned_to = ?"
            params.append(user["id"])

        else:
            return jsonify(error="Invalid role"), 403

        # ---------------------------------------------------------
        # OPTIONAL ZONE FILTER
        # ---------------------------------------------------------

        if args.get("zone_id"):

            zone_id = args["zone_id"]

            if role == "employee":

                # Employee may only request one of their own zones.
                allowed = conn.execute("""
                    SELECT 1
                    FROM tasks t
                    JOIN zones z
                        ON z.id = t.zone_id
                    JOIN locations l
                        ON l.id = z.location_id
                    WHERE t.zone_id = ?
                      AND t.assigned_to = ?
                      AND l.organization_id = ?
                    LIMIT 1
                """, (
                    zone_id,
                    user["id"],
                    organization_id
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            elif role == "supervisor":

                team_ids = get_current_team_ids()

                if not team_ids:
                    return jsonify(error="Access denied"), 403

                placeholders = ",".join("?" for _ in team_ids)

                allowed = conn.execute(f"""
                    SELECT 1
                    FROM team_zones tz
                    JOIN zones z
                        ON z.id = tz.zone_id
                    JOIN locations l
                        ON l.id = z.location_id
                    WHERE tz.zone_id = ?
                      AND tz.team_id IN ({placeholders})
                      AND l.organization_id = ?
                    LIMIT 1
                """, (
                    zone_id,
                    *team_ids,
                    organization_id
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            else:
                allowed = conn.execute("""
                    SELECT 1
                    FROM zones z
                    JOIN locations l
                        ON l.id = z.location_id
                    WHERE z.id = ?
                      AND l.organization_id = ?
                """, (
                    zone_id,
                    organization_id
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            q += " AND t.zone_id = ?"
            params.append(zone_id)

        # ---------------------------------------------------------
        # ASSIGNED USER FILTER
        # ---------------------------------------------------------

        if args.get("assigned_to"):

            requested_user = args["assigned_to"]

            if role == "employee" and requested_user != user["id"]:
                return jsonify(error="Access denied"), 403

            if role == "supervisor":

                team_ids = get_current_team_ids()

                if not team_ids:
                    return jsonify(error="Access denied"), 403

                placeholders = ",".join("?" for _ in team_ids)

                allowed = conn.execute(f"""
                    SELECT 1
                    FROM team_members tm
                    WHERE tm.user_id = ?
                      AND tm.team_id IN ({placeholders})
                    LIMIT 1
                """, (
                    requested_user,
                    *team_ids
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            elif role == "admin":

                allowed = conn.execute("""
                    SELECT id
                    FROM users
                    WHERE id = ?
                      AND organization_id = ?
                      AND role = 'employee'
                """, (
                    requested_user,
                    organization_id
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            q += " AND t.assigned_to = ?"
            params.append(requested_user)

        # ---------------------------------------------------------
        # STATUS FILTER
        # ---------------------------------------------------------

        if args.get("status"):
            q += " AND t.status = ?"
            params.append(args["status"])

        # ---------------------------------------------------------
        # LOCATION FILTER
        # ---------------------------------------------------------

        if args.get("location_id"):

            location_id = args["location_id"]

            location = conn.execute("""
                SELECT id
                FROM locations
                WHERE id = ?
                  AND organization_id = ?
            """, (
                location_id,
                organization_id
            )).fetchone()

            if not location:
                return jsonify(error="Access denied"), 403

            if role == "supervisor":

                team_ids = get_current_team_ids()

                if not team_ids:
                    return jsonify(error="Access denied"), 403

                placeholders = ",".join("?" for _ in team_ids)

                allowed = conn.execute(f"""
                    SELECT 1
                    FROM team_locations tl
                    WHERE tl.location_id = ?
                      AND tl.team_id IN ({placeholders})
                    LIMIT 1
                """, (
                    location_id,
                    *team_ids
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            elif role == "employee":

                allowed = conn.execute("""
                    SELECT 1
                    FROM team_locations tl
                    JOIN team_members tm
                        ON tm.team_id = tl.team_id
                    WHERE tl.location_id = ?
                      AND tm.user_id = ?
                    LIMIT 1
                """, (
                    location_id,
                    user["id"]
                )).fetchone()

                if not allowed:
                    return jsonify(error="Access denied"), 403

            q += " AND z.location_id = ?"
            params.append(location_id)

        # ---------------------------------------------------------
        # DATE FILTER
        # ---------------------------------------------------------

        if args.get("date"):
            q += " AND DATE(t.scheduled_at) = DATE(?)"
            params.append(args["date"])

        q += """
            ORDER BY t.scheduled_at DESC
            LIMIT 200
        """

        rows = conn.execute(q, params).fetchall()

        return jsonify(rows_to_list(rows))

    finally:
        conn.close()


# ─── create task ──────────────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_task():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")

    d = request.get_json() or {}

    if not d.get("zone_id") or not d.get("scheduled_at"):
        return jsonify(
            error="zone_id and scheduled_at required"
        ), 400

    conn = get_db()

    try:

        # ---------------------------------------------------------
        # VERIFY ZONE + ORGANIZATION
        # ---------------------------------------------------------

        zone = conn.execute("""
            SELECT
                z.id,
                z.location_id,
                l.organization_id
            FROM zones z
            JOIN locations l
                ON l.id = z.location_id
            WHERE z.id = ?
              AND l.organization_id = ?
        """, (
            d["zone_id"],
            organization_id
        )).fetchone()

        if not zone:
            return jsonify(error="Zone not found"), 404

        # ---------------------------------------------------------
        # SUPERVISOR — zone must belong to supervisor's team
        # ---------------------------------------------------------

        if role == "supervisor":

            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones
                WHERE zone_id = ?
                  AND team_id IN ({placeholders})
                LIMIT 1
            """, (
                d["zone_id"],
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only create tasks in your team's zones"
                ), 403

        # ---------------------------------------------------------
        # ASSIGNMENT
        # ---------------------------------------------------------

        assigned_to = d.get("assigned_to")

        if assigned_to:

            assigned_user = conn.execute("""
                SELECT
                    id,
                    role,
                    organization_id,
                    account_status
                FROM users
                WHERE id = ?
            """, (assigned_to,)).fetchone()

            if not assigned_user:
                return jsonify(
                    error="Assigned employee not found"
                ), 404

            if assigned_user["role"] != "employee":
                return jsonify(
                    error="Tasks can only be assigned to employees"
                ), 400

            if assigned_user["organization_id"] != organization_id:
                return jsonify(
                    error="Employee belongs to another organization"
                ), 403

            if assigned_user["account_status"] != "active":
                return jsonify(
                    error="Employee account is not active"
                ), 400

            # Employee must belong to a team that controls this zone.
            allowed_team = conn.execute("""
                SELECT 1
                FROM team_members tm
                JOIN team_zones tz
                    ON tz.team_id = tm.team_id
                WHERE tm.user_id = ?
                  AND tz.zone_id = ?
                LIMIT 1
            """, (
                assigned_to,
                d["zone_id"]
            )).fetchone()

            if not allowed_team:
                return jsonify(
                    error="Employee is not assigned to this zone's team"
                ), 400

            # Supervisor can only assign their own team members.
            if role == "supervisor":

                supervisor_team_ids = get_current_team_ids()

                if not any(
                    conn.execute("""
                        SELECT 1
                        FROM team_members
                        WHERE team_id = ?
                          AND user_id = ?
                    """, (
                        team_id,
                        assigned_to
                    )).fetchone()
                    for team_id in supervisor_team_ids
                ):
                    return jsonify(
                        error="You can only assign employees from your team"
                    ), 403

        tid = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO tasks (
                id,
                zone_id,
                assigned_to,
                status,
                scheduled_at,
                started_at,
                completed_at,
                duration_minutes,
                notes,
                is_overdue,
                overdue_count,
                created_at
            )
            VALUES (
                ?, ?, ?, 'pending', ?, NULL, NULL,
                NULL, NULL, 0, 0, CURRENT_TIMESTAMP
            )
        """, (
            tid,
            d["zone_id"],
            assigned_to,
            d["scheduled_at"]
        ))

        conn.commit()

        return jsonify(
            id=tid,
            message="Task created"
        ), 201

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()


# ─── start task ───────────────────────────────────────────────────────────────

@app.route("/api/tasks/<tid>/start", methods=["PUT"])
@jwt_required()
def start_task(tid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")

    conn = get_db()

    try:

        task = task_scope_allowed(conn, tid, user)

        if not task:
            return jsonify(error="Access denied"), 403

        if task["status"] != "pending":
            return jsonify(
                error="Only pending tasks can be started"
            ), 400

        # Admin can manage any task in own organization.
        # Supervisor can manage only their team tasks.
        # Employee can only start their own task.
        if role == "employee" and task["assigned_to"] != user["id"]:
            return jsonify(
                error="You can only start tasks assigned to you"
            ), 403

        conn.execute("""
            UPDATE tasks
            SET status = 'in-progress',
                started_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tid,))

        conn.execute("""
            UPDATE zones
            SET status = 'in-progress'
            WHERE id = ?
        """, (task["zone_id"],))

        conn.commit()

        return jsonify(message="Started")

    finally:
        conn.close()


# ─── complete task ────────────────────────────────────────────────────────────

@app.route("/api/tasks/<tid>/complete", methods=["PUT"])
@jwt_required()
def complete_task(tid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")
    d = request.get_json() or {}

    conn = get_db()

    try:

        task = task_scope_allowed(conn, tid, user)

        if not task:
            return jsonify(error="Access denied"), 403

        if task["status"] != "in-progress":
            return jsonify(
                error="Only in-progress tasks can be completed"
            ), 400

        if role == "employee" and task["assigned_to"] != user["id"]:
            return jsonify(
                error="You can only complete tasks assigned to you"
            ), 403

        duration = None

        if task["started_at"]:
            try:
                start = datetime.fromisoformat(task["started_at"])
                duration = round(
                    (datetime.now() - start).total_seconds() / 60,
                    1
                )
            except Exception:
                duration = None

        # Employee completion remains assigned to the employee.
        assigned_to = task["assigned_to"]

        if not assigned_to and role == "employee":
            assigned_to = user["id"]

        conn.execute("""
            UPDATE tasks
            SET status = 'pending-approval',
                completed_at = CURRENT_TIMESTAMP,
                duration_minutes = ?,
                notes = ?,
                assigned_to = ?
            WHERE id = ?
        """, (
            duration,
            d.get("notes"),
            assigned_to,
            tid
        ))

        conn.execute("""
            UPDATE zones
            SET status = 'pending'
            WHERE id = ?
        """, (task["zone_id"],))

        conn.commit()

        return jsonify(
            message="Completed and sent for approval",
            duration_minutes=duration
        )

    finally:
        conn.close()


# ─── miss task ────────────────────────────────────────────────────────────────

@app.route("/api/tasks/<tid>/miss", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def miss_task(tid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    role = user.get("role")

    conn = get_db()

    try:

        task = task_scope_allowed(conn, tid, user)

        if not task:
            return jsonify(error="Access denied"), 403

        if task["status"] != "pending":
            return jsonify(
                error="Only pending tasks can be marked as missed"
            ), 400

        new_count = (task["overdue_count"] or 0) + 1

        conn.execute("""
            UPDATE tasks
            SET status = 'missed',
                is_overdue = 1,
                overdue_count = ?
            WHERE id = ?
        """, (
            new_count,
            tid
        ))

        conn.execute("""
            UPDATE zones
            SET status = 'overdue'
            WHERE id = ?
        """, (task["zone_id"],))

        severity = (
            "critical"
            if new_count >= 3
            else "high"
            if new_count >= 2
            else "warning"
        )

        conn.execute("""
            INSERT INTO alerts (
                id,
                type,
                severity,
                zone_id,
                user_id,
                task_id,
                message,
                is_read,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP
            )
        """, (
            str(uuid.uuid4()),
            "missed_cleaning",
            severity,
            task["zone_id"],
            task["assigned_to"],
            tid,
            f"Cleaning missed {new_count}x for zone. Immediate attention required."
        ))

        conn.commit()

        return jsonify(
            message="Missed",
            overdue_count=new_count
        )

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()

#overdue_tasks-----------------------------------------------------------------
@app.route("/api/tasks/overdue")
@jwt_required()
def overdue_tasks():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    organization_id = user.get("organization_id")
    role = user.get("role")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        q = """
            SELECT
                t.*,
                z.name AS zone_name,
                z.floor,
                l.name AS location_name,
                u.name AS staff_name
            FROM tasks t
            JOIN zones z
                ON t.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            LEFT JOIN users u
                ON t.assigned_to = u.id
            WHERE t.is_overdue = 1
              AND l.organization_id = ?
        """

        params = [organization_id]

        # ---------------------------------------------------------
        # ADMIN
        # ---------------------------------------------------------
        # Admin sees every overdue task inside their organization.
        if role == "admin":
            pass

        # ---------------------------------------------------------
        # SUPERVISOR
        # ---------------------------------------------------------
        # Supervisor sees overdue tasks only for zones controlled
        # by one of their teams.
        elif role == "supervisor":

            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            q += f"""
                AND EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    WHERE tz.zone_id = t.zone_id
                      AND tz.team_id IN ({placeholders})
                )
            """

            params.extend(team_ids)

        # ---------------------------------------------------------
        # EMPLOYEE
        # ---------------------------------------------------------
        # Employee sees only their own overdue tasks.
        elif role == "employee":

            q += """
                AND t.assigned_to = ?
            """

            params.append(user.get("id"))

        else:
            return jsonify(error="Invalid role"), 403

        q += """
            ORDER BY
                t.overdue_count DESC,
                t.scheduled_at ASC
        """

        rows = rows_to_list(
            conn.execute(q, params).fetchall()
        )

        return jsonify(rows)

    finally:
        conn.close()
# ─── cleaning logs ───────────────────────────────────────────────────────────

#create_logs^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
@app.route("/api/logs", methods=["POST"])
@jwt_required()
def create_log():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    uid = user.get("id")
    role = user.get("role")
    organization_id = user.get("organization_id")

    zone_id = request.form.get("zone_id")
    task_id = request.form.get("task_id")
    notes = request.form.get("notes")

    if not task_id or not zone_id:
        return jsonify(
            error="task_id and zone_id required"
        ), 400

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        task = conn.execute("""
            SELECT
                t.*,
                z.location_id,
                l.organization_id
            FROM tasks t
            JOIN zones z
                ON t.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            WHERE t.id = ?
              AND l.organization_id = ?
        """, (
            task_id,
            organization_id
        )).fetchone()

        if not task:
            return jsonify(
                error="Task not found"
            ), 404

        # Submitted zone must match the task's zone.
        if task["zone_id"] != zone_id:
            return jsonify(
                error="Task and zone do not match"
            ), 400

        # Only in-progress tasks can receive a cleaning log.
        if task["status"] != "in-progress":
            return jsonify(
                error="Only in-progress tasks can receive cleaning logs"
            ), 400

        # ---------------------------------------------------------
        # ROLE / SCOPE CHECK
        # ---------------------------------------------------------

        if role == "admin":
            # Admin can submit logs for any task in their organization.
            pass

        elif role == "supervisor":
            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones tz
                WHERE tz.zone_id = ?
                  AND tz.team_id IN ({placeholders})
                LIMIT 1
            """, (
                zone_id,
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only submit logs for your team's zones"
                ), 403

        elif role == "employee":
            # Employee can only submit a log for their own assigned task.
            if task["assigned_to"] != uid:
                return jsonify(
                    error="You can only submit logs for your assigned tasks"
                ), 403

        else:
            return jsonify(
                error="Invalid role"
            ), 403

        before_photo = None
        after_photo = None

        for field in ("before_photo", "after_photo"):
            f = request.files.get(field)

            if f:
                fname = str(uuid.uuid4()) + os.path.splitext(
                    f.filename
                )[1]

                f.save(
                    os.path.join(
                        UPLOAD_FOLDER,
                        fname
                    )
                )

                if field == "before_photo":
                    before_photo = fname
                else:
                    after_photo = fname

        ai_score = None
        ai_feedback = None

        if after_photo:
            ai_score, ai_feedback = simulate_ai_score()

        lid = str(uuid.uuid4())

        conn.execute(
            """
            INSERT INTO cleaning_logs (
                id,
                task_id,
                user_id,
                zone_id,
                before_photo,
                after_photo,
                ai_cleanliness_score,
                ai_feedback,
                notes,
                logged_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                lid,
                task_id,
                uid,
                zone_id,
                before_photo,
                after_photo,
                ai_score,
                ai_feedback,
                notes
            )
        )

        # Task remains associated with the original assigned employee.
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending-approval'
            WHERE id = ?
            """,
            (task_id,)
        )

        conn.execute(
            """
            UPDATE zones
            SET status = 'pending'
            WHERE id = ?
            """,
            (zone_id,)
        )

        conn.commit()

        return jsonify(
            id=lid,
            ai_cleanliness_score=ai_score,
            ai_feedback=ai_feedback
        ), 201

    except Exception as e:
        conn.rollback()
        return jsonify(
            error=str(e)
        ), 500

    finally:
        conn.close()


#get_logs^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
@app.route("/api/logs", methods=["GET"])
@jwt_required()
def get_logs():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    uid = user.get("id")
    role = user.get("role")
    organization_id = user.get("organization_id")
    team_ids = get_current_team_ids()

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    args = request.args

    conn = get_db()

    try:
        q = """
            SELECT
                cl.*,
                u.name AS staff_name,
                u.notification_email AS staff_email,
                z.name AS zone_name,
                l.name AS location_name
            FROM cleaning_logs cl
            JOIN zones z
                ON cl.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            LEFT JOIN users u
                ON cl.user_id = u.id
            WHERE l.organization_id = ?
        """

        params = [organization_id]

        # ---------------------------------------------------------
        # ADMIN
        # ---------------------------------------------------------
        if role == "admin":
            pass

        # ---------------------------------------------------------
        # SUPERVISOR
        # ---------------------------------------------------------
        elif role == "supervisor":

            if not team_ids:
                return jsonify([])

            placeholders = ",".join("?" for _ in team_ids)

            q += f"""
                AND EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    WHERE tz.zone_id = cl.zone_id
                      AND tz.team_id IN ({placeholders})
                )
            """

            params.extend(team_ids)

        # ---------------------------------------------------------
        # EMPLOYEE
        # ---------------------------------------------------------
        elif role == "employee":

            q += """
                AND cl.user_id = ?
            """

            params.append(uid)

        else:
            return jsonify(
                error="Invalid role"
            ), 403

        # ---------------------------------------------------------
        # OPTIONAL FILTERS
        # ---------------------------------------------------------

        if args.get("zone_id"):
            requested_zone = args["zone_id"]

            # Employee/supervisor scope remains enforced by the
            # base query above.
            q += """
                AND cl.zone_id = ?
            """

            params.append(requested_zone)

        if args.get("user_id"):
            requested_user = args["user_id"]

            # Base role scope still applies.
            q += """
                AND cl.user_id = ?
            """

            params.append(requested_user)

        if args.get("task_id"):
            requested_task = args["task_id"]

            q += """
                AND cl.task_id = ?
            """

            params.append(requested_task)

        q += """
            ORDER BY cl.logged_at DESC
            LIMIT 100
        """

        rows = rows_to_list(
            conn.execute(q, params).fetchall()
        )

        return jsonify(rows)

    finally:
        conn.close()


# approve_tasks -----------------------------------------------------------------
@app.route("/api/tasks/<tid>/approve", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def approve_task(tid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    uid = user.get("id")
    role = user.get("role")
    organization_id = user.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        task = conn.execute("""
            SELECT
                t.id,
                t.status,
                t.zone_id,
                l.organization_id
            FROM tasks t
            JOIN zones z
                ON t.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            WHERE t.id = ?
              AND l.organization_id = ?
        """, (
            tid,
            organization_id
        )).fetchone()

        if not task:
            return jsonify(error="Task not found"), 404

        if task["status"] != "pending-approval":
            return jsonify(
                error="Only tasks pending approval can be approved"
            ), 400

        # Supervisor can only approve tasks belonging to their teams.
        if role == "supervisor":
            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones tz
                WHERE tz.zone_id = ?
                  AND tz.team_id IN ({placeholders})
                LIMIT 1
            """, (
                task["zone_id"],
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only approve tasks in your team's zones"
                ), 403

        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed'
            WHERE id = ?
            """,
            (tid,)
        )

        conn.commit()

        return jsonify(message="Task approved")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()


# reject_tasks ------------------------------------------------------------------
@app.route("/api/tasks/<tid>/reject", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def reject_task(tid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active.",
            account_status=user.get("account_status")
        ), 403

    uid = user.get("id")
    role = user.get("role")
    organization_id = user.get("organization_id")

    if not organization_id:
        return jsonify(
            error="User has no organization assigned"
        ), 403

    conn = get_db()

    try:
        task = conn.execute("""
            SELECT
                t.id,
                t.status,
                t.zone_id,
                l.organization_id
            FROM tasks t
            JOIN zones z
                ON t.zone_id = z.id
            JOIN locations l
                ON z.location_id = l.id
            WHERE t.id = ?
              AND l.organization_id = ?
        """, (
            tid,
            organization_id
        )).fetchone()

        if not task:
            return jsonify(error="Task not found"), 404

        if task["status"] != "pending-approval":
            return jsonify(
                error="Only tasks pending approval can be rejected"
            ), 400

        # Supervisor can only reject tasks belonging to their teams.
        if role == "supervisor":
            team_ids = get_current_team_ids()

            if not team_ids:
                return jsonify(
                    error="You are not assigned to a team"
                ), 403

            placeholders = ",".join("?" for _ in team_ids)

            allowed = conn.execute(f"""
                SELECT 1
                FROM team_zones tz
                WHERE tz.zone_id = ?
                  AND tz.team_id IN ({placeholders})
                LIMIT 1
            """, (
                task["zone_id"],
                *team_ids
            )).fetchone()

            if not allowed:
                return jsonify(
                    error="You can only reject tasks in your team's zones"
                ), 403

        conn.execute(
            """
            UPDATE tasks
            SET status = 'rejected'
            WHERE id = ?
            """,
            (tid,)
        )

        conn.commit()

        return jsonify(message="Task rejected")

    except Exception as e:
        conn.rollback()
        return jsonify(error=str(e)), 500

    finally:
        conn.close()
# ─── alerts ────────────────────────────────────────────────────────────────────
@app.route("/api/alerts", methods=["GET"])
@jwt_required()
def get_alerts():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    role = user.get("role")
    uid = user.get("id")
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    args = request.args
    conn = get_db()

    q = """
        SELECT a.*,
               z.name AS zone_name,
               l.name AS location_name
        FROM alerts a
        LEFT JOIN zones z
            ON a.zone_id = z.id
        LEFT JOIN locations l
            ON z.location_id = l.id
        LEFT JOIN users alert_user
            ON a.user_id = alert_user.id
        WHERE (
            l.organization_id = ?
            OR alert_user.organization_id = ?
        )
    """

    params = [org_id, org_id]

    # Admin: all alerts belonging to their organization.
    if role == "admin":
        pass

    # Supervisor: only alerts for zones belonging to teams they supervise.
    elif role == "supervisor":
        q += """
            AND (
                a.user_id = ?
                OR EXISTS (
                    SELECT 1
                    FROM team_supervisors ts
                    JOIN team_zones tz
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = a.zone_id
                )
            )
        """
        params.extend([uid, uid])

    # Employee: own alerts or alerts for zones explicitly assigned to them.
    elif role == "employee":
        q += """
            AND (
                a.user_id = ?
                OR EXISTS (
                    SELECT 1
                    FROM staff_zones sz
                    WHERE sz.user_id = ?
                      AND sz.zone_id = a.zone_id
                )
            )
        """
        params.extend([uid, uid])

    else:
        conn.close()
        return jsonify(error="Access denied"), 403

    if args.get("is_read") is not None:
        q += " AND a.is_read=?"
        params.append(1 if args["is_read"] == "true" else 0)

    if args.get("severity"):
        q += " AND a.severity=?"
        params.append(args["severity"])

    q += " ORDER BY a.created_at DESC LIMIT 100"

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)


@app.route("/api/alerts/<aid>/read", methods=["PUT"])
@jwt_required()
def mark_alert_read(aid):
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    role = user.get("role")
    uid = user.get("id")
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    conn = get_db()

    alert = conn.execute(
        """
        SELECT a.*
        FROM alerts a
        LEFT JOIN zones z
            ON a.zone_id = z.id
        LEFT JOIN locations l
            ON z.location_id = l.id
        LEFT JOIN users alert_user
            ON a.user_id = alert_user.id
        WHERE a.id=?
          AND (
              l.organization_id=?
              OR alert_user.organization_id=?
          )
        """,
        (aid, org_id, org_id)
    ).fetchone()

    if not alert:
        conn.close()
        return jsonify(error="Alert not found"), 404

    allowed = False

    # Admin can mark any alert in their organization.
    if role == "admin":
        allowed = True

    # Supervisor can mark their own alerts or alerts in their team's zones.
    elif role == "supervisor":
        scope = conn.execute(
            """
            SELECT 1
            WHERE ? = ?
               OR EXISTS (
                    SELECT 1
                    FROM team_supervisors ts
                    JOIN team_zones tz
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id=?
                      AND tz.zone_id=?
               )
            LIMIT 1
            """,
            (alert["user_id"], uid, uid, alert["zone_id"])
        ).fetchone()

        allowed = bool(scope)

    # Employee can mark their own alerts or alerts for assigned zones.
    elif role == "employee":
        scope = conn.execute(
            """
            SELECT 1
            WHERE ? = ?
               OR EXISTS (
                    SELECT 1
                    FROM staff_zones sz
                    WHERE sz.user_id=?
                      AND sz.zone_id=?
               )
            LIMIT 1
            """,
            (alert["user_id"], uid, uid, alert["zone_id"])
        ).fetchone()

        allowed = bool(scope)

    if not allowed:
        conn.close()
        return jsonify(error="Access denied"), 403

    conn.execute(
        "UPDATE alerts SET is_read=1 WHERE id=?",
        (aid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Marked read")


@app.route("/api/alerts/read-all", methods=["PUT"])
@jwt_required()
def mark_all_read():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    role = user.get("role")
    uid = user.get("id")
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    conn = get_db()

    # Admin: mark all alerts belonging to their organization only.
    if role == "admin":
        conn.execute(
            """
            UPDATE alerts
            SET is_read=1
            WHERE id IN (
                SELECT a.id
                FROM alerts a
                LEFT JOIN zones z
                    ON a.zone_id=z.id
                LEFT JOIN locations l
                    ON z.location_id=l.id
                LEFT JOIN users alert_user
                    ON a.user_id=alert_user.id
                WHERE l.organization_id=?
                   OR alert_user.organization_id=?
            )
            """,
            (org_id, org_id)
        )

    # Supervisor: only alerts from their supervised teams.
    elif role == "supervisor":
        conn.execute(
            """
            UPDATE alerts
            SET is_read=1
            WHERE user_id=?
               OR zone_id IN (
                    SELECT tz.zone_id
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id=tz.team_id
                    WHERE ts.user_id=?
               )
            """,
            (uid, uid)
        )

    # Employee: own alerts or alerts for their assigned zones.
    elif role == "employee":
        conn.execute(
            """
            UPDATE alerts
            SET is_read=1
            WHERE user_id=?
               OR zone_id IN (
                    SELECT zone_id
                    FROM staff_zones
                    WHERE user_id=?
               )
            """,
            (uid, uid)
        )

    else:
        conn.close()
        return jsonify(error="Access denied"), 403

    conn.commit()
    conn.close()

    return jsonify(message="Alerts marked read")

#home page---------------------------------------------------------------------
@app.route("/api/home/admin")
@jwt_required()
def admin_home():

    user = get_current_user()

    if not user:
        return jsonify(
            error="User not found"
        ), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active"
        ), 403

    if user["role"] != "admin":
        return jsonify(
            error="Admin access required"
        ), 403

    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(
            error="Organization not configured"
        ), 403

    conn = get_db()

    try:

        home = conn.execute(
            """
            SELECT
                o.id,
                o.name,
                o.organization_code,

                (
                    SELECT COUNT(*)
                    FROM users u
                    WHERE u.organization_id = o.id
                      AND u.role IN (
                          'employee',
                          'supervisor'
                      )
                      AND u.is_active = 1
                ) AS staff_count,

                (
                    SELECT COUNT(*)
                    FROM teams t
                    WHERE t.organization_id = o.id
                ) AS team_count,

                (
                    SELECT COUNT(*)
                    FROM locations l
                    WHERE l.organization_id = o.id
                ) AS location_count,

                (
                    SELECT COUNT(*)
                    FROM zones z
                    JOIN locations l2
                        ON l2.id = z.location_id
                    WHERE l2.organization_id = o.id
                ) AS zone_count

            FROM organizations o
            WHERE o.id = ?
            """,
            (org_id,)
        ).fetchone()

        if not home:
            return jsonify(
                error="Organization not found"
            ), 404

        return jsonify(
            organization={
                "id": home["id"],
                "name": home["name"],
                "organization_code":
                    home["organization_code"]
            },
            counts={
                "staff":
                    home["staff_count"] or 0,
                "teams":
                    home["team_count"] or 0,
                "locations":
                    home["location_count"] or 0,
                "zones":
                    home["zone_count"] or 0
            }
        ), 200

    finally:
        conn.close()        
     # ============================================================
# ADMIN DASHBOARD — ADMIN ONLY
# ============================================================

@app.route("/api/admin/dashboard")
@jwt_required()
def admin_dashboard():
    user = get_current_user()

    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    if user.get("role") != "admin":
        return jsonify(error="Admin access required"), 403

    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    today = datetime.now().date()

    default_from = today - timedelta(days=6)

    from_value = request.args.get(
        "from",
        default_from.isoformat()
    )

    to_value = request.args.get(
        "to",
        today.isoformat()
    )

    try:
        from_date = datetime.strptime(
            from_value,
            "%Y-%m-%d"
        ).date()

        to_date = datetime.strptime(
            to_value,
            "%Y-%m-%d"
        ).date()

    except ValueError:
        return jsonify(
            error="Dates must use YYYY-MM-DD format"
        ), 400

    if from_date > to_date:
        return jsonify(
            error="From date cannot be after To date"
        ), 400

    period_days = (to_date - from_date).days + 1

    conn = get_db()

    try:
        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------

        summary = conn.execute(
            """
            SELECT
                COUNT(t.id) AS total_tasks,

                SUM(
                    CASE
                        WHEN t.status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed_tasks,

                SUM(
                    CASE
                        WHEN t.status = 'missed'
                        THEN 1
                        ELSE 0
                    END
                ) AS missed_tasks,

                SUM(
                    CASE
                        WHEN t.status IN ('pending', 'in-progress')
                        THEN 1
                        ELSE 0
                    END
                ) AS pending_tasks,

                AVG(
                    CASE
                        WHEN t.status = 'completed'
                        THEN t.duration_minutes
                    END
                ) AS avg_clean_time

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?
              AND DATE(t.scheduled_at)
                  BETWEEN ? AND ?
            """,
            (
                org_id,
                from_value,
                to_value
            )
        ).fetchone()

        total_tasks = summary["total_tasks"] or 0
        completed_tasks = summary["completed_tasks"] or 0
        missed_tasks = summary["missed_tasks"] or 0
        pending_tasks = summary["pending_tasks"] or 0

        if total_tasks:
            compliance = round(
                (completed_tasks / total_tasks) * 100,
                1
            )
        else:
            compliance = 0

        avg_clean_time = summary["avg_clean_time"]


        # ----------------------------------------------------
        # CURRENT ZONE STATUS
        # ----------------------------------------------------

        zone_rows = conn.execute(
            """
            SELECT
                z.status,
                COUNT(*) AS count

            FROM zones z

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?

            GROUP BY z.status
            """,
            (org_id,)
        ).fetchall()

        zone_status = {
            "cleaned": 0,
            "in_progress": 0,
            "pending": 0,
            "overdue": 0
        }

        for row in zone_rows:
            status = str(
                row["status"] or ""
            ).strip().lower()

            count = row["count"] or 0

            if status == "cleaned":
                zone_status["cleaned"] += count

            elif status in (
                "in-progress",
                "in_progress"
            ):
                zone_status["in_progress"] += count

            elif status == "overdue":
                zone_status["overdue"] += count

            else:
                zone_status["pending"] += count

        zone_status["total"] = sum(
            zone_status.values()
        )


        # ----------------------------------------------------
        # OVERDUE ZONES
        # ----------------------------------------------------

        overdue_zones = conn.execute(
            """
            SELECT COUNT(*)

            FROM zones z

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?
              AND z.status = 'overdue'
            """,
            (org_id,)
        ).fetchone()[0] or 0


        # ----------------------------------------------------
        # TREND
        # ----------------------------------------------------

        trend_rows = conn.execute(
            """
            SELECT
                DATE(t.scheduled_at) AS day,
                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN t.status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?
              AND DATE(t.scheduled_at)
                  BETWEEN ? AND ?

            GROUP BY DATE(t.scheduled_at)

            ORDER BY day ASC
            """,
            (
                org_id,
                from_value,
                to_value
            )
        ).fetchall()

        trend = []

        for row in trend_rows:
            total = row["total"] or 0
            completed = row["completed"] or 0

            if total:
                compliance_pct = round(
                    (completed / total) * 100,
                    1
                )
            else:
                compliance_pct = 0

            if period_days <= 31:
                label = datetime.strptime(
                    row["day"],
                    "%Y-%m-%d"
                ).strftime("%d %b")

            elif period_days <= 180:
                label = datetime.strptime(
                    row["day"],
                    "%Y-%m-%d"
                ).strftime("Week %W")

            else:
                label = datetime.strptime(
                    row["day"],
                    "%Y-%m-%d"
                ).strftime("%b %Y")

            trend.append({
                "label": label,
                "total": total,
                "completed": completed,
                "compliance_pct": compliance_pct
            })


        # ----------------------------------------------------
        # LOCATION PULSE
        # ----------------------------------------------------

        location_rows = conn.execute(
            """
            SELECT
                l.id,
                l.name,

                COUNT(DISTINCT z.id) AS zone_count,

                COUNT(t.id) AS total_tasks,

                SUM(
                    CASE
                        WHEN t.status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed_tasks,

                SUM(
                    CASE
                        WHEN z.status = 'overdue'
                        THEN 1
                        ELSE 0
                    END
                ) AS overdue_zones

            FROM locations l

            LEFT JOIN zones z
                ON z.location_id = l.id

            LEFT JOIN tasks t
                ON t.zone_id = z.id
               AND DATE(t.scheduled_at)
                   BETWEEN ? AND ?

            WHERE l.organization_id = ?

            GROUP BY l.id, l.name

            ORDER BY l.name

            LIMIT 8
            """,
            (
                from_value,
                to_value,
                org_id
            )
        ).fetchall()

        locations = []

        for row in location_rows:
            total = row["total_tasks"] or 0
            completed = row["completed_tasks"] or 0

            if total:
                location_compliance = round(
                    (completed / total) * 100,
                    1
                )
            else:
                location_compliance = None

            locations.append({
                "id": row["id"],
                "name": row["name"],
                "zone_count": row["zone_count"] or 0,
                "overdue_zones": row["overdue_zones"] or 0,
                "total_tasks": total,
                "completed_tasks": completed,
                "compliance_pct": location_compliance
            })


        # ----------------------------------------------------
        # STAFF OVERVIEW
        # ----------------------------------------------------

        staff_rows = conn.execute(
            """
            SELECT
                role,
                COUNT(*) AS count

            FROM users

            WHERE organization_id = ?
              AND is_active = 1
              AND role IN ('employee', 'supervisor')

            GROUP BY role
            """,
            (org_id,)
        ).fetchall()

        supervisors = 0
        employees = 0

        for row in staff_rows:
            if row["role"] == "supervisor":
                supervisors = row["count"] or 0

            elif row["role"] == "employee":
                employees = row["count"] or 0

        staff_total = supervisors + employees


        # ----------------------------------------------------
        # NEEDS ATTENTION
        # ----------------------------------------------------

        needs_attention = []

        overdue_rows = conn.execute(
            """
            SELECT
                z.name AS zone_name,
                l.name AS location_name

            FROM zones z

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?
              AND z.status = 'overdue'

            ORDER BY z.name

            LIMIT 3
            """,
            (org_id,)
        ).fetchall()

        for row in overdue_rows:
            needs_attention.append({
                "severity": "high",
                "title": "Overdue zone",
                "detail": (
                    f'{row["zone_name"]} · '
                    f'{row["location_name"]}'
                )
            })

        for location in locations:
            score = location["compliance_pct"]

            if (
                score is not None
                and score < 80
            ):
                needs_attention.append({
                    "severity": "medium",
                    "title": "Location below target",
                    "detail": (
                        f'{location["name"]} · '
                        f'{score}% compliance'
                    )
                })

        unread_alerts = conn.execute(
            """
            SELECT COUNT(*)

            FROM alerts a

            LEFT JOIN zones z
                ON z.id = a.zone_id

            LEFT JOIN locations l
                ON l.id = z.location_id

            LEFT JOIN users u
                ON u.id = a.user_id

            WHERE a.is_read = 0
              AND (
                    l.organization_id = ?
                    OR u.organization_id = ?
              )
            """,
            (
                org_id,
                org_id
            )
        ).fetchone()[0] or 0

        if unread_alerts:
            needs_attention.append({
                "severity": "medium",
                "title": "Unread alerts",
                "detail": (
                    f"{unread_alerts} "
                    f"notification"
                    f"{'' if unread_alerts == 1 else 's'}"
                )
            })

        needs_attention = needs_attention[:6]


        # ----------------------------------------------------
        # RECENT ACTIVITY
        # ----------------------------------------------------

        activity_rows = conn.execute(
            """
            SELECT
                'cleaning' AS kind,
                'Cleaning completed' AS title,
                z.name || ' · ' || l.name AS detail,
                cl.logged_at AS activity_time

            FROM cleaning_logs cl

            JOIN zones z
                ON z.id = cl.zone_id

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?

            UNION ALL

            SELECT
                'task' AS kind,

                CASE
                    WHEN t.status = 'completed'
                    THEN 'Task completed'
                    ELSE 'Task created'
                END AS title,

                z.name || ' · ' || l.name AS detail,

                COALESCE(
                    t.completed_at,
                    t.created_at
                ) AS activity_time

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            JOIN locations l
                ON l.id = z.location_id

            WHERE l.organization_id = ?

            UNION ALL

            SELECT
                'alert' AS kind,
                'Alert' AS title,
                a.message AS detail,
                a.created_at AS activity_time

            FROM alerts a

            LEFT JOIN zones z
                ON z.id = a.zone_id

            LEFT JOIN locations l
                ON l.id = z.location_id

            LEFT JOIN users u
                ON u.id = a.user_id

            WHERE
                l.organization_id = ?
                OR u.organization_id = ?

            ORDER BY activity_time DESC

            LIMIT 8
            """,
            (
                org_id,
                org_id,
                org_id,
                org_id
            )
        ).fetchall()

        recent_activity = [
            dict(row)
            for row in activity_rows
        ]


        return jsonify({
            "period": {
                "from": from_value,
                "to": to_value,
                "days": period_days
            },

            "summary": {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "missed_tasks": missed_tasks,
                "pending_tasks": pending_tasks,
                "compliance_pct": compliance,
                "overdue_zones": overdue_zones,
                "avg_clean_time": (
                    round(avg_clean_time)
                    if avg_clean_time is not None
                    else None
                )
            },

            "zone_status": zone_status,

            "trend": trend,

            "locations": locations,

            "staff": {
                "supervisors": supervisors,
                "employees": employees,
                "total": staff_total
            },

            "needs_attention": needs_attention,

            "recent_activity": recent_activity
        })

    finally:
        conn.close()
# ─── analytics ────────────────────────────────────────────────────────────────
# ============================================================
# ANALYTICS — ORGANIZATION / TEAM / EMPLOYEE SCOPED
# ============================================================

@app.route("/api/analytics/dashboard")
@jwt_required()
def analytics_dashboard():
    user = get_current_user()
    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    uid = user["id"]
    role = user["role"]
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    conn = get_db()

    try:
        if role == "admin":
            zone_scope = """
                z.location_id IN (
                    SELECT id FROM locations
                    WHERE organization_id = ?
                )
            """
            zone_params = [org_id]

            task_scope = """
                z.location_id IN (
                    SELECT id FROM locations
                    WHERE organization_id = ?
                )
            """
            task_params = [org_id]

            alert_scope = """
                (
                    a.user_id IN (
                        SELECT id FROM users
                        WHERE organization_id = ?
                    )
                    OR z.location_id IN (
                        SELECT id FROM locations
                        WHERE organization_id = ?
                    )
                )
            """
            alert_params = [org_id, org_id]

        elif role == "supervisor":
            zone_scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = z.id
                )
            """
            zone_params = [uid]

            task_scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = t.zone_id
                )
            """
            task_params = [uid]

            alert_scope = """
                (
                    a.user_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM team_zones tz
                        JOIN team_supervisors ts
                            ON ts.team_id = tz.team_id
                        WHERE ts.user_id = ?
                          AND tz.zone_id = a.zone_id
                    )
                )
            """
            alert_params = [uid, uid]

        elif role == "employee":
            zone_scope = """
                EXISTS (
                    SELECT 1
                    FROM staff_zones sz
                    WHERE sz.user_id = ?
                      AND sz.zone_id = z.id
                )
            """
            zone_params = [uid]

            task_scope = "t.assigned_to = ?"
            task_params = [uid]

            alert_scope = """
                (
                    a.user_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM staff_zones sz
                        WHERE sz.user_id = ?
                          AND sz.zone_id = a.zone_id
                    )
                )
            """
            alert_params = [uid, uid]

        else:
            return jsonify(error="Invalid role"), 403

        # -------------------------
        # ZONES
        # -------------------------
        zones = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE WHEN z.status = 'cleaned'
                    THEN 1 ELSE 0 END
                ) AS cleaned,
                SUM(
                    CASE WHEN z.status = 'overdue'
                    THEN 1 ELSE 0 END
                ) AS overdue,
                SUM(
                    CASE WHEN z.status = 'in-progress'
                    THEN 1 ELSE 0 END
                ) AS in_progress,
                SUM(
                    CASE WHEN z.status = 'pending'
                    THEN 1 ELSE 0 END
                ) AS pending
            FROM zones z
            WHERE {zone_scope}
        """, zone_params).fetchone()

        zones_data = {
            "total": zones["total"] or 0,
            "cleaned": zones["cleaned"] or 0,
            "overdue": zones["overdue"] or 0,
            "in_progress": zones["in_progress"] or 0,
            "pending": zones["pending"] or 0
        }

        # -------------------------
        # TODAY'S TASKS
        # -------------------------
        today = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE WHEN t.status = 'completed'
                    THEN 1 ELSE 0 END
                ) AS completed,
                SUM(
                    CASE WHEN t.status = 'missed'
                    THEN 1 ELSE 0 END
                ) AS missed,
                SUM(
                    CASE
                        WHEN t.status IN ('pending', 'in-progress')
                        THEN 1 ELSE 0
                    END
                ) AS pending
            FROM tasks t
            JOIN zones z ON z.id = t.zone_id
            WHERE DATE(t.scheduled_at) = DATE('now')
              AND {task_scope}
        """, task_params).fetchone()

        total_today = today["total"] or 0
        completed_today = today["completed"] or 0

        today_data = {
            "total": total_today,
            "completed": completed_today,
            "missed": today["missed"] or 0,
            "pending": today["pending"] or 0,
            "compliance_pct": (
                round((completed_today / total_today) * 100)
                if total_today else 0
            )
        }

        # -------------------------
        # AVERAGE CLEANING DURATION
        # -------------------------
        avg_duration = conn.execute(f"""
            SELECT AVG(t.duration_minutes)
            FROM tasks t
            JOIN zones z ON z.id = t.zone_id
            WHERE t.status = 'completed'
              AND t.duration_minutes IS NOT NULL
              AND {task_scope}
        """, task_params).fetchone()[0]

        # -------------------------
        # RECENT UNREAD ALERTS
        # -------------------------
        alerts = rows_to_list(conn.execute(f"""
            SELECT
                a.*,
                z.name AS zone_name
            FROM alerts a
            LEFT JOIN zones z
                ON z.id = a.zone_id
            WHERE a.is_read = 0
              AND {alert_scope}
            ORDER BY a.created_at DESC
            LIMIT 10
        """, alert_params).fetchall())

        return jsonify(
            zones=zones_data,
            today=today_data,
            avg_cleaning_duration=(
                round(avg_duration)
                if avg_duration is not None
                else None
            ),
            recent_alerts=alerts
        )

    finally:
        conn.close()


@app.route("/api/analytics/staff")
@jwt_required()
def analytics_staff():
    user = get_current_user()
    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    uid = user["id"]
    role = user["role"]
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    from_d = request.args.get(
        "from",
        (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    )

    to_d = request.args.get(
        "to",
        datetime.now().strftime("%Y-%m-%d")
    )

    conn = get_db()

    try:
        if role == "admin":
            user_scope = """
                u.organization_id = ?
                AND u.role = 'employee'
                AND u.is_active = 1
            """
            user_params = [org_id]

        elif role == "supervisor":
            user_scope = """
                u.organization_id = ?
                AND u.role = 'employee'
                AND u.is_active = 1
                AND EXISTS (
                    SELECT 1
                    FROM team_members tm
                    JOIN team_supervisors ts
                        ON ts.team_id = tm.team_id
                    WHERE ts.user_id = ?
                      AND tm.user_id = u.id
                )
            """
            user_params = [org_id, uid]

        elif role == "employee":
            user_scope = """
                u.id = ?
                AND u.organization_id = ?
            """
            user_params = [uid, org_id]

        else:
            return jsonify(error="Invalid role"), 403

        params = [from_d, to_d] + user_params

        rows = rows_to_list(conn.execute(f"""
            SELECT
                u.id,
                u.name,
                u.email,

                COUNT(t.id) AS total_tasks,

                SUM(
                    CASE WHEN t.status = 'completed'
                    THEN 1 ELSE 0 END
                ) AS completed,

                SUM(
                    CASE WHEN t.status = 'missed'
                    THEN 1 ELSE 0 END
                ) AS missed,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) AS avg_duration,

                ROUND(
                    100.0 *
                    SUM(
                        CASE WHEN t.status = 'completed'
                        THEN 1 ELSE 0 END
                    )
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) AS compliance_pct

            FROM users u

            LEFT JOIN tasks t
                ON t.assigned_to = u.id
                AND DATE(t.scheduled_at)
                    BETWEEN ? AND ?

            WHERE {user_scope}

            GROUP BY u.id
            ORDER BY u.name
        """, params).fetchall())

        return jsonify(rows)

    finally:
        conn.close()


@app.route("/api/analytics/heatmap")
@jwt_required()
def analytics_heatmap():
    user = get_current_user()
    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    uid = user["id"]
    role = user["role"]
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    try:
        days = max(1, min(int(request.args.get("days", 7)), 90))
    except ValueError:
        days = 7

    conn = get_db()

    try:
        if role == "admin":
            zone_scope = """
                z.location_id IN (
                    SELECT id FROM locations
                    WHERE organization_id = ?
                )
            """
            zone_params = [org_id]

            task_scope = "1=1"
            task_params = []

        elif role == "supervisor":
            zone_scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = z.id
                )
            """
            zone_params = [uid]

            task_scope = "1=1"
            task_params = []

        elif role == "employee":
            zone_scope = """
                EXISTS (
                    SELECT 1
                    FROM staff_zones sz
                    WHERE sz.user_id = ?
                      AND sz.zone_id = z.id
                )
            """
            zone_params = [uid]

            task_scope = "t.assigned_to = ?"
            task_params = [uid]

        else:
            return jsonify(error="Invalid role"), 403

        # IMPORTANT:
        # SQL placeholder order is:
        # days -> task scope -> zone scope
        params = [days] + task_params + zone_params

        rows = rows_to_list(conn.execute(f"""
            SELECT
                z.id,
                z.name,
                z.floor,
                l.name AS location_name,

                COUNT(t.id) AS total_tasks,

                SUM(
                    CASE WHEN t.status = 'missed'
                    THEN 1 ELSE 0 END
                ) AS missed_count,

                SUM(
                    CASE WHEN t.status = 'completed'
                    THEN 1 ELSE 0 END
                ) AS completed_count,

                ROUND(
                    100.0 *
                    SUM(
                        CASE WHEN t.status = 'completed'
                        THEN 1 ELSE 0 END
                    )
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) AS compliance_pct,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) AS avg_duration,

                z.status AS current_status

            FROM zones z

            LEFT JOIN locations l
                ON l.id = z.location_id

            LEFT JOIN tasks t
                ON t.zone_id = z.id
                AND t.scheduled_at >= datetime(
                    'now',
                    '-' || ? || ' days'
                )
                AND {task_scope}

            WHERE {zone_scope}

            GROUP BY z.id
            ORDER BY missed_count DESC
        """, params).fetchall())

        return jsonify(rows)

    finally:
        conn.close()


@app.route("/api/analytics/reports")
@jwt_required()
def analytics_reports():
    user = get_current_user()
    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    uid = user["id"]
    role = user["role"]
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    period = request.args.get("period", "daily")

    group_expression = {
        "weekly": "strftime('%Y-W%W', t.scheduled_at)",
        "monthly": "strftime('%Y-%m', t.scheduled_at)",
        "daily": "DATE(t.scheduled_at)"
    }.get(period, "DATE(t.scheduled_at)")

    conn = get_db()

    try:
        if role == "admin":
            scope = """
                z.location_id IN (
                    SELECT id FROM locations
                    WHERE organization_id = ?
                )
            """
            params = [org_id]

        elif role == "supervisor":
            scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = t.zone_id
                )
            """
            params = [uid]

        elif role == "employee":
            scope = "t.assigned_to = ?"
            params = [uid]

        else:
            return jsonify(error="Invalid role"), 403

        rows = rows_to_list(conn.execute(f"""
            SELECT
                {group_expression} AS period,

                COUNT(t.id) AS total,

                SUM(
                    CASE WHEN t.status = 'completed'
                    THEN 1 ELSE 0 END
                ) AS completed,

                SUM(
                    CASE WHEN t.status = 'missed'
                    THEN 1 ELSE 0 END
                ) AS missed,

                ROUND(
                    100.0 *
                    SUM(
                        CASE WHEN t.status = 'completed'
                        THEN 1 ELSE 0 END
                    )
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) AS compliance_pct,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) AS avg_duration

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            WHERE {scope}

            GROUP BY {group_expression}
            ORDER BY period DESC
            LIMIT 30
        """, params).fetchall())

        return jsonify(rows)

    finally:
        conn.close()


@app.route("/api/analytics/kpis")
@jwt_required()
def analytics_kpis():
    user = get_current_user()
    if not user:
        return jsonify(error="User not found"), 404

    if not require_account_status(user):
        return jsonify(error="Account is not active"), 403

    uid = user["id"]
    role = user["role"]
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(error="Organization not configured"), 403

    conn = get_db()

    try:
        if role == "admin":
            task_scope = """
                z.location_id IN (
                    SELECT id FROM locations
                    WHERE organization_id = ?
                )
            """
            scope_params = [org_id]

        elif role == "supervisor":
            task_scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = t.zone_id
                )
            """
            scope_params = [uid]

        elif role == "employee":
            task_scope = "t.assigned_to = ?"
            scope_params = [uid]

        else:
            return jsonify(error="Invalid role"), 403

        def compliance(days):
            row = conn.execute(f"""
                SELECT
                    ROUND(
                        100.0 *
                        SUM(
                            CASE WHEN t.status = 'completed'
                            THEN 1 ELSE 0 END
                        )
                        / NULLIF(COUNT(*), 0),
                        1
                    ) AS pct

                FROM tasks t

                JOIN zones z
                    ON z.id = t.zone_id

                WHERE t.scheduled_at >= datetime(
                    'now',
                    '-' || ? || ' days'
                )

                AND {task_scope}
            """, [days] + scope_params).fetchone()

            return row["pct"] or 0

        avg_duration = conn.execute(f"""
            SELECT AVG(t.duration_minutes)

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            WHERE t.status = 'completed'
              AND t.duration_minutes IS NOT NULL
              AND t.scheduled_at >= datetime('now', '-30 days')
              AND {task_scope}
        """, scope_params).fetchone()[0]

        warnings = rows_to_list(conn.execute(f"""
            SELECT
                z.id,
                z.name,
                COUNT(t.id) AS missed_count

            FROM zones z

            JOIN tasks t
                ON t.zone_id = z.id

            WHERE t.status = 'missed'
              AND t.scheduled_at >= datetime('now', '-7 days')
              AND {task_scope}

            GROUP BY z.id

            HAVING COUNT(t.id) >= 2

            ORDER BY missed_count DESC
        """, scope_params).fetchall())

        return jsonify(
            compliance_7d=compliance(7),
            compliance_30d=compliance(30),
            avg_duration_30d=(
                round(avg_duration)
                if avg_duration is not None
                else None
            ),
            predictive_warnings=warnings
        )

    finally:
        conn.close()

#pdf reports________________________________________________________________________________
# ============================================================
# PDF REPORTS — ORGANIZATION / TEAM / EMPLOYEE SCOPED
# ============================================================

@app.route(
    "/api/reports/pdf",
    methods=["GET"]
)
@jwt_required()
def export_pdf():

    user = get_current_user()

    if not user:
        return jsonify(
            error="User not found"
        ), 404

    if not require_account_status(user):
        return jsonify(
            error="Account is not active"
        ), 403

    uid = user.get("id")
    role = user.get("role")
    org_id = user.get("organization_id")

    if not org_id:
        return jsonify(
            error="Organization not configured"
        ), 403

    conn = get_db()

    try:

        # -----------------------------------------------------
        # Determine report scope
        # -----------------------------------------------------

        if role == "admin":

            scope = """
                z.location_id IN (
                    SELECT id
                    FROM locations
                    WHERE organization_id = ?
                )
            """

            params = [
                org_id
            ]

            title = (
                "CleanTrack Organization Report"
            )

        elif role == "supervisor":

            scope = """
                EXISTS (
                    SELECT 1
                    FROM team_zones tz
                    JOIN team_supervisors ts
                        ON ts.team_id = tz.team_id
                    WHERE ts.user_id = ?
                      AND tz.zone_id = t.zone_id
                )
            """

            params = [
                uid
            ]

            title = (
                "CleanTrack Team Report"
            )

        elif role == "employee":

            scope = """
                t.assigned_to = ?
                AND z.location_id IN (
                    SELECT id
                    FROM locations
                    WHERE organization_id = ?
                )
            """

            params = [
                uid,
                org_id
            ]

            title = (
                "CleanTrack Personal Report"
            )

        else:

            return jsonify(
                error="Invalid role"
            ), 403


        # -----------------------------------------------------
        # Task statistics
        # -----------------------------------------------------

        stats = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN t.status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed,

                SUM(
                    CASE
                        WHEN t.status = 'missed'
                        THEN 1
                        ELSE 0
                    END
                ) AS missed,

                SUM(
                    CASE
                        WHEN t.status IN (
                            'pending',
                            'in-progress'
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS pending,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) AS avg_duration

            FROM tasks t

            JOIN zones z
                ON z.id = t.zone_id

            WHERE {scope}
            """,
            params
        ).fetchone()


        total = (
            stats["total"]
            or 0
        )

        completed = (
            stats["completed"]
            or 0
        )

        missed = (
            stats["missed"]
            or 0
        )

        pending = (
            stats["pending"]
            or 0
        )

        avg_duration = (
            stats["avg_duration"]
        )


        compliance = (
            round(
                (
                    completed /
                    total
                ) * 100,
                1
            )
            if total
            else 0
        )


        # -----------------------------------------------------
        # Recent tasks
        # -----------------------------------------------------

        recent_tasks = rows_to_list(
            conn.execute(
                f"""
                SELECT
                    t.id,
                    t.status,
                    t.scheduled_at,
                    t.completed_at,
                    t.duration_minutes,
                    z.name AS zone_name,
                    u.name AS employee_name

                FROM tasks t

                JOIN zones z
                    ON z.id = t.zone_id

                LEFT JOIN users u
                    ON u.id = t.assigned_to

                WHERE {scope}

                ORDER BY
                    t.scheduled_at DESC

                LIMIT 20
                """,
                params
            ).fetchall()
        )


        # -----------------------------------------------------
        # Create PDF
        # -----------------------------------------------------

        buffer = BytesIO()

        doc = SimpleDocTemplate(
            buffer,
            rightMargin=40,
            leftMargin=40,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()


        content = []


        content.append(
            Paragraph(
                title,
                styles["Title"]
            )
        )

        content.append(
            Spacer(
                1,
                15
            )
        )

        content.append(
            Paragraph(
                f"Generated for: "
                f"{user.get('name') or 'User'}",
                styles["Normal"]
            )
        )

        content.append(
            Spacer(
                1,
                15
            )
        )

        content.append(
            Paragraph(
                f"Total Tasks: {total}",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Completed Tasks: {completed}",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Missed Tasks: {missed}",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Pending Tasks: {pending}",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Compliance: {compliance}%",
                styles["Normal"]
            )
        )

        average_duration_text = (
            f"{round(avg_duration)} minutes"
            if avg_duration is not None
            else "N/A"
        )

        content.append(
            Paragraph(
                f"Average Cleaning Duration: "
                f"{average_duration_text}",
                styles["Normal"]
            )
        )

        content.append(
            Spacer(
                1,
                20
            )
        )

        content.append(
            Paragraph(
                "Recent Tasks",
                styles["Heading2"]
            )
        )

        content.append(
            Spacer(
                1,
                10
            )
        )


        # -----------------------------------------------------
        # Add recent task rows
        # -----------------------------------------------------

        if recent_tasks:

            for task in recent_tasks:

                employee_name = (
                    task["employee_name"]
                    if task["employee_name"]
                    else "Unassigned"
                )

                zone_name = (
                    task["zone_name"]
                    if task["zone_name"]
                    else "Unknown Zone"
                )

                scheduled = (
                    task["scheduled_at"]
                    if task["scheduled_at"]
                    else "N/A"
                )

                status = (
                    task["status"]
                    if task["status"]
                    else "Unknown"
                )

                content.append(
                    Paragraph(
                        f"{zone_name} — "
                        f"{employee_name} — "
                        f"{status} — "
                        f"{scheduled}",
                        styles["Normal"]
                    )
                )

        else:

            content.append(
                Paragraph(
                    "No recent tasks found.",
                    styles["Normal"]
                )
            )


        # -----------------------------------------------------
        # Build PDF
        # -----------------------------------------------------

        doc.build(
            content
        )

        buffer.seek(0)


        return send_file(
            buffer,
            as_attachment=True,
            download_name="cleantrack_report.pdf",
            mimetype="application/pdf"
        )


    finally:

        conn.close()
# ─── uploads ─────────────────────────────────────────────────────────────────

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ─── SPA fallback ────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    full = os.path.join(app.static_folder, "frontend", path)
    if path and os.path.exists(full):
        return send_from_directory(os.path.join(app.static_folder, "frontend"), path)
    return send_from_directory(os.path.join(app.static_folder, "frontend"), "index.html")

# ─── scheduler ───────────────────────────────────────────────────────────────

def generate_hourly_tasks():
    conn = get_db()
    zones = rows_to_list(conn.execute("SELECT * FROM zones").fetchall())
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    for z in zones:
        exists = conn.execute(
            "SELECT id FROM tasks WHERE zone_id=? AND strftime('%Y-%m-%d %H',scheduled_at)=strftime('%Y-%m-%d %H','now')", (z["id"],)
        ).fetchone()
        if not exists:
            assigned = conn.execute("SELECT user_id FROM staff_zones WHERE zone_id=? LIMIT 1", (z["id"],)).fetchone()
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                         (str(uuid.uuid4()), z["id"], assigned["user_id"] if assigned else None,
                          "pending", now.isoformat(), None, None, None, None, 0, 0))
    conn.commit(); conn.close()

def check_overdue():
    conn = get_db()
    overdue = rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE status IN ('pending','in-progress') AND scheduled_at < datetime('now','-90 minutes') AND is_overdue=0"
    ).fetchall())
    for t in overdue:
        conn.execute("UPDATE tasks SET is_overdue=1, status='missed', overdue_count=overdue_count+1 WHERE id=?", (t["id"],))
        conn.execute("UPDATE zones SET status='overdue' WHERE id=?", (t["zone_id"],))
        zone = row_to_dict(conn.execute("SELECT name FROM zones WHERE id=?", (t["zone_id"],)).fetchone())
        conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (str(uuid.uuid4()), "overdue_cleaning", "warning", t["zone_id"], None, t["id"],
                      f'Zone "{zone["name"] if zone else ""}" cleaning overdue by 90+ minutes.', 0))
    conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(generate_hourly_tasks, "cron", minute=0)
scheduler.add_job(check_overdue, "interval", minutes=15)

# ─── run ──────────────────────────────────────────────────────────────────────

init_db()


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    host = os.environ.get(
        "HOST",
        "0.0.0.0"
    )


    # Run seed only when explicitly requested.
    if os.environ.get(
        "CLEANTRACK_SEED",
        "0"
    ) == "1":

        from seed import seed

        seed()


    # Start background jobs only for the local
    # direct Python process.
    try:

        scheduler.start()

    except Exception as error:

        print(
            f"Scheduler startup warning: {error}"
        )


    print(
        f"\nCleanTrack API -> "
        f"http://{host}:{port}"
    )


    app.run(
        host=host,
        port=port,
        debug=False,
        use_reloader=False
    )