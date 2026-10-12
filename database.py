import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "cleantrack.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            description TEXT,
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (organization_id) REFERENCES organizations(id)
        );
        CREATE TABLE IF NOT EXISTS locations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT,
            city TEXT,
            country TEXT,
            organization_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS zones (

            location_id TEXT NOT NULL,
            name TEXT NOT NULL,
            floor TEXT,
            type TEXT DEFAULT 'bathroom',
            qr_code TEXT,
            cleaning_interval_minutes INTEGER DEFAULT 60,
            status TEXT DEFAULT 'pending',
            last_cleaned_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (location_id) REFERENCES locations(id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            notification_email TEXT,
            phone TEXT,           
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'staff',
            location_id TEXT,
            organization_id TEXT,
            membership_status TEXT NOT NULL DEFAULT 'approved',
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS staff_zones (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            zone_id TEXT NOT NULL,
            shift TEXT DEFAULT 'morning',
            assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            zone_id TEXT NOT NULL,
            assigned_to TEXT,
            status TEXT DEFAULT 'pending',
            scheduled_at DATETIME NOT NULL,
            started_at DATETIME,
            completed_at DATETIME,
            duration_minutes REAL,
            notes TEXT,
            is_overdue INTEGER DEFAULT 0,
            overdue_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cleaning_logs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            zone_id TEXT NOT NULL,
            before_photo TEXT,
            after_photo TEXT,
            ai_cleanliness_score REAL,
            ai_feedback TEXT,
            notes TEXT,
            logged_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            severity TEXT DEFAULT 'warning',
            zone_id TEXT,
            user_id TEXT,
            task_id TEXT,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
       """)

    # Safe additive migrations for databases created before organizations existed.
    migrations = {
        "users": {
            "notification_email": "TEXT",
            "organization_id": "TEXT",
            "membership_status": "TEXT NOT NULL DEFAULT 'approved'"
        },
        "locations": {
            "organization_id": "TEXT"
        }
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in columns.items():
            if column not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    c.execute("CREATE INDEX IF NOT EXISTS idx_users_organization ON users(organization_id, membership_status, role)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_locations_organization ON locations(organization_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_teams_organization ON teams(organization_id)")

    conn.commit()
    conn.close()