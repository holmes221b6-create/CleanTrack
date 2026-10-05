import sqlite3
import os

_turso_url = os.getenv("TURSO_DATABASE_URL")
_turso_token = os.getenv("TURSO_AUTH_TOKEN")

TURSO_DATABASE_URL = _turso_url.strip() if _turso_url else None
TURSO_AUTH_TOKEN = _turso_token.strip() if _turso_token else None
DB_PATH = os.getenv("DB_PATH", "cleantrack.db")


class CleanTrackRow(dict):
    """
    SQLite-compatible row object for Turso.

    Supports both:
        row["email"]
    and:
        row[0]

    This lets the existing CleanTrack backend continue using
    its current row-access patterns.
    """

    def __init__(self, cursor, values):
        self._keys = [
            column[0]
            for column in (cursor.description or [])
        ]
        self._values = tuple(values)

        super().__init__(
            zip(self._keys, self._values)
        )

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]

        return super().__getitem__(key)


def turso_row_factory(cursor, row):
    return CleanTrackRow(cursor, row)


def using_turso():
    return bool(
        TURSO_DATABASE_URL
        and TURSO_AUTH_TOKEN
    )


def get_db():

    if using_turso():

        import turso_serverless

        conn = turso_serverless.connect(
            TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN
        )

        conn.row_factory = turso_row_factory

        return conn


    # Local development continues using normal SQLite.

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA foreign_keys=ON"
    )

    return conn


def column_exists(conn, table, column):
    columns = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(row["name"] == column for row in columns)


def add_column_if_missing(conn, table, column, definition):
    if not column_exists(conn, table, column):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
def init_db():

    if using_turso():
        print(
            "Using Turso database; "
            "skipping local SQLite initialization."
        )
        return

    conn = get_db()
    try:
        c = conn.cursor()

        # =========================================================
        # CORE TABLES
        # =========================================================

        c.executescript("""
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                city TEXT,
                country TEXT,
                organization_code TEXT UNIQUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
                id TEXT PRIMARY KEY,
                location_id TEXT NOT NULL,
                name TEXT NOT NULL,
                floor TEXT,
                type TEXT DEFAULT 'bathroom',
                qr_code TEXT,
                cleaning_interval_minutes INTEGER DEFAULT 60,
                status TEXT DEFAULT 'pending',
                last_cleaned_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                notification_email TEXT,
                phone TEXT,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'employee',
                location_id TEXT,
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

            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS team_supervisors (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS team_members (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS team_zones (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, zone_id)
            );

            CREATE TABLE IF NOT EXISTS team_locations (
                team_id TEXT NOT NULL,
                location_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (team_id, location_id),
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
                FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS account_requests (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                notification_email TEXT,
                password_hash TEXT NOT NULL,
                requested_role TEXT NOT NULL,
                employee_id TEXT,
                requested_team_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by TEXT,
                reviewed_at DATETIME,
                rejection_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # =========================================================
        # MIGRATION-SAFE COLUMNS
        # =========================================================

        # USERS
        add_column_if_missing(
            conn,
            "users",
            "organization_id",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "users",
            "account_status",
            "TEXT DEFAULT 'active'"
        )

        add_column_if_missing(
            conn,
            "users",
            "approved_by",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "users",
            "approved_at",
            "DATETIME"
        )

        add_column_if_missing(
            conn,
            "users",
            "employee_id",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "users",
            "notification_email",
            "TEXT"
        )

        # LOCATIONS
        add_column_if_missing(
            conn,
            "locations",
            "organization_id",
            "TEXT"
        )

        # =========================================================
        # NORMALIZE LEGACY ROLE
        # =========================================================

        c.execute("""
            UPDATE users
            SET role = 'employee'
            WHERE role = 'staff'
        """)

        c.execute("""
            UPDATE users
            SET account_status = 'active'
            WHERE account_status IS NULL
               OR account_status = ''
        """)

        # =========================================================
        # INDEXES
        # =========================================================

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_organization
            ON users(organization_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_role
            ON users(role)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_account_status
            ON users(account_status)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_location
            ON users(location_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_locations_organization
            ON locations(organization_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_zones_location
            ON zones(location_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_staff_zones_user
            ON staff_zones(user_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_staff_zones_zone
            ON staff_zones(zone_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_zone
            ON tasks(zone_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to
            ON tasks(assigned_to)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_cleaning_logs_user
            ON cleaning_logs(user_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_cleaning_logs_zone
            ON cleaning_logs(zone_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_user
            ON alerts(user_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_zone
            ON alerts(zone_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_teams_organization
            ON teams(organization_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_supervisors_team
            ON team_supervisors(team_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_supervisors_user
            ON team_supervisors(user_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_members_team
            ON team_members(team_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_members_user
            ON team_members(user_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_zones_team
            ON team_zones(team_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_zones_zone
            ON team_zones(zone_id)
        """)
        
        conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_team_locations_location
    ON team_locations(location_id)
""")

        # =========================================================
        # ACCOUNT REQUEST INDEXES
        # =========================================================

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_account_requests_org
            ON account_requests(organization_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_account_requests_status
            ON account_requests(status)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_account_requests_email
            ON account_requests(email)
        """)
        
        # =========================================================
# MIGRATE EXISTING LOCATIONS INTO TEAM LOCATIONS
# =========================================================
 
        # =========================================================
        # COMMIT
        # =========================================================

        conn.commit()

        print("Database initialization completed successfully.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

       