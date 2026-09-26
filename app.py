import os, uuid, random, io, base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import qrcode
import resend

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
from reportlab.platypus import SimpleDocTemplate, Paragraph
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

# Explicitly trust both localhost and 127.0.0.1 variants of your dashboard
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "https://cleantrack-frontend-ry0o.onrender.com"
            ]
        }
    },
    supports_credentials=True
)
jwt = JWTManager(app)

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
    response.headers["Access-Control-Allow-Origin"] = "https://cleantrack-frontend-ry0o.onrender.com"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        res = jsonify({})
        res.headers["Access-Control-Allow-Origin"] = "https://cleantrack-frontend-ry0o.onrender.com"
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
    email = data.get("email", "").strip()
    password = data.get("password", "")
    if not email or not password:
        return jsonify(error="Email and password required"), 400
    conn = get_db()
    user = row_to_dict(conn.execute(
        "SELECT * FROM users WHERE email=? AND is_active=1", (email,)
    ).fetchone())
    conn.close()
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify(error="Invalid credentials"), 401
    token = create_access_token(
        identity=user["id"],
        additional_claims={"role": user["role"], "name": user["name"], "email": user["email"], "location_id": user.get("location_id")}
    )
    return jsonify(token=token, user={k: user[k] for k in ("id","name","email","role","location_id")})

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    name, email, password = data.get("name"), data.get("email"), data.get("password")
    if not name or not email or not password:
        return jsonify(error="name, email, password required"), 400
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return jsonify(error="Email already registered"), 409
    uid = str(uuid.uuid4())
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn.execute("""
    INSERT INTO users
    (id, name, email, notification_email, phone, password_hash, role, location_id, is_active)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    uid,
    name,
    email,
    data.get("notification_email"),
    data.get("phone"),
    hashed,
    data.get("role", "staff"),
    data.get("location_id"),
    1
))
    conn.commit(); conn.close()
    return jsonify(id=uid), 201

@app.route("/api/auth/me")
@jwt_required()
def me():
    uid = get_jwt_identity()
    conn = get_db()
    user = row_to_dict(conn.execute(
        "SELECT id,name,email,phone,role,location_id,created_at FROM users WHERE id=?", (uid,)
    ).fetchone())
    conn.close()
    return jsonify(user)
# ─── locations ────────────────────────────────────────────────────────────────

@app.route("/api/locations", methods=["GET"])
@jwt_required()
def get_locations():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    # Admin can see all locations
    if role == "admin":
        rows = rows_to_list(conn.execute("""
            SELECT *
            FROM locations
            ORDER BY name
        """).fetchall())

    # Supervisor can only see their own location
    elif role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        rows = rows_to_list(conn.execute("""
            SELECT *
            FROM locations
            WHERE id=?
            ORDER BY name
        """, (supervisor["location_id"],)).fetchall())

    # Staff can only see their own location
    else:
        staff = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if not staff or not staff["location_id"]:
            conn.close()
            return jsonify([])

        rows = rows_to_list(conn.execute("""
            SELECT *
            FROM locations
            WHERE id=?
            ORDER BY name
        """, (staff["location_id"],)).fetchall())

    conn.close()

    return jsonify(rows)
#create-locations**************************************************************
@app.route("/api/locations", methods=["POST"])
@jwt_required()
@require_roles("admin")
def create_location():
    d = request.get_json() or {}

    if not d.get("name"):
        return jsonify(error="name required"), 400

    lid = str(uuid.uuid4())

    conn = get_db()

    conn.execute(
        "INSERT INTO locations VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            lid,
            d["name"],
            d.get("address"),
            d.get("city"),
            d.get("country")
        )
    )

    conn.commit()
    conn.close()

    return jsonify(id=lid), 201
#update-locations**************************************************************
@app.route("/api/locations/<lid>", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def update_location(lid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    conn = get_db()

    location = conn.execute("""
        SELECT id
        FROM locations
        WHERE id=?
    """, (lid,)).fetchone()

    if not location:
        conn.close()
        return jsonify(error="Location not found"), 404

    # Supervisor can only update their assigned location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or supervisor["location_id"] != lid
        ):
            conn.close()
            return jsonify(error="You can only update your location"), 403

    conn.execute(
        """UPDATE locations
           SET name=COALESCE(?,name),
               address=COALESCE(?,address),
               city=COALESCE(?,city),
               country=COALESCE(?,country)
           WHERE id=?""",
        (
            d.get("name"),
            d.get("address"),
            d.get("city"),
            d.get("country"),
            lid
        )
    )

    conn.commit()
    conn.close()

    return jsonify(message="Updated")
#delete-locations**************************************************************
@app.route("/api/locations/<lid>", methods=["DELETE"])
@jwt_required()
@require_roles("admin")
def delete_location(lid):
    conn = get_db()

    location = conn.execute("""
        SELECT id
        FROM locations
        WHERE id=?
    """, (lid,)).fetchone()

    if not location:
        conn.close()
        return jsonify(error="Location not found"), 404

    conn.execute(
        "DELETE FROM locations WHERE id=?",
        (lid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Deleted")
# ─── zones ────────────────────────────────────────────────────────────────────
@app.route("/api/zones", methods=["GET"])
@jwt_required()
def get_zones():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    requested_loc = request.args.get("location_id")

    conn = get_db()

    # ---------------------------------------------------------
    # Admin — all zones, optional location filter
    # ---------------------------------------------------------
    if role == "admin":

        q = """
            SELECT z.*, l.name location_name,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.status='pending') pending_tasks,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.is_overdue=1) overdue_tasks
            FROM zones z
            LEFT JOIN locations l ON z.location_id=l.id
        """

        if requested_loc:
            q += " WHERE z.location_id=? ORDER BY z.name"
            params = (requested_loc,)
        else:
            q += " ORDER BY z.name"
            params = ()

    # ---------------------------------------------------------
    # Supervisor — zones in their own location
    # ---------------------------------------------------------
    elif role == "supervisor":

        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q = """
            SELECT z.*, l.name location_name,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.status='pending') pending_tasks,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.is_overdue=1) overdue_tasks
            FROM zones z
            LEFT JOIN locations l ON z.location_id=l.id
            WHERE z.location_id=?
            ORDER BY z.name
        """

        params = (supervisor["location_id"],)

    # ---------------------------------------------------------
    # Staff — only zones explicitly assigned to them
    # ---------------------------------------------------------
    else:

        q = """
            SELECT z.*, l.name location_name,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.status='pending') pending_tasks,
              (SELECT COUNT(*)
               FROM tasks t
               WHERE t.zone_id=z.id
                 AND t.is_overdue=1) overdue_tasks
            FROM zones z
            JOIN staff_zones sz
              ON sz.zone_id=z.id
             AND sz.user_id=?
            LEFT JOIN locations l ON z.location_id=l.id
            ORDER BY z.name
        """

        params = (uid,)

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)
#get-zones-zid&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&
@app.route("/api/zones/<zid>", methods=["GET"])
@jwt_required()
def get_zone(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    row = conn.execute("""
        SELECT
            z.*,
            l.name location_name
        FROM zones z
        LEFT JOIN locations l
            ON z.location_id=l.id
        WHERE z.id=?
    """, (zid,)).fetchone()

    if not row:
        conn.close()
        return jsonify(error="Not found"), 404

    # ---------------------------------------------------------
    # Admin can view any zone
    # ---------------------------------------------------------

    if role == "admin":
        result = row_to_dict(row)
        conn.close()
        return jsonify(result)

    # ---------------------------------------------------------
    # Supervisor can view zones in their location
    # ---------------------------------------------------------

    if role == "supervisor":

        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or row["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="Access denied"), 403

    # ---------------------------------------------------------
    # Staff can only view assigned zones
    # ---------------------------------------------------------

    else:

        assigned = conn.execute("""
            SELECT 1
            FROM staff_zones
            WHERE user_id=?
              AND zone_id=?
        """, (uid, zid)).fetchone()

        if not assigned:
            conn.close()
            return jsonify(error="Access denied"), 403

    result = row_to_dict(row)

    conn.close()

    return jsonify(result)
#create_zones___________________________________________________________________
@app.route("/api/zones", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_zone():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    if not d.get("location_id") or not d.get("name"):
        return jsonify(error="location_id and name required"), 400

    conn = get_db()

    # Verify the location exists
    location = conn.execute("""
        SELECT id
        FROM locations
        WHERE id=?
    """, (d["location_id"],)).fetchone()

    if not location:
        conn.close()
        return jsonify(error="Location not found"), 404

    # Supervisor can only create zones in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or d["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="You can only create zones in your location"), 403

    zid = str(uuid.uuid4())

    qr = gen_qr_b64(
        f'{{"zoneId":"{zid}","name":"{d["name"]}"}}'
    )

    conn.execute(
        "INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
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

    conn.commit()
    conn.close()

    return jsonify(
        id=zid,
        qr_code=qr
    ), 201
#update_zones_____________________________________________________________________
@app.route("/api/zones/<zid>", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def update_zone(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    conn = get_db()

    zone = conn.execute("""
        SELECT id, location_id
        FROM zones
        WHERE id=?
    """, (zid,)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Zone not found"), 404

    # Supervisor can only update zones in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="You can only update zones in your location"), 403

    conn.execute(
        """UPDATE zones
           SET name=COALESCE(?,name),
               floor=COALESCE(?,floor),
               type=COALESCE(?,type),
               cleaning_interval_minutes=COALESCE(?,cleaning_interval_minutes),
               status=COALESCE(?,status)
           WHERE id=?""",
        (
            d.get("name"),
            d.get("floor"),
            d.get("type"),
            d.get("cleaning_interval_minutes"),
            d.get("status"),
            zid
        )
    )

    conn.commit()
    conn.close()

    return jsonify(message="Updated")
#delete_zones______________________________________________________________________
@app.route("/api/zones/<zid>", methods=["DELETE"])
@jwt_required()
@require_roles("admin", "supervisor")
def delete_zone(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    zone = conn.execute("""
        SELECT id, location_id
        FROM zones
        WHERE id=?
    """, (zid,)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Zone not found"), 404

    # Supervisor can only delete zones in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="You can only delete zones in your location"), 403

    conn.execute(
        "DELETE FROM zones WHERE id=?",
        (zid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Deleted")
#get-api zones-zid-qr&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&
@app.route("/api/zones/<zid>/qr")
@jwt_required()
def zone_qr(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    zone = conn.execute("""
        SELECT id, location_id, qr_code
        FROM zones
        WHERE id=?
    """, (zid,)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Not found"), 404

    # ---------------------------------------------------------
    # Admin can access any zone QR
    # ---------------------------------------------------------

    if role == "admin":
        pass

    # ---------------------------------------------------------
    # Supervisor can access QR for their location
    # ---------------------------------------------------------

    elif role == "supervisor":

        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="Access denied"), 403

    # ---------------------------------------------------------
    # Staff can access QR only for assigned zones
    # ---------------------------------------------------------

    else:

        assigned = conn.execute("""
            SELECT 1
            FROM staff_zones
            WHERE user_id=?
              AND zone_id=?
        """, (uid, zid)).fetchone()

        if not assigned:
            conn.close()
            return jsonify(error="Access denied"), 403

    qr_code = zone["qr_code"]

    conn.close()

    return jsonify(qr_code=qr_code)
#get-ap zones-zid-staff&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&
@app.route("/api/zones/<zid>/staff")
@jwt_required()
def zone_staff(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    zone = conn.execute("""
        SELECT id, location_id
        FROM zones
        WHERE id=?
    """, (zid,)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Zone not found"), 404

    # ---------------------------------------------------------
    # Admin can view staff for any zone
    # ---------------------------------------------------------

    if role == "admin":
        pass

    # ---------------------------------------------------------
    # Supervisor can view staff for zones in their location
    # ---------------------------------------------------------

    elif role == "supervisor":

        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="Access denied"), 403

    # ---------------------------------------------------------
    # Staff can only view their own assignment
    # ---------------------------------------------------------

    else:

        assigned = conn.execute("""
            SELECT 1
            FROM staff_zones
            WHERE user_id=?
              AND zone_id=?
        """, (uid, zid)).fetchone()

        if not assigned:
            conn.close()
            return jsonify(error="Access denied"), 403

        # Staff should not receive the entire staff directory
        rows = rows_to_list(conn.execute("""
            SELECT
                u.id,
                u.name,
                sz.shift,
                sz.assigned_at
            FROM staff_zones sz
            JOIN users u
                ON sz.user_id=u.id
            WHERE sz.zone_id=?
              AND sz.user_id=?
        """, (zid, uid)).fetchall())

        conn.close()
        return jsonify(rows)

    # ---------------------------------------------------------
    # Admin / Supervisor
    # ---------------------------------------------------------

    rows = rows_to_list(conn.execute("""
        SELECT
            u.id,
            u.name,
            u.email,
            u.phone,
            sz.shift,
            sz.assigned_at
        FROM staff_zones sz
        JOIN users u
            ON sz.user_id=u.id
        WHERE sz.zone_id=?
        ORDER BY u.name
    """, (zid,)).fetchall())

    conn.close()

    return jsonify(rows)
#assign_zone task___________________________________________________________________
@app.route("/api/zones/<zid>/assign", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def assign_zone(zid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    if not d.get("user_id"):
        return jsonify(error="user_id required"), 400

    conn = get_db()

    # Verify the zone exists
    zone = conn.execute("""
        SELECT id, location_id
        FROM zones
        WHERE id=?
    """, (zid,)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Zone not found"), 404

    # Verify the user exists
    user = conn.execute("""
        SELECT id, role, location_id
        FROM users
        WHERE id=?
    """, (d["user_id"],)).fetchone()

    if not user:
        conn.close()
        return jsonify(error="User not found"), 404

    # Only staff can be assigned to zones
    if user["role"] != "staff":
        conn.close()
        return jsonify(error="Only staff can be assigned to zones"), 400

    # Staff and zone must belong to the same location
    if user["location_id"] != zone["location_id"]:
        conn.close()
        return jsonify(error="Staff and zone must belong to the same location"), 400

    # Supervisor can only assign zones in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="You can only manage zones in your location"), 403

    conn.execute(
        "DELETE FROM staff_zones WHERE user_id=? AND zone_id=?",
        (d["user_id"], zid)
    )

    conn.execute(
        "INSERT INTO staff_zones VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
        (
            str(uuid.uuid4()),
            d["user_id"],
            zid,
            d.get("shift", "morning")
        )
    )

    conn.commit()
    conn.close()

    return jsonify(message="Assigned"), 201

# ─── users ────────────────────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@jwt_required()
def get_users():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    # Staff should not access the user directory
    if role == "staff":
        conn.close()
        return jsonify(error="Access denied"), 403

    q = """
        SELECT id, name, email, notification_email, phone,
               role, location_id, is_active, created_at
        FROM users
        WHERE 1=1
    """

    params = []

    # Supervisor can only see staff in their own location
    if role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q += " AND location_id=? AND role='staff'"
        params.append(supervisor["location_id"])

    # Optional filters
    requested_role = request.args.get("role")
    requested_location = request.args.get("location_id")

    if requested_role and role == "admin":
        q += " AND role=?"
        params.append(requested_role)

    if requested_location and role == "admin":
        q += " AND location_id=?"
        params.append(requested_location)

    q += " ORDER BY name"

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)
#create*users##################################################################
@app.route("/api/users", methods=["POST"])
@jwt_required()
@require_roles("admin")
def create_user():
    d = request.get_json() or {}
    if not d.get("name") or not d.get("email") or not d.get("password"):
        return jsonify(error="name, email, password required"), 400
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (d["email"],)).fetchone():
        conn.close()
        return jsonify(error="Email exists"), 409
    uid = str(uuid.uuid4())
    hashed = bcrypt.hashpw(d["password"].encode(), bcrypt.gensalt()).decode()
    conn.execute("""
    INSERT INTO users
    (id, name, email, notification_email, phone, password_hash, role, location_id, is_active)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    uid,
    d["name"],
    d["email"],
    d.get("notification_email"),
    d.get("phone"),
    hashed,
    "staff",
    d.get("location_id"),
    1
))
    conn.commit(); conn.close()
    return jsonify(id=uid), 201

