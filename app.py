import os, uuid, random, io, base64
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import qrcode
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity, get_jwt
)
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from database import get_db, init_db

load_dotenv()

app = Flask(__name__, static_folder="../frontend", static_url_path="")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET", "dev_secret_change_me")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=12)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# Explicitly trust both localhost and 127.0.0.1 variants of your dashboard
CORS(app)
jwt = JWTManager(app)

@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        res = jsonify({})
        res.headers["Access-Control-Allow-Origin"] = "*"
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
    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (uid, name, email, data.get("phone"), hashed, data.get("role","staff"), data.get("location_id"), 1))
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
    conn = get_db()
    rows = rows_to_list(conn.execute("""
        SELECT l.*, COUNT(DISTINCT z.id) zone_count, COUNT(DISTINCT u.id) staff_count
        FROM locations l
        LEFT JOIN zones z ON z.location_id=l.id
        LEFT JOIN users u ON u.location_id=l.id AND u.role='staff'
        GROUP BY l.id ORDER BY l.name
    """).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/locations", methods=["POST"])
@jwt_required()
def create_location():
    d = request.get_json() or {}
    if not d.get("name"):
        return jsonify(error="name required"), 400
    lid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO locations VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (lid, d["name"], d.get("address"), d.get("city"), d.get("country")))
    conn.commit(); conn.close()
    return jsonify(id=lid), 201

@app.route("/api/locations/<lid>", methods=["PUT"])
@jwt_required()
def update_location(lid):
    d = request.get_json() or {}
    conn = get_db()
    conn.execute("UPDATE locations SET name=COALESCE(?,name), address=COALESCE(?,address), city=COALESCE(?,city), country=COALESCE(?,country) WHERE id=?",
                 (d.get("name"),d.get("address"),d.get("city"),d.get("country"),lid))
    conn.commit(); conn.close()
    return jsonify(message="Updated")

@app.route("/api/locations/<lid>", methods=["DELETE"])
@jwt_required()
def delete_location(lid):
    conn = get_db()
    conn.execute("DELETE FROM locations WHERE id=?", (lid,))
    conn.commit(); conn.close()
    return jsonify(message="Deleted")

# ─── zones ────────────────────────────────────────────────────────────────────

@app.route("/api/zones", methods=["GET"])
@jwt_required()
def get_zones():
    loc = request.args.get("location_id")
    conn = get_db()
    q = """
        SELECT z.*, l.name location_name,
          (SELECT COUNT(*) FROM tasks t WHERE t.zone_id=z.id AND t.status='pending') pending_tasks,
          (SELECT COUNT(*) FROM tasks t WHERE t.zone_id=z.id AND t.is_overdue=1) overdue_tasks
        FROM zones z LEFT JOIN locations l ON z.location_id=l.id
    """
    rows = rows_to_list(conn.execute(q + (" WHERE z.location_id=? ORDER BY z.name" if loc else " ORDER BY z.name"),
                                     (loc,) if loc else ()).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/zones/<zid>", methods=["GET"])
@jwt_required()
def get_zone(zid):
    conn = get_db()
    row = row_to_dict(conn.execute(
        "SELECT z.*, l.name location_name FROM zones z LEFT JOIN locations l ON z.location_id=l.id WHERE z.id=?", (zid,)
    ).fetchone())
    conn.close()
    return jsonify(row) if row else (jsonify(error="Not found"), 404)

@app.route("/api/zones", methods=["POST"])
@jwt_required()
def create_zone():
    d = request.get_json() or {}
    if not d.get("location_id") or not d.get("name"):
        return jsonify(error="location_id and name required"), 400
    zid = str(uuid.uuid4())
    qr = gen_qr_b64(f'{{"zoneId":"{zid}","name":"{d["name"]}"}}')
    conn = get_db()
    conn.execute("INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (zid, d["location_id"], d["name"], d.get("floor"), d.get("type","bathroom"), qr,
                  d.get("cleaning_interval_minutes", 60), "pending", None))
    conn.commit(); conn.close()
    return jsonify(id=zid, qr_code=qr), 201

@app.route("/api/zones/<zid>", methods=["PUT"])
@jwt_required()
def update_zone(zid):
    d = request.get_json() or {}
    conn = get_db()
    conn.execute("UPDATE zones SET name=COALESCE(?,name), floor=COALESCE(?,floor), type=COALESCE(?,type), cleaning_interval_minutes=COALESCE(?,cleaning_interval_minutes), status=COALESCE(?,status) WHERE id=?",
                 (d.get("name"),d.get("floor"),d.get("type"),d.get("cleaning_interval_minutes"),d.get("status"),zid))
    conn.commit(); conn.close()
    return jsonify(message="Updated")

@app.route("/api/zones/<zid>", methods=["DELETE"])
@jwt_required()
def delete_zone(zid):
    conn = get_db()
    conn.execute("DELETE FROM zones WHERE id=?", (zid,))
    conn.commit(); conn.close()
    return jsonify(message="Deleted")

@app.route("/api/zones/<zid>/qr")
@jwt_required()
def zone_qr(zid):
    conn = get_db()
    row = conn.execute("SELECT qr_code FROM zones WHERE id=?", (zid,)).fetchone()
    conn.close()
    return jsonify(qr_code=row["qr_code"]) if row else (jsonify(error="Not found"), 404)

@app.route("/api/zones/<zid>/staff")
@jwt_required()
def zone_staff(zid):
    conn = get_db()
    rows = rows_to_list(conn.execute("""
        SELECT u.id, u.name, u.email, u.phone, sz.shift, sz.assigned_at
        FROM staff_zones sz JOIN users u ON sz.user_id=u.id WHERE sz.zone_id=?
    """, (zid,)).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/zones/<zid>/assign", methods=["POST"])
@jwt_required()
def assign_zone(zid):
    d = request.get_json() or {}
    if not d.get("user_id"):
        return jsonify(error="user_id required"), 400
    conn = get_db()
    conn.execute("DELETE FROM staff_zones WHERE user_id=? AND zone_id=?", (d["user_id"], zid))
    conn.execute("INSERT INTO staff_zones VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                 (str(uuid.uuid4()), d["user_id"], zid, d.get("shift","morning")))
    conn.commit(); conn.close()
    return jsonify(message="Assigned"), 201

# ─── users ────────────────────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@jwt_required()
def get_users():
    role = request.args.get("role")
    loc  = request.args.get("location_id")
    q = "SELECT id,name,email,phone,role,location_id,is_active,created_at FROM users WHERE 1=1"
    params = []
    if role: q += " AND role=?"; params.append(role)
    if loc:  q += " AND location_id=?"; params.append(loc)
    q += " ORDER BY name"
    conn = get_db()
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/users", methods=["POST"])
@jwt_required()
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
    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (uid, d["name"], d["email"], d.get("phone"), hashed, d.get("role","staff"), d.get("location_id"), 1))
    conn.commit(); conn.close()
    return jsonify(id=uid), 201

@app.route("/api/users/<uid>", methods=["PUT"])
@jwt_required()
def update_user(uid):
    d = request.get_json() or {}
    conn = get_db()
    conn.execute("UPDATE users SET name=COALESCE(?,name), phone=COALESCE(?,phone), role=COALESCE(?,role), location_id=COALESCE(?,location_id), is_active=COALESCE(?,is_active) WHERE id=?",
                 (d.get("name"),d.get("phone"),d.get("role"),d.get("location_id"),d.get("is_active"),uid))
    conn.commit(); conn.close()
    return jsonify(message="Updated")

@app.route("/api/users/<uid>", methods=["DELETE"])
@jwt_required()
def deactivate_user(uid):
    conn = get_db()
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (uid,))
    conn.commit(); conn.close()
    return jsonify(message="Deactivated")

# ─── tasks ────────────────────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
@jwt_required()
def get_tasks():
    args = request.args
    q = """
        SELECT t.*, z.name zone_name, z.floor, l.name location_name, u.name staff_name
        FROM tasks t
        LEFT JOIN zones z ON t.zone_id=z.id
        LEFT JOIN locations l ON z.location_id=l.id
        LEFT JOIN users u ON t.assigned_to=u.id
        WHERE 1=1
    """
    params = []
    if args.get("zone_id"):       q += " AND t.zone_id=?";       params.append(args["zone_id"])
    if args.get("assigned_to"):   q += " AND t.assigned_to=?";   params.append(args["assigned_to"])
    if args.get("status"):        q += " AND t.status=?";         params.append(args["status"])
    if args.get("location_id"):   q += " AND z.location_id=?";   params.append(args["location_id"])
    if args.get("date"):          q += " AND DATE(t.scheduled_at)=DATE(?)"; params.append(args["date"])
    q += " ORDER BY t.scheduled_at DESC LIMIT 200"
    conn = get_db()
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/tasks/my")
@jwt_required()
def my_tasks():
    uid = get_jwt_identity()
    conn = get_db()
    rows = rows_to_list(conn.execute("""
        SELECT t.*, z.name zone_name, z.floor, l.name location_name
        FROM tasks t LEFT JOIN zones z ON t.zone_id=z.id LEFT JOIN locations l ON z.location_id=l.id
        WHERE t.assigned_to=? ORDER BY t.scheduled_at DESC LIMIT 50
    """, (uid,)).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/tasks", methods=["POST"])
@jwt_required()
def create_task():
    d = request.get_json() or {}
    if not d.get("zone_id") or not d.get("scheduled_at"):
        return jsonify(error="zone_id and scheduled_at required"), 400
    tid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (tid, d["zone_id"], d.get("assigned_to"), "pending", d["scheduled_at"],
                  None, None, None, None, 0, 0))
    conn.commit(); conn.close()
    return jsonify(id=tid), 201

@app.route("/api/tasks/<tid>/start", methods=["PUT"])
@jwt_required()
def start_task(tid):
    uid = get_jwt_identity()
    conn = get_db()
    conn.execute("UPDATE tasks SET status='in-progress', started_at=CURRENT_TIMESTAMP, assigned_to=? WHERE id=?", (uid, tid))
    task = row_to_dict(conn.execute("SELECT zone_id FROM tasks WHERE id=?", (tid,)).fetchone())
    if task:
        conn.execute("UPDATE zones SET status='in-progress' WHERE id=?", (task["zone_id"],))
    conn.commit(); conn.close()
    return jsonify(message="Started")

@app.route("/api/tasks/<tid>/complete", methods=["PUT"])
@jwt_required()
def complete_task(tid):
    uid = get_jwt_identity()
    d = request.get_json() or {}
    conn = get_db()
    task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    if not task:
        conn.close(); return jsonify(error="Not found"), 404
    duration = None
    if task["started_at"]:
        try:
            start = datetime.fromisoformat(task["started_at"])
            duration = round((datetime.now() - start).total_seconds() / 60, 1)
        except: pass
    conn.execute("UPDATE tasks SET status='completed', completed_at=CURRENT_TIMESTAMP, duration_minutes=?, notes=?, assigned_to=? WHERE id=?",
                 (duration, d.get("notes"), uid, tid))
    conn.execute("UPDATE zones SET status='cleaned', last_cleaned_at=CURRENT_TIMESTAMP WHERE id=?", (task["zone_id"],))
    conn.commit(); conn.close()
    return jsonify(message="Completed", duration_minutes=duration)

@app.route("/api/tasks/<tid>/miss", methods=["PUT"])
@jwt_required()
def miss_task(tid):
    conn = get_db()
    task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    if not task:
        conn.close(); return jsonify(error="Not found"), 404
    new_count = (task["overdue_count"] or 0) + 1
    conn.execute("UPDATE tasks SET status='missed', is_overdue=1, overdue_count=? WHERE id=?", (new_count, tid))
    conn.execute("UPDATE zones SET status='overdue' WHERE id=?", (task["zone_id"],))
    sev = "critical" if new_count >= 3 else "high" if new_count >= 2 else "warning"
    conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (str(uuid.uuid4()), "missed_cleaning", sev, task["zone_id"], None, tid,
                  f"Cleaning missed {new_count}x for zone. Immediate attention required.", 0))
    conn.commit(); conn.close()
    return jsonify(message="Missed", overdue_count=new_count)

@app.route("/api/tasks/overdue")
@jwt_required()
def overdue_tasks():
    conn = get_db()
    rows = rows_to_list(conn.execute("""
        SELECT t.*, z.name zone_name, z.floor, l.name location_name, u.name staff_name
        FROM tasks t LEFT JOIN zones z ON t.zone_id=z.id
        LEFT JOIN locations l ON z.location_id=l.id LEFT JOIN users u ON t.assigned_to=u.id
        WHERE t.is_overdue=1 AND t.status!='completed'
        ORDER BY t.overdue_count DESC, t.scheduled_at ASC
    """).fetchall())
    conn.close()
    return jsonify(rows)

# ─── cleaning logs ────────────────────────────────────────────────────────────

@app.route("/api/logs", methods=["POST"])
@jwt_required()
def create_log():
    uid = get_jwt_identity()
    task_id  = request.form.get("task_id")
    zone_id  = request.form.get("zone_id")
    notes    = request.form.get("notes")
    if not task_id or not zone_id:
        return jsonify(error="task_id and zone_id required"), 400

    before_photo = after_photo = None
    for field in ("before_photo", "after_photo"):
        f = request.files.get(field)
        if f:
            fname = str(uuid.uuid4()) + os.path.splitext(f.filename)[1]
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            if field == "before_photo": before_photo = fname
            else: after_photo = fname

    ai_score, ai_feedback = (None, None)
    if after_photo:
        ai_score, ai_feedback = simulate_ai_score()

    lid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO cleaning_logs VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 (lid, task_id, uid, zone_id, before_photo, after_photo, ai_score, ai_feedback, notes))
    conn.execute("UPDATE tasks SET status='completed', completed_at=CURRENT_TIMESTAMP, assigned_to=? WHERE id=? AND status!='completed'", (uid, task_id))
    conn.execute("UPDATE zones SET status='cleaned', last_cleaned_at=CURRENT_TIMESTAMP WHERE id=?", (zone_id,))
    conn.commit(); conn.close()
    return jsonify(id=lid, ai_cleanliness_score=ai_score, ai_feedback=ai_feedback), 201

@app.route("/api/logs", methods=["GET"])
@jwt_required()
def get_logs():
    args = request.args
    q = """
        SELECT cl.*, u.name staff_name, z.name zone_name
        FROM cleaning_logs cl LEFT JOIN users u ON cl.user_id=u.id LEFT JOIN zones z ON cl.zone_id=z.id
        WHERE 1=1
    """
    params = []
    if args.get("zone_id"):  q += " AND cl.zone_id=?";  params.append(args["zone_id"])
    if args.get("user_id"):  q += " AND cl.user_id=?";  params.append(args["user_id"])
    if args.get("task_id"):  q += " AND cl.task_id=?";  params.append(args["task_id"])
    q += " ORDER BY cl.logged_at DESC LIMIT 100"
    conn = get_db()
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    return jsonify(rows)

# ─── alerts ────────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
@jwt_required()
def get_alerts():
    args = request.args
    q = "SELECT a.*, z.name zone_name FROM alerts a LEFT JOIN zones z ON a.zone_id=z.id WHERE 1=1"
    params = []
    if args.get("is_read") is not None:
        q += " AND a.is_read=?"; params.append(1 if args["is_read"]=="true" else 0)
    if args.get("severity"):
        q += " AND a.severity=?"; params.append(args["severity"])
    q += " ORDER BY a.created_at DESC LIMIT 100"
    conn = get_db()
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/alerts/<aid>/read", methods=["PUT"])
@jwt_required()
def mark_alert_read(aid):
    conn = get_db()
    conn.execute("UPDATE alerts SET is_read=1 WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return jsonify(message="Marked read")

@app.route("/api/alerts/read-all", methods=["PUT"])
@jwt_required()
def mark_all_read():
    conn = get_db()
    conn.execute("UPDATE alerts SET is_read=1")
    conn.commit(); conn.close()
    return jsonify(message="All read")

# ─── analytics ────────────────────────────────────────────────────────────────

@app.route("/api/analytics/dashboard")
@jwt_required()
def analytics_dashboard():
    loc = request.args.get("location_id")
    lf  = "AND z.location_id=?" if loc else ""
    p   = (loc,) if loc else ()
    conn = get_db()

    def count(where, params=()):
        return conn.execute(f"SELECT COUNT(*) FROM zones z WHERE 1=1 {where}", params).fetchone()[0]

    zones = {
        "total":       count(lf, p),
        "cleaned":     count(f"AND z.status='cleaned' {lf}", p),
        "overdue":     count(f"AND z.status='overdue' {lf}", p),
        "in_progress": count(f"AND z.status='in-progress' {lf}", p),
        "pending":     count(f"AND z.status='pending' {lf}", p),
    }

    today_q = f"""
        SELECT COUNT(*) total,
          SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) completed,
          SUM(CASE WHEN t.status='missed' THEN 1 ELSE 0 END) missed,
          SUM(CASE WHEN t.status IN ('pending','in-progress') THEN 1 ELSE 0 END) pending
        FROM tasks t LEFT JOIN zones z ON t.zone_id=z.id
        WHERE DATE(t.scheduled_at)=DATE('now') {lf}
    """
    today = row_to_dict(conn.execute(today_q, p).fetchone()) or {}
    total = today.get("total") or 1
    today["compliance_pct"] = round((today.get("completed") or 0) / total * 100)

    avg_dur = conn.execute(
        f"SELECT AVG(t.duration_minutes) FROM tasks t LEFT JOIN zones z ON t.zone_id=z.id WHERE t.status='completed' AND t.duration_minutes IS NOT NULL {lf}", p
    ).fetchone()[0]

    alerts = rows_to_list(conn.execute(
        "SELECT a.*, z.name zone_name FROM alerts a LEFT JOIN zones z ON a.zone_id=z.id WHERE a.is_read=0 ORDER BY a.created_at DESC LIMIT 10"
    ).fetchall())
    conn.close()
    return jsonify(zones=zones, today=today, avg_cleaning_duration=round(avg_dur) if avg_dur else None, recent_alerts=alerts)

@app.route("/api/analytics/staff")
@jwt_required()
def analytics_staff():
    from_d = request.args.get("from", (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d"))
    to_d   = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))
    conn = get_db()
    rows = rows_to_list(conn.execute("""
        SELECT u.id, u.name, u.email,
          COUNT(t.id) total_tasks,
          SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) completed,
          SUM(CASE WHEN t.status='missed' THEN 1 ELSE 0 END) missed,
          AVG(CASE WHEN t.duration_minutes IS NOT NULL THEN t.duration_minutes END) avg_duration,
          ROUND(100.0*SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END)/NULLIF(COUNT(t.id),0),1) compliance_pct
        FROM users u
        LEFT JOIN tasks t ON t.assigned_to=u.id AND DATE(t.scheduled_at) BETWEEN ? AND ?
        WHERE u.role='staff' AND u.is_active=1
        GROUP BY u.id ORDER BY compliance_pct DESC
    """, (from_d, to_d)).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/analytics/heatmap")
@jwt_required()
def analytics_heatmap():
    days = request.args.get("days", 7)
    loc  = request.args.get("location_id")
    lf   = "AND z.location_id=?" if loc else ""
    p    = [days, loc] if loc else [days]
    conn = get_db()
    rows = rows_to_list(conn.execute(f"""
        SELECT z.id, z.name, z.floor, l.name location_name,
          COUNT(t.id) total_tasks,
          SUM(CASE WHEN t.status='missed' THEN 1 ELSE 0 END) missed_count,
          SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) completed_count,
          ROUND(100.0*SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END)/NULLIF(COUNT(t.id),0),1) compliance_pct,
          AVG(CASE WHEN t.duration_minutes IS NOT NULL THEN t.duration_minutes END) avg_duration,
          z.status current_status
        FROM zones z LEFT JOIN locations l ON z.location_id=l.id
        LEFT JOIN tasks t ON t.zone_id=z.id AND t.scheduled_at >= datetime('now','-'||?||' days')
        WHERE 1=1 {lf}
        GROUP BY z.id ORDER BY missed_count DESC
    """, p).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/analytics/reports")
@jwt_required()
def analytics_reports():
    period = request.args.get("period", "daily")
    loc    = request.args.get("location_id")
    lf     = "AND z.location_id=?" if loc else ""
    p      = (loc,) if loc else ()
    grp = {"weekly":"strftime('%Y-W%W',t.scheduled_at)", "monthly":"strftime('%Y-%m',t.scheduled_at)"}.get(period, "DATE(t.scheduled_at)")
    conn = get_db()
    rows = rows_to_list(conn.execute(f"""
        SELECT {grp} period,
          COUNT(t.id) total,
          SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) completed,
          SUM(CASE WHEN t.status='missed' THEN 1 ELSE 0 END) missed,
          ROUND(100.0*SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END)/NULLIF(COUNT(t.id),0),1) compliance_pct,
          AVG(CASE WHEN t.duration_minutes IS NOT NULL THEN t.duration_minutes END) avg_duration
        FROM tasks t LEFT JOIN zones z ON t.zone_id=z.id
        WHERE 1=1 {lf}
        GROUP BY {grp} ORDER BY period DESC LIMIT 30
    """, p).fetchall())
    conn.close()
    return jsonify(rows)

@app.route("/api/analytics/kpis")
@jwt_required()
def analytics_kpis():
    conn = get_db()
    def compliance(days):
    # ADD THIS LINE RIGHT HERE:
      conn = get_db() 
    
    # This is your original line 631 (leave it exactly as it is):
      r = conn.execute(f"SELECT ROUND(100.0*SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) pct FROM tasks WHERE scheduled_at>=datetime('now','-{days} days')").fetchone()
      return r["pct"] or 0
    avg_dur = conn.execute("SELECT AVG(duration_minutes) FROM tasks WHERE status='completed' AND duration_minutes IS NOT NULL AND scheduled_at>=datetime('now','-30 days')").fetchone()[0]
    warnings = rows_to_list(conn.execute("""
        SELECT z.id, z.name, COUNT(t.id) missed_count FROM zones z
        JOIN tasks t ON t.zone_id=z.id
        WHERE t.status='missed' AND t.scheduled_at>=datetime('now','-7 days')
        GROUP BY z.id HAVING missed_count>=2 ORDER BY missed_count DESC
    """).fetchall())
    conn.close()
    return jsonify(compliance_7d=compliance(7), compliance_30d=compliance(30),
                   avg_duration_30d=round(avg_dur) if avg_dur else None, predictive_warnings=warnings)

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

if __name__ == "__main__":
    init_db()
    from seed import seed
    seed()
    scheduler.start()
    print("\n🧹 CleanTrack API  →  http://127.0.0.1:5000")
    app.run(debug=True, port=5000, use_reloader=False)