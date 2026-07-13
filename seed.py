import uuid, bcrypt, random, io, base64
from datetime import datetime, timedelta
from database import get_db, init_db

try:
    import qrcode
    def gen_qr(data):
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
except:
    def gen_qr(data):
        return ""

def seed():
    init_db()
    conn = get_db()
    c = conn.cursor()

    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        print("Already seeded.")
        conn.close()
        return

    loc1 = str(uuid.uuid4())
    loc2 = str(uuid.uuid4())
    c.execute("INSERT INTO locations VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
              (loc1, "Tower A HQ", "123 Main St", "Mumbai", "India"))
    c.execute("INSERT INTO locations VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
              (loc2, "Branch Office B", "456 Park Ave", "Bengaluru", "India"))

    zone_data = [
        (loc1, "Floor 1 - Male", "1"),
        (loc1, "Floor 1 - Female", "1"),
        (loc1, "Floor 5 - Male", "5"),
        (loc1, "Floor 5 - Female", "5"),
        (loc1, "Floor 12 - Bathroom C", "12"),
        (loc2, "Ground - Lobby WC", "G"),
        (loc2, "Level 2 - Male", "2"),
    ]
    statuses = ["cleaned", "pending", "overdue", "cleaned", "cleaned", "pending", "in-progress"]
    zone_ids = []

    for i, (loc, name, floor) in enumerate(zone_data):
        zid = str(uuid.uuid4())
        zone_ids.append(zid)
        qr = gen_qr(str({"zoneId": zid, "name": name}))
        c.execute("INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                  (zid, loc, name, floor, "bathroom", qr, 60, statuses[i], None))

    pw = bcrypt.hashpw(b"Password123!", bcrypt.gensalt()).decode()

    admin_id = str(uuid.uuid4())
    sup_id   = str(uuid.uuid4())
    s1_id    = str(uuid.uuid4())
    s2_id    = str(uuid.uuid4())
    s3_id    = str(uuid.uuid4())

    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (admin_id, "Admin User", "admin@cleantrack.demo", None, pw, "admin", loc1, 1))
    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (sup_id, "Sarah Supervisor", "supervisor@cleantrack.demo", None, pw, "supervisor", loc1, 1))
    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (s1_id, "Raj Kumar", "raj@cleantrack.demo", "+91-98765-43210", pw, "staff", loc1, 1))
    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (s2_id, "Priya Sharma", "priya@cleantrack.demo", "+91-91234-56789", pw, "staff", loc1, 1))
    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (s3_id, "Amit Singh", "amit@cleantrack.demo", "+91-87654-32109", pw, "staff", loc2, 1))

    assignments = [
        (s1_id, zone_ids[0]), (s1_id, zone_ids[1]),
        (s2_id, zone_ids[2]), (s2_id, zone_ids[3]),
        (s3_id, zone_ids[5]), (s3_id, zone_ids[6]),
    ]
    for uid, zid in assignments:
        c.execute("INSERT INTO staff_zones VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                  (str(uuid.uuid4()), uid, zid, "morning"))

    task_statuses = ["completed", "completed", "completed", "missed",
                     "completed", "completed", "missed", "completed"]

    for day in range(7, 0, -1):
        for zid in zone_ids:
            for hour in range(8, 19):
                tid = str(uuid.uuid4())
                sched = datetime.now() - timedelta(days=day)
                sched = sched.replace(hour=hour, minute=0, second=0, microsecond=0)
                st = random.choice(task_statuses)
                assigned = s1_id if zid in zone_ids[:4] else s3_id
                dur = round(8 + random.random() * 15, 1) if st == "completed" else None
                completed_at = (sched + timedelta(minutes=dur)).isoformat() if dur else None
                c.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (tid, zid, assigned, st, sched.isoformat(),
                     sched.isoformat() if st == "completed" else None,
                     completed_at, dur, None,
                     1 if st == "missed" else 0, 0)
                )

    alert_data = [
        ("repeat_miss",       "high",     "Floor 12 - Bathroom C missed 2x this week"),
        ("overdue_cleaning",  "warning",  "Floor 1 - Male overdue by 90 minutes"),
        ("repeat_miss",       "critical", "Predictive: Ground - Lobby WC missed 3x this week"),
    ]
    for typ, sev, msg in alert_data:
        c.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                  (str(uuid.uuid4()), typ, sev, zone_ids[0], None, None, msg, 0))

    conn.commit()
    conn.close()

    print("\nSeed complete!")
    print("   admin@cleantrack.demo / Password123!")
    print("   supervisor@cleantrack.demo / Password123!")
    print("   raj@cleantrack.demo / Password123!")

if __name__ == "__main__":
    seed()