#staff user details
#update*users##################################################################
@app.route("/api/users/<uid>", methods=["PUT"])
@jwt_required()
@require_roles("admin")
def update_user(uid):
    d = request.get_json() or {}

    conn = get_db()

    # Prevent duplicate email addresses
    if d.get("email"):
        existing = conn.execute(
            "SELECT id FROM users WHERE email=? AND id!=?",
            (d["email"].strip(), uid)
        ).fetchone()

        if existing:
            conn.close()
            return jsonify(error="Email already in use"), 409

    conn.execute("""
        UPDATE users
        SET
            name = COALESCE(?, name),
            email = COALESCE(?, email),
            notification_email = COALESCE(?, notification_email),
            phone = COALESCE(?, phone),
            role = COALESCE(?, role),
            location_id = COALESCE(?, location_id),
            is_active = COALESCE(?, is_active)
        WHERE id=?
    """, (
        d.get("name"),
        d.get("email"),
        d.get("notification_email"),
        d.get("phone"),
        d.get("role"),
        d.get("location_id"),
        d.get("is_active"),
        uid
    ))

    conn.commit()
    conn.close()

    return jsonify(message="Updated")

# ─── tasks ────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["GET"])
@jwt_required()
def get_tasks():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    args = request.args

    conn = get_db()

    q = """
        SELECT
            t.*,
            z.name zone_name,
            z.floor,
            l.name location_name,
            u.name staff_name,
            u.email staff_email
        FROM tasks t
        LEFT JOIN zones z
            ON t.zone_id=z.id
        LEFT JOIN locations l
            ON z.location_id=l.id
        LEFT JOIN users u
            ON t.assigned_to=u.id
        WHERE 1=1
    """

    params = []

    # ---------------------------------------------------------
    # STAFF — only their own tasks
    # ---------------------------------------------------------

    if role == "staff":

        q += " AND t.assigned_to=?"
        params.append(uid)

    # ---------------------------------------------------------
    # SUPERVISOR — only tasks in their location
    # ---------------------------------------------------------

    elif role == "supervisor":

        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q += " AND z.location_id=?"
        params.append(supervisor["location_id"])

    # ---------------------------------------------------------
    # Optional filters
    # ---------------------------------------------------------

    if args.get("zone_id"):
        q += " AND t.zone_id=?"
        params.append(args["zone_id"])

    if args.get("assigned_to"):

        # Staff cannot override their own scope
        if role == "staff":
            if args["assigned_to"] != uid:
                conn.close()
                return jsonify(error="Access denied"), 403

        q += " AND t.assigned_to=?"
        params.append(args["assigned_to"])

    if args.get("status"):
        q += " AND t.status=?"
        params.append(args["status"])

    # Only Admin can explicitly filter another location
    if args.get("location_id"):

        if role == "admin":
            q += " AND z.location_id=?"
            params.append(args["location_id"])

        elif role == "supervisor":
            # Supervisor is already restricted to own location.
            # Reject attempts to request another location.
            supervisor_location = conn.execute("""
                SELECT location_id
                FROM users
                WHERE id=?
            """, (uid,)).fetchone()["location_id"]

            if args["location_id"] != supervisor_location:
                conn.close()
                return jsonify(error="Access denied"), 403

    if args.get("date"):
        q += " AND DATE(t.scheduled_at)=DATE(?)"
        params.append(args["date"])

    q += """
        ORDER BY t.scheduled_at DESC
        LIMIT 200
    """

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)
#create_task-------------------------------------------------------------------

@app.route("/api/tasks", methods=["POST"])
@jwt_required()
@require_roles("admin", "supervisor")
def create_task():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    if not d.get("zone_id") or not d.get("scheduled_at"):
        return jsonify(error="zone_id and scheduled_at required"), 400

    conn = get_db()

    # Verify the zone exists and get its location
    zone = conn.execute("""
        SELECT id, location_id
        FROM zones
        WHERE id=?
    """, (d["zone_id"],)).fetchone()

    if not zone:
        conn.close()
        return jsonify(error="Zone not found"), 404

    # Supervisor can only create tasks in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or zone["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="You can only create tasks in your location"), 403

    # If a staff member is assigned, verify that the user exists
    # and belongs to the same location as the zone
    assigned_to = d.get("assigned_to")

    if assigned_to:
        assigned_user = conn.execute("""
            SELECT id, role, location_id
            FROM users
            WHERE id=?
        """, (assigned_to,)).fetchone()

        if not assigned_user:
            conn.close()
            return jsonify(error="Assigned user not found"), 404

        if assigned_user["role"] != "staff":
            conn.close()
            return jsonify(error="Tasks can only be assigned to staff"), 400

        if assigned_user["location_id"] != zone["location_id"]:
            conn.close()
            return jsonify(error="Staff and zone must belong to the same location"), 400

    tid = str(uuid.uuid4())

    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            tid,
            d["zone_id"],
            assigned_to,
            "pending",
            d["scheduled_at"],
            None,
            None,
            None,
            None,
            0,
            0
        )
    )

    conn.commit()
    conn.close()

    return jsonify(id=tid), 201
#start_task-----------------------------------------------------
@app.route("/api/tasks/<tid>/start", methods=["PUT"])
@jwt_required()
def start_task(tid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    task = row_to_dict(
        conn.execute("""
            SELECT
                t.*,
                z.location_id
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE t.id=?
        """, (tid,)).fetchone()
    )

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Only pending tasks can be started
    if task["status"] != "pending":
        conn.close()
        return jsonify(
            error="Only pending tasks can be started"
        ), 400

    # Staff can only start tasks assigned to themselves
    if role == "staff" and task["assigned_to"] != uid:
        conn.close()
        return jsonify(
            error="You can only start tasks assigned to you"
        ), 403

    # Supervisor can only start tasks in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only start tasks in your location"
            ), 403

    conn.execute(
        """UPDATE tasks
           SET status='in-progress',
               started_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (tid,)
    )

    conn.execute(
        "UPDATE zones SET status='in-progress' WHERE id=?",
        (task["zone_id"],)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Started")
#complete_task------------------------------------------------------------
@app.route("/api/tasks/<tid>/complete", methods=["PUT"])
@jwt_required()
def complete_task(tid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    d = request.get_json() or {}

    conn = get_db()

    task = row_to_dict(
        conn.execute("""
            SELECT
                t.*,
                z.location_id
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE t.id=?
        """, (tid,)).fetchone()
    )

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Only in-progress tasks can be completed
    if task["status"] != "in-progress":
        conn.close()
        return jsonify(
            error="Only in-progress tasks can be completed"
        ), 400

    # Staff can only complete tasks assigned to themselves
    if role == "staff" and task["assigned_to"] != uid:
        conn.close()
        return jsonify(
            error="You can only complete tasks assigned to you"
        ), 403

    # Supervisor can only complete tasks in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only complete tasks in your location"
            ), 403

    duration = None

    if task["started_at"]:
        try:
            start = datetime.fromisoformat(task["started_at"])
            duration = round(
                (datetime.now() - start).total_seconds() / 60,
                1
            )
        except:
            pass

    conn.execute(
        """UPDATE tasks
           SET status='pending-approval',
               completed_at=CURRENT_TIMESTAMP,
               duration_minutes=?,
               notes=?,
               assigned_to=?
           WHERE id=?""",
        (
            duration,
            d.get("notes"),
            task["assigned_to"] or uid,
            tid
        )
    )

    conn.execute(
        """UPDATE zones
           SET status='pending'
           WHERE id=?""",
        (task["zone_id"],)
    )

    conn.commit()
    conn.close()

    return jsonify(
        message="Completed and sent for approval",
        duration_minutes=duration
    )
#miss_task---------------------------------------------------------------
@app.route("/api/tasks/<tid>/miss", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def miss_task(tid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    task = row_to_dict(
        conn.execute("""
            SELECT
                t.*,
                z.location_id
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE t.id=?
        """, (tid,)).fetchone()
    )

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Only pending tasks can be marked as missed
    if task["status"] != "pending":
        conn.close()
        return jsonify(
            error="Only pending tasks can be marked as missed"
        ), 400

    # Supervisor can only mark tasks as missed in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only manage tasks in your location"
            ), 403

    new_count = (task["overdue_count"] or 0) + 1

    conn.execute(
        """UPDATE tasks
           SET status='missed',
               is_overdue=1,
               overdue_count=?
           WHERE id=?""",
        (new_count, tid)
    )

    conn.execute(
        "UPDATE zones SET status='overdue' WHERE id=?",
        (task["zone_id"],)
    )

    sev = (
        "critical"
        if new_count >= 3
        else "high"
        if new_count >= 2
        else "warning"
    )

    conn.execute(
        "INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            str(uuid.uuid4()),
            "missed_cleaning",
            sev,
            task["zone_id"],
            None,
            tid,
            f"Cleaning missed {new_count}x for zone. Immediate attention required.",
            0
        )
    )

    conn.commit()
    conn.close()

    return jsonify(
        message="Missed",
        overdue_count=new_count
    )

#overdue_tasks-----------------------------------------------------------------
@app.route("/api/tasks/overdue")
@jwt_required()
def overdue_tasks():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    q = """
        SELECT t.*, 
               z.name zone_name,
               z.floor,
               l.name location_name,
               u.name staff_name
        FROM tasks t
        LEFT JOIN zones z ON t.zone_id=z.id
        LEFT JOIN locations l ON z.location_id=l.id
        LEFT JOIN users u ON t.assigned_to=u.id
       WHERE t.is_overdue=1
    """

    params = []

    # Staff can only see their own overdue tasks
    if role == "staff":
        q += " AND t.assigned_to=?"
        params.append(uid)

    # Supervisor can only see overdue tasks in their location
    elif role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q += " AND z.location_id=?"
        params.append(supervisor["location_id"])

    q += """
        ORDER BY t.overdue_count DESC,
                 t.scheduled_at ASC
    """

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)
# ─── cleaning logs ────────────────────────────────────────────────────────────
#create^logs^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
@app.route("/api/logs", methods=["POST"])
@jwt_required()
def create_log():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    zone_id = request.form.get("zone_id")
    task_id = request.form.get("task_id")
    notes = request.form.get("notes")

    if not task_id or not zone_id:
        return jsonify(error="task_id and zone_id required"), 400

    conn = get_db()

    task = conn.execute("""
        SELECT
            t.*,
            z.location_id
        FROM tasks t
        LEFT JOIN zones z
            ON t.zone_id=z.id
        WHERE t.id=?
    """, (task_id,)).fetchone()

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Task and submitted zone must match
    if task["zone_id"] != zone_id:
        conn.close()
        return jsonify(error="Task and zone do not match"), 400

    # Only in-progress tasks can receive a cleaning log
    if task["status"] != "in-progress":
        conn.close()
        return jsonify(
            error="Only in-progress tasks can receive cleaning logs"
        ), 400

    # Staff can only submit logs for their own assigned tasks
    if role == "staff":
        if task["assigned_to"] != uid:
            conn.close()
            return jsonify(
                error="You can only submit logs for your assigned tasks"
            ), 403

    # Supervisor can only submit logs for tasks in their location
    elif role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only submit logs for tasks in your location"
            ), 403

    before_photo = after_photo = None

    for field in ("before_photo", "after_photo"):
        f = request.files.get(field)

        if f:
            fname = str(uuid.uuid4()) + os.path.splitext(f.filename)[1]
            f.save(os.path.join(UPLOAD_FOLDER, fname))

            if field == "before_photo":
                before_photo = fname
            else:
                after_photo = fname

    ai_score, ai_feedback = (None, None)

    if after_photo:
        ai_score, ai_feedback = simulate_ai_score()

    lid = str(uuid.uuid4())

    conn.execute(
        "INSERT INTO cleaning_logs VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
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

    conn.execute(
        "UPDATE tasks SET status='pending-approval', assigned_to=? WHERE id=?",
        (task["assigned_to"] or uid, task_id)
    )

    conn.execute(
        "UPDATE zones SET status='pending' WHERE id=?",
        (zone_id,)
    )

    conn.commit()
    conn.close()

    return jsonify(
        id=lid,
        ai_cleanliness_score=ai_score,
        ai_feedback=ai_feedback
    ), 201
#get^logs^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
@app.route("/api/logs", methods=["GET"])
@jwt_required()
def get_logs():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    args = request.args

    conn = get_db()

    q = """
        SELECT cl.*,
               u.name staff_name,
               u.notification_email staff_email,
               z.name zone_name,
               l.name location_name
        FROM cleaning_logs cl
        LEFT JOIN tasks t ON cl.task_id=t.id
        LEFT JOIN users u ON cl.user_id=u.id
        LEFT JOIN zones z ON cl.zone_id=z.id
        LEFT JOIN locations l ON z.location_id=l.id
        WHERE 1=1
    """

    params = []

    # Staff can only see their own logs
    if role == "staff":
        q += " AND cl.user_id=?"
        params.append(uid)

    # Supervisor can only see logs from their location
    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q += " AND z.location_id=?"
        params.append(supervisor["location_id"])

    # Optional filters
    if args.get("zone_id"):
        q += " AND cl.zone_id=?"
        params.append(args["zone_id"])

    if args.get("user_id"):
        q += " AND cl.user_id=?"
        params.append(args["user_id"])

    if args.get("task_id"):
        q += " AND cl.task_id=?"
        params.append(args["task_id"])

    q += " ORDER BY cl.logged_at DESC LIMIT 100"

    rows = rows_to_list(
        conn.execute(q, params).fetchall()
    )

    conn.close()

    return jsonify(rows)
#approve_tasks-------------------------------------------------------------------
@app.route("/api/tasks/<tid>/approve", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def approve_task(tid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    task = conn.execute("""
        SELECT
            t.id,
            t.status,
            z.location_id
        FROM tasks t
        LEFT JOIN zones z
            ON t.zone_id=z.id
        WHERE t.id=?
    """, (tid,)).fetchone()

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Only tasks waiting for approval can be approved
    if task["status"] != "pending-approval":
        conn.close()
        return jsonify(
            error="Only tasks pending approval can be approved"
        ), 400

    # Supervisor can only approve tasks in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only approve tasks in your location"
            ), 403

    conn.execute(
        "UPDATE tasks SET status='completed' WHERE id=?",
        (tid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Task approved")
#reject_tasks---------------------------------------------------------------------
@app.route("/api/tasks/<tid>/reject", methods=["PUT"])
@jwt_required()
@require_roles("admin", "supervisor")
def reject_task(tid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    task = conn.execute("""
        SELECT
            t.id,
            t.status,
            z.location_id
        FROM tasks t
        LEFT JOIN zones z
            ON t.zone_id=z.id
        WHERE t.id=?
    """, (tid,)).fetchone()

    if not task:
        conn.close()
        return jsonify(error="Task not found"), 404

    # Only tasks waiting for approval can be rejected
    if task["status"] != "pending-approval":
        conn.close()
        return jsonify(
            error="Only tasks pending approval can be rejected"
        ), 400

    # Supervisor can only reject tasks in their own location
    if role == "supervisor":
        supervisor = conn.execute("""
            SELECT location_id
            FROM users
            WHERE id=?
        """, (uid,)).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or task["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(
                error="You can only reject tasks in your location"
            ), 403

    conn.execute(
        "UPDATE tasks SET status='rejected' WHERE id=?",
        (tid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Task rejected")
# ─── alerts ────────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
@jwt_required()
def get_alerts():
    uid = get_jwt_identity()
    role = get_jwt().get("role")
    args = request.args

    conn = get_db()

    q = """
        SELECT a.*,
               z.name zone_name,
               l.name location_name
        FROM alerts a
        LEFT JOIN zones z ON a.zone_id=z.id
        LEFT JOIN locations l ON z.location_id=l.id
        WHERE 1=1
    """

    params = []

    # Staff can only see their own alerts
    if role == "staff":
        q += " AND a.user_id=?"
        params.append(uid)

    # Supervisor can see alerts from their location
    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify([])

        q += " AND z.location_id=?"
        params.append(supervisor["location_id"])

    # Optional filters
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
#mark-alert-read$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$
@app.route("/api/alerts/<aid>/read", methods=["PUT"])
@jwt_required()
def mark_alert_read(aid):
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    alert = conn.execute(
        """
        SELECT a.*, z.location_id
        FROM alerts a
        LEFT JOIN zones z ON a.zone_id=z.id
        WHERE a.id=?
        """,
        (aid,)
    ).fetchone()

    if not alert:
        conn.close()
        return jsonify(error="Alert not found"), 404

    # Staff can only mark their own alerts
    if role == "staff" and alert["user_id"] != uid:
        conn.close()
        return jsonify(error="Access denied"), 403

    # Supervisor can only mark alerts in their location
    if role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if (
            not supervisor
            or not supervisor["location_id"]
            or alert["location_id"] != supervisor["location_id"]
        ):
            conn.close()
            return jsonify(error="Access denied"), 403

    conn.execute(
        "UPDATE alerts SET is_read=1 WHERE id=?",
        (aid,)
    )

    conn.commit()
    conn.close()

    return jsonify(message="Marked read")
#mark-all-read$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$
@app.route("/api/alerts/read-all", methods=["PUT"])
@jwt_required()
def mark_all_read():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    conn = get_db()

    if role == "admin":
        conn.execute(
            "UPDATE alerts SET is_read=1"
        )

    elif role == "staff":
        conn.execute(
            "UPDATE alerts SET is_read=1 WHERE user_id=?",
            (uid,)
        )

    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        conn.execute(
            """
            UPDATE alerts
            SET is_read=1
            WHERE zone_id IN (
                SELECT id
                FROM zones
                WHERE location_id=?
            )
            """,
            (supervisor["location_id"],)
        )

    conn.commit()
    conn.close()

    return jsonify(message="Alerts marked read")

# ─── analytics ────────────────────────────────────────────────────────────────
@app.route("/api/analytics/dashboard")
@jwt_required()
def analytics_dashboard():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    requested_loc = request.args.get("location_id")

    conn = get_db()

    # ---------------------------------------------------------
    # Determine what data this user is allowed to see
    # ---------------------------------------------------------

    if role == "admin":
        # Admin can optionally filter by location
        loc = requested_loc

    elif role == "supervisor":
        # Supervisor is restricted to their own location
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        loc = supervisor["location_id"]

    else:
        # Staff dashboard is personal
        loc = None

    # ---------------------------------------------------------
    # STAFF DASHBOARD
    # ---------------------------------------------------------

    if role == "staff":

        zones = conn.execute("""
            SELECT COUNT(*) total
            FROM staff_zones
            WHERE user_id=?
        """, (uid,)).fetchone()["total"]

        cleaned = conn.execute("""
            SELECT COUNT(DISTINCT z.id) total
            FROM zones z
            JOIN staff_zones sz ON sz.zone_id=z.id
            WHERE sz.user_id=?
              AND z.status='cleaned'
        """, (uid,)).fetchone()["total"]

        overdue = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
              AND is_overdue=1
              AND status!='completed'
        """, (uid,)).fetchone()[0]

        in_progress = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
              AND status='in-progress'
        """, (uid,)).fetchone()[0]

        pending = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
              AND status IN ('pending', 'pending-approval')
        """, (uid,)).fetchone()[0]

        zones_data = {
            "total": zones,
            "cleaned": cleaned,
            "overdue": overdue,
            "in_progress": in_progress,
            "pending": pending
        }

        today = row_to_dict(conn.execute("""
            SELECT
                COUNT(*) total,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) missed,
                SUM(CASE WHEN status IN ('pending','in-progress')
                         THEN 1 ELSE 0 END) pending
            FROM tasks
            WHERE assigned_to=?
              AND DATE(scheduled_at)=DATE('now')
        """, (uid,)).fetchone()) or {}

        total = today.get("total") or 1

        today["compliance_pct"] = round(
            (today.get("completed") or 0) / total * 100
        )

        avg_dur = conn.execute("""
            SELECT AVG(duration_minutes)
            FROM tasks
            WHERE assigned_to=?
              AND status='completed'
              AND duration_minutes IS NOT NULL
        """, (uid,)).fetchone()[0]

        alerts = rows_to_list(conn.execute("""
            SELECT a.*, z.name zone_name
            FROM alerts a
            LEFT JOIN zones z ON a.zone_id=z.id
            WHERE a.user_id=?
              AND a.is_read=0
            ORDER BY a.created_at DESC
            LIMIT 10
        """, (uid,)).fetchall())

    # ---------------------------------------------------------
    # ADMIN / SUPERVISOR DASHBOARD
    # ---------------------------------------------------------

    else:

        lf = "AND z.location_id=?" if loc else ""
        p = (loc,) if loc else ()

        def count(where, params=()):
            return conn.execute(
                f"""
                SELECT COUNT(*)
                FROM zones z
                WHERE 1=1 {where}
                """,
                params
            ).fetchone()[0]

        zones_data = {
            "total": count(lf, p),
            "cleaned": count(
                f"AND z.status='cleaned' {lf}", p
            ),
            "overdue": count(
                f"AND z.status='overdue' {lf}", p
            ),
            "in_progress": count(
                f"AND z.status='in-progress' {lf}", p
            ),
            "pending": count(
                f"AND z.status='pending' {lf}", p
            ),
        }

        today_q = f"""
            SELECT
                COUNT(*) total,
                SUM(CASE WHEN t.status='completed'
                         THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN t.status='missed'
                         THEN 1 ELSE 0 END) missed,
                SUM(CASE WHEN t.status IN ('pending','in-progress')
                         THEN 1 ELSE 0 END) pending
            FROM tasks t
            LEFT JOIN zones z ON t.zone_id=z.id
            WHERE DATE(t.scheduled_at)=DATE('now') {lf}
        """

        today = row_to_dict(
            conn.execute(today_q, p).fetchone()
        ) or {}

        total = today.get("total") or 1

        today["compliance_pct"] = round(
            (today.get("completed") or 0) / total * 100
        )

        avg_dur = conn.execute(
            f"""
            SELECT AVG(t.duration_minutes)
            FROM tasks t
            LEFT JOIN zones z ON t.zone_id=z.id
            WHERE t.status='completed'
              AND t.duration_minutes IS NOT NULL
              {lf}
            """,
            p
        ).fetchone()[0]

        alerts = rows_to_list(conn.execute(
            f"""
            SELECT a.*, z.name zone_name
            FROM alerts a
            LEFT JOIN zones z ON a.zone_id=z.id
            WHERE a.is_read=0
              {lf}
            ORDER BY a.created_at DESC
            LIMIT 10
            """,
            p
        ).fetchall())

    conn.close()

    return jsonify(
        zones=zones_data,
        today=today,
        avg_cleaning_duration=round(avg_dur) if avg_dur else None,
        recent_alerts=alerts
    )
#staff analytics@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@app.route("/api/analytics/staff")
@jwt_required()
def analytics_staff():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    from_d = request.args.get(
        "from",
        (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    )
    to_d = request.args.get(
        "to",
        datetime.now().strftime("%Y-%m-%d")
    )

    conn = get_db()

    # Staff should only see their own performance
    if role == "staff":

        rows = rows_to_list(conn.execute("""
            SELECT
                u.id,
                u.name,
                u.email,
                COUNT(t.id) total_tasks,
                SUM(CASE WHEN t.status='completed'
                         THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN t.status='missed'
                         THEN 1 ELSE 0 END) missed,
                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) avg_duration,
                ROUND(
                    100.0 *
                    SUM(CASE WHEN t.status='completed'
                             THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) compliance_pct
            FROM users u
            LEFT JOIN tasks t
                ON t.assigned_to=u.id
                AND DATE(t.scheduled_at) BETWEEN ? AND ?
            WHERE u.id=?
            GROUP BY u.id
        """, (from_d, to_d, uid)).fetchall())

        conn.close()
        return jsonify(rows)

    # Supervisor can see staff in their own location
    if role == "supervisor":

        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        rows = rows_to_list(conn.execute("""
            SELECT
                u.id,
                u.name,
                u.email,
                COUNT(t.id) total_tasks,
                SUM(CASE WHEN t.status='completed'
                         THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN t.status='missed'
                         THEN 1 ELSE 0 END) missed,
                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) avg_duration,
                ROUND(
                    100.0 *
                    SUM(CASE WHEN t.status='completed'
                             THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) compliance_pct
            FROM users u
            LEFT JOIN tasks t
                ON t.assigned_to=u.id
                AND DATE(t.scheduled_at) BETWEEN ? AND ?
            WHERE u.role='staff'
              AND u.is_active=1
              AND u.location_id=?
            GROUP BY u.id
            ORDER BY compliance_pct DESC
        """, (
            from_d,
            to_d,
            supervisor["location_id"]
        )).fetchall())

        conn.close()
        return jsonify(rows)

    # Admin can see all staff
    rows = rows_to_list(conn.execute("""
        SELECT
            u.id,
            u.name,
            u.email,
            COUNT(t.id) total_tasks,
            SUM(CASE WHEN t.status='completed'
                     THEN 1 ELSE 0 END) completed,
            SUM(CASE WHEN t.status='missed'
                     THEN 1 ELSE 0 END) missed,
            AVG(
                CASE
                    WHEN t.duration_minutes IS NOT NULL
                    THEN t.duration_minutes
                END
            ) avg_duration,
            ROUND(
                100.0 *
                SUM(CASE WHEN t.status='completed'
                         THEN 1 ELSE 0 END)
                / NULLIF(COUNT(t.id), 0),
                1
            ) compliance_pct
        FROM users u
        LEFT JOIN tasks t
            ON t.assigned_to=u.id
            AND DATE(t.scheduled_at) BETWEEN ? AND ?
        WHERE u.role='staff'
          AND u.is_active=1
        GROUP BY u.id
        ORDER BY compliance_pct DESC
    """, (from_d, to_d)).fetchall())

    conn.close()
    return jsonify(rows)
#analytics heatmap@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@app.route("/api/analytics/heatmap")
@jwt_required()
def analytics_heatmap():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    days = request.args.get("days", 7)
    requested_loc = request.args.get("location_id")

    conn = get_db()

    # ---------------------------------------------------------
    # Determine allowed scope
    # ---------------------------------------------------------

    if role == "admin":
        loc = requested_loc

    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        loc = supervisor["location_id"]

    else:
        # Staff can only see zones assigned to them
        loc = None

    # ---------------------------------------------------------
    # STAFF
    # ---------------------------------------------------------

    if role == "staff":

        rows = rows_to_list(conn.execute("""
            SELECT
                z.id,
                z.name,
                z.floor,
                l.name location_name,

                COUNT(t.id) total_tasks,

                SUM(
                    CASE
                        WHEN t.status='missed'
                        THEN 1 ELSE 0
                    END
                ) missed_count,

                SUM(
                    CASE
                        WHEN t.status='completed'
                        THEN 1 ELSE 0
                    END
                ) completed_count,

                ROUND(
                    100.0 *
                    SUM(
                        CASE
                            WHEN t.status='completed'
                            THEN 1 ELSE 0
                        END
                    )
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) compliance_pct,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) avg_duration,

                z.status current_status

            FROM zones z

            JOIN staff_zones sz
                ON sz.zone_id=z.id
               AND sz.user_id=?

            LEFT JOIN locations l
                ON z.location_id=l.id

            LEFT JOIN tasks t
                ON t.zone_id=z.id
               AND t.assigned_to=?
               AND t.scheduled_at >= datetime(
                    'now',
                    '-' || ? || ' days'
               )

            GROUP BY z.id
            ORDER BY missed_count DESC
        """, (uid, uid, days)).fetchall())

        conn.close()
        return jsonify(rows)

    # ---------------------------------------------------------
    # ADMIN / SUPERVISOR
    # ---------------------------------------------------------

    lf = "AND z.location_id=?" if loc else ""
    params = [days]

    if loc:
        params.append(loc)

    rows = rows_to_list(conn.execute(f"""
        SELECT
            z.id,
            z.name,
            z.floor,
            l.name location_name,

            COUNT(t.id) total_tasks,

            SUM(
                CASE
                    WHEN t.status='missed'
                    THEN 1 ELSE 0
                END
            ) missed_count,

            SUM(
                CASE
                    WHEN t.status='completed'
                    THEN 1 ELSE 0
                END
            ) completed_count,

            ROUND(
                100.0 *
                SUM(
                    CASE
                        WHEN t.status='completed'
                        THEN 1 ELSE 0
                    END
                )
                / NULLIF(COUNT(t.id), 0),
                1
            ) compliance_pct,

            AVG(
                CASE
                    WHEN t.duration_minutes IS NOT NULL
                    THEN t.duration_minutes
                END
            ) avg_duration,

            z.status current_status

        FROM zones z

        LEFT JOIN locations l
            ON z.location_id=l.id

        LEFT JOIN tasks t
            ON t.zone_id=z.id
           AND t.scheduled_at >= datetime(
                'now',
                '-' || ? || ' days'
           )

        WHERE 1=1 {lf}

        GROUP BY z.id
        ORDER BY missed_count DESC

    """, params).fetchall())

    conn.close()
    return jsonify(rows)
#analytics reports@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@app.route("/api/analytics/reports")
@jwt_required()
def analytics_reports():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    period = request.args.get("period", "daily")
    requested_loc = request.args.get("location_id")

    conn = get_db()

    grp = {
        "weekly": "strftime('%Y-W%W',t.scheduled_at)",
        "monthly": "strftime('%Y-%m',t.scheduled_at)"
    }.get(
        period,
        "DATE(t.scheduled_at)"
    )

    # ---------------------------------------------------------
    # STAFF — personal report
    # ---------------------------------------------------------

    if role == "staff":

        rows = rows_to_list(conn.execute(f"""
            SELECT
                {grp} period,

                COUNT(t.id) total,

                SUM(
                    CASE
                        WHEN t.status='completed'
                        THEN 1 ELSE 0
                    END
                ) completed,

                SUM(
                    CASE
                        WHEN t.status='missed'
                        THEN 1 ELSE 0
                    END
                ) missed,

                ROUND(
                    100.0 *
                    SUM(
                        CASE
                            WHEN t.status='completed'
                            THEN 1 ELSE 0
                        END
                    )
                    / NULLIF(COUNT(t.id), 0),
                    1
                ) compliance_pct,

                AVG(
                    CASE
                        WHEN t.duration_minutes IS NOT NULL
                        THEN t.duration_minutes
                    END
                ) avg_duration

            FROM tasks t

            WHERE t.assigned_to=?

            GROUP BY {grp}

            ORDER BY period DESC
            LIMIT 30

        """, (uid,)).fetchall())

        conn.close()
        return jsonify(rows)

    # ---------------------------------------------------------
    # SUPERVISOR — own location
    # ---------------------------------------------------------

    if role == "supervisor":

        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        loc = supervisor["location_id"]

    # ---------------------------------------------------------
    # ADMIN — optional location filter
    # ---------------------------------------------------------

    else:
        loc = requested_loc

    lf = "AND z.location_id=?" if loc else ""
    params = (loc,) if loc else ()

    rows = rows_to_list(conn.execute(f"""
        SELECT
            {grp} period,

            COUNT(t.id) total,

            SUM(
                CASE
                    WHEN t.status='completed'
                    THEN 1 ELSE 0
                END
            ) completed,

            SUM(
                CASE
                    WHEN t.status='missed'
                    THEN 1 ELSE 0
                END
            ) missed,

            ROUND(
                100.0 *
                SUM(
                    CASE
                        WHEN t.status='completed'
                        THEN 1 ELSE 0
                    END
                )
                / NULLIF(COUNT(t.id), 0),
                1
            ) compliance_pct,

            AVG(
                CASE
                    WHEN t.duration_minutes IS NOT NULL
                    THEN t.duration_minutes
                END
            ) avg_duration

        FROM tasks t

        LEFT JOIN zones z
            ON t.zone_id=z.id

        WHERE 1=1 {lf}

        GROUP BY {grp}

        ORDER BY period DESC
        LIMIT 30

    """, params).fetchall())

    conn.close()
    return jsonify(rows)
#analytics kpis@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@app.route("/api/analytics/kpis")
@jwt_required()
def analytics_kpis():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    requested_loc = request.args.get("location_id")

    conn = get_db()

    # ---------------------------------------------------------
    # Determine allowed scope
    # ---------------------------------------------------------

    if role == "admin":
        loc = requested_loc

    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(error="Supervisor location not configured"), 403

        loc = supervisor["location_id"]

    else:
        loc = None

    # ---------------------------------------------------------
    # STAFF — personal KPIs
    # ---------------------------------------------------------

    if role == "staff":

        def compliance(days):
            row = conn.execute("""
                SELECT
                    ROUND(
                        100.0 *
                        SUM(
                            CASE
                                WHEN status='completed'
                                THEN 1 ELSE 0
                            END
                        )
                        / NULLIF(COUNT(*), 0),
                        1
                    ) pct
                FROM tasks
                WHERE assigned_to=?
                  AND scheduled_at >= datetime(
                      'now',
                      '-' || ? || ' days'
                  )
            """, (uid, days)).fetchone()

            return row["pct"] or 0

        avg_dur = conn.execute("""
            SELECT AVG(duration_minutes)
            FROM tasks
            WHERE assigned_to=?
              AND status='completed'
              AND duration_minutes IS NOT NULL
              AND scheduled_at >= datetime('now','-30 days')
        """, (uid,)).fetchone()[0]

        warnings = rows_to_list(conn.execute("""
            SELECT
                z.id,
                z.name,
                COUNT(t.id) missed_count

            FROM zones z

            JOIN staff_zones sz
                ON sz.zone_id=z.id
               AND sz.user_id=?

            JOIN tasks t
                ON t.zone_id=z.id
               AND t.assigned_to=?
               AND t.status='missed'
               AND t.scheduled_at >= datetime('now','-7 days')

            GROUP BY z.id

            HAVING missed_count >= 2

            ORDER BY missed_count DESC
        """, (uid, uid)).fetchall())

    # ---------------------------------------------------------
    # ADMIN / SUPERVISOR
    # ---------------------------------------------------------

    else:

        lf = "AND z.location_id=?" if loc else ""
        params = (loc,) if loc else ()

        def compliance(days):
            row = conn.execute(f"""
                SELECT
                    ROUND(
                        100.0 *
                        SUM(
                            CASE
                                WHEN t.status='completed'
                                THEN 1 ELSE 0
                            END
                        )
                        / NULLIF(COUNT(*), 0),
                        1
                    ) pct

                FROM tasks t

                LEFT JOIN zones z
                    ON t.zone_id=z.id

                WHERE t.scheduled_at >= datetime(
                    'now',
                    '-' || ? || ' days'
                )
                {lf}
            """, (days,) + params).fetchone()

            return row["pct"] or 0

        avg_dur = conn.execute(f"""
            SELECT AVG(t.duration_minutes)

            FROM tasks t

            LEFT JOIN zones z
                ON t.zone_id=z.id

            WHERE t.status='completed'
              AND t.duration_minutes IS NOT NULL
              AND t.scheduled_at >= datetime('now','-30 days')
              {lf}
        """, params).fetchone()[0]

        warnings = rows_to_list(conn.execute(f"""
            SELECT
                z.id,
                z.name,
                COUNT(t.id) missed_count

            FROM zones z

            JOIN tasks t
                ON t.zone_id=z.id

            WHERE t.status='missed'
              AND t.scheduled_at >= datetime('now','-7 days')
              {lf}

            GROUP BY z.id

            HAVING missed_count >= 2

            ORDER BY missed_count DESC
        """, params).fetchall())

    conn.close()

    return jsonify(
        compliance_7d=compliance(7),
        compliance_30d=compliance(30),
        avg_duration_30d=round(avg_dur) if avg_dur else None,
        predictive_warnings=warnings
    )
#analytics pdf reports@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@app.route("/api/reports/pdf")
@jwt_required()
def export_pdf():
    uid = get_jwt_identity()
    role = get_jwt().get("role")

    requested_loc = request.args.get("location_id")

    conn = get_db()

    # ---------------------------------------------------------
    # Determine allowed scope
    # ---------------------------------------------------------

    if role == "admin":
        loc = requested_loc

    elif role == "supervisor":
        supervisor = conn.execute(
            "SELECT location_id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not supervisor or not supervisor["location_id"]:
            conn.close()
            return jsonify(
                error="Supervisor location not configured"
            ), 403

        loc = supervisor["location_id"]

    else:
        loc = None

    # ---------------------------------------------------------
    # Build report data
    # ---------------------------------------------------------

    if role == "staff":

        total = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
        """, (uid,)).fetchone()[0]

        completed = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
              AND status='completed'
        """, (uid,)).fetchone()[0]

        missed = conn.execute("""
            SELECT COUNT(*)
            FROM tasks
            WHERE assigned_to=?
              AND status='missed'
        """, (uid,)).fetchone()[0]

    else:

        lf = "AND z.location_id=?" if loc else ""
        params = (loc,) if loc else ()

        total = conn.execute(f"""
            SELECT COUNT(*)
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE 1=1 {lf}
        """, params).fetchone()[0]

        completed = conn.execute(f"""
            SELECT COUNT(*)
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE t.status='completed'
              {lf}
        """, params).fetchone()[0]

        missed = conn.execute(f"""
            SELECT COUNT(*)
            FROM tasks t
            LEFT JOIN zones z
                ON t.zone_id=z.id
            WHERE t.status='missed'
              {lf}
        """, params).fetchone()[0]

    conn.close()

    compliance = round(
        (completed / total) * 100,
        1
    ) if total else 0

    # ---------------------------------------------------------
    # Generate PDF
    # ---------------------------------------------------------

    buffer = BytesIO()

    doc = SimpleDocTemplate(buffer)
    styles = getSampleStyleSheet()

    content = [
        Paragraph(
            "CleanTrack Report",
            styles["Title"]
        ),
        Paragraph(
            f"Total Tasks: {total}",
            styles["Normal"]
        ),
        Paragraph(
            f"Completed Tasks: {completed}",
            styles["Normal"]
        ),
        Paragraph(
            f"Missed Tasks: {missed}",
            styles["Normal"]
        ),
        Paragraph(
            f"Compliance: {compliance}%",
            styles["Normal"]
        ),
    ]

    doc.build(content)

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="cleantrack_report.pdf",
        mimetype="application/pdf"
    )

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
    from seed import seed
    seed()
    scheduler.start()
    print("\n🧹 CleanTrack API  →  http://127.0.0.1:5000")
    app.run(debug=True, port=5000, use_reloader=False)