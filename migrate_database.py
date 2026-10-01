import sqlite3
import shutil
import os
import uuid
from datetime import datetime

DB_PATH = "cleantrack.db"
BACKUP_PATH = "cleantrack_before_migration.db"


def backup_database():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"{DB_PATH} was not found.")

    shutil.copy2(DB_PATH, BACKUP_PATH)
    print(f"Backup created: {BACKUP_PATH}")


def migrate():
    backup_database()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        print("\nCreating organization structure...")

        # ---------------------------------------------------------
        # 1. ORGANIZATIONS
        # ---------------------------------------------------------
        conn.execute("""
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
            )
        """)

        # ---------------------------------------------------------
        # 2. TEAMS
        # ---------------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------------------------------------------------------
        # 3. TEAM SUPERVISORS
        # ---------------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_supervisors (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, user_id)
            )
        """)

        # ---------------------------------------------------------
        # 4. TEAM MEMBERS
        # ---------------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, user_id)
            )
        """)

        # ---------------------------------------------------------
        # 5. TEAM ZONES
        # ---------------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_zones (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, zone_id)
            )
        """)

        # ---------------------------------------------------------
        # 6. ADD ORGANIZATION_ID TO USERS
        # ---------------------------------------------------------
        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        if "organization_id" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN organization_id TEXT"
            )

        # ---------------------------------------------------------
        # 7. ADD ACCOUNT STATUS TO USERS
        # ---------------------------------------------------------
        if "account_status" not in user_columns:
            conn.execute("""
                ALTER TABLE users
                ADD COLUMN account_status TEXT DEFAULT 'active'
            """)

        # ---------------------------------------------------------
        # 8. ADD APPROVAL FIELDS
        # ---------------------------------------------------------
        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        if "approved_by" not in user_columns:
            conn.execute("""
                ALTER TABLE users
                ADD COLUMN approved_by TEXT
            """)

        if "approved_at" not in user_columns:
            conn.execute("""
                ALTER TABLE users
                ADD COLUMN approved_at DATETIME
            """)

        if "employee_id" not in user_columns:
            conn.execute("""
                ALTER TABLE users
                ADD COLUMN employee_id TEXT
            """)

        # ---------------------------------------------------------
        # 9. ADD ORGANIZATION_ID TO LOCATIONS
        # ---------------------------------------------------------
        location_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(locations)").fetchall()
        }

        if "organization_id" not in location_columns:
            conn.execute("""
                ALTER TABLE locations
                ADD COLUMN organization_id TEXT
            """)

        # ---------------------------------------------------------
        # 10. FIND EXISTING DATA
        # ---------------------------------------------------------
        users = conn.execute("""
            SELECT *
            FROM users
            ORDER BY created_at
        """).fetchall()

        locations = conn.execute("""
            SELECT *
            FROM locations
            ORDER BY created_at
        """).fetchall()

        print(f"Existing users: {len(users)}")
        print(f"Existing locations: {len(locations)}")

        # ---------------------------------------------------------
        # 11. CREATE ONE LEGACY ORGANIZATION
        # ---------------------------------------------------------
        organization = conn.execute("""
            SELECT *
            FROM organizations
            LIMIT 1
        """).fetchone()

        if organization:
            organization_id = organization["id"]
            print(f"Using existing organization: {organization_id}")
        else:
            organization_id = str(uuid.uuid4())

            conn.execute("""
                INSERT INTO organizations (
                    id,
                    name,
                    organization_code
                )
                VALUES (?, ?, ?)
            """, (
                organization_id,
                "CleanTrack Facility",
                "CLEANTRACK"
            ))

            print(f"Created organization: {organization_id}")

        # ---------------------------------------------------------
        # 12. LINK EXISTING USERS TO ORGANIZATION
        # ---------------------------------------------------------
        conn.execute("""
            UPDATE users
            SET organization_id = ?
            WHERE organization_id IS NULL
        """, (organization_id,))

        # ---------------------------------------------------------
        # 13. CONVERT OLD STAFF ROLE TO EMPLOYEE
        # ---------------------------------------------------------
        conn.execute("""
            UPDATE users
            SET role = 'employee'
            WHERE role = 'staff'
        """)

        # ---------------------------------------------------------
        # 14. MAKE EXISTING USERS ACTIVE
        # ---------------------------------------------------------
        conn.execute("""
            UPDATE users
            SET account_status = 'active'
            WHERE account_status IS NULL
               OR account_status = ''
        """)

        # ---------------------------------------------------------
        # 15. LINK EXISTING LOCATIONS
        # ---------------------------------------------------------
        conn.execute("""
            UPDATE locations
            SET organization_id = ?
            WHERE organization_id IS NULL
        """, (organization_id,))

        # ---------------------------------------------------------
        # 16. CREATE DEFAULT TEAM
        # ---------------------------------------------------------
        existing_team = conn.execute("""
            SELECT *
            FROM teams
            WHERE organization_id = ?
            LIMIT 1
        """, (organization_id,)).fetchone()

        if existing_team:
            team_id = existing_team["id"]
            print(f"Using existing team: {team_id}")
        else:
            team_id = str(uuid.uuid4())

            conn.execute("""
                INSERT INTO teams (
                    id,
                    organization_id,
                    name,
                    description
                )
                VALUES (?, ?, ?, ?)
            """, (
                team_id,
                organization_id,
                "General Team",
                "Existing CleanTrack team migrated from the legacy system"
            ))

            print(f"Created default team: {team_id}")

        # ---------------------------------------------------------
        # 17. FIND SUPERVISORS
        # ---------------------------------------------------------
        supervisors = conn.execute("""
            SELECT id
            FROM users
            WHERE organization_id = ?
              AND role = 'supervisor'
              AND account_status = 'active'
        """, (organization_id,)).fetchall()

        for supervisor in supervisors:
            exists = conn.execute("""
                SELECT 1
                FROM team_supervisors
                WHERE team_id = ?
                  AND user_id = ?
            """, (team_id, supervisor["id"])).fetchone()

            if not exists:
                conn.execute("""
                    INSERT INTO team_supervisors (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                """, (
                    str(uuid.uuid4()),
                    team_id,
                    supervisor["id"]
                ))

        # ---------------------------------------------------------
        # 18. ADD EXISTING EMPLOYEES TO DEFAULT TEAM
        # ---------------------------------------------------------
        employees = conn.execute("""
            SELECT id
            FROM users
            WHERE organization_id = ?
              AND role = 'employee'
              AND account_status = 'active'
        """, (organization_id,)).fetchall()

        for employee in employees:
            exists = conn.execute("""
                SELECT 1
                FROM team_members
                WHERE team_id = ?
                  AND user_id = ?
            """, (team_id, employee["id"])).fetchone()

            if not exists:
                conn.execute("""
                    INSERT INTO team_members (
                        id,
                        team_id,
                        user_id
                    )
                    VALUES (?, ?, ?)
                """, (
                    str(uuid.uuid4()),
                    team_id,
                    employee["id"]
                ))

        # ---------------------------------------------------------
        # 19. PRESERVE EXISTING STAFF-ZONE ASSIGNMENTS
        #     AND ALSO LINK THEIR ZONES TO THE DEFAULT TEAM
        # ---------------------------------------------------------
        assignments = conn.execute("""
            SELECT DISTINCT zone_id
            FROM staff_zones
        """).fetchall()

        for assignment in assignments:
            zone_id = assignment["zone_id"]

            exists = conn.execute("""
                SELECT 1
                FROM team_zones
                WHERE team_id = ?
                  AND zone_id = ?
            """, (team_id, zone_id)).fetchone()

            if not exists:
                conn.execute("""
                    INSERT INTO team_zones (
                        id,
                        team_id,
                        zone_id
                    )
                    VALUES (?, ?, ?)
                """, (
                    str(uuid.uuid4()),
                    team_id,
                    zone_id
                ))

        # ---------------------------------------------------------
        # 20. INDEXES
        # ---------------------------------------------------------
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_organization
            ON users(organization_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_locations_organization
            ON locations(organization_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_teams_organization
            ON teams(organization_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_members_team
            ON team_members(team_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_members_user
            ON team_members(user_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_supervisors_team
            ON team_supervisors(team_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_supervisors_user
            ON team_supervisors(user_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_zones_team
            ON team_zones(team_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_team_zones_zone
            ON team_zones(zone_id)
        """)

        conn.commit()

        print("\n========================================")
        print("MIGRATION COMPLETED SUCCESSFULLY")
        print("========================================")

        print("\nVerification:")

        tables = [
            "organizations",
            "teams",
            "team_supervisors",
            "team_members",
            "team_zones"
        ]

        for table in tables:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

            print(f"{table}: {count}")

        print("\nExisting data:")
        print("users:", conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        print("locations:", conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0])
        print("zones:", conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0])
        print("tasks:", conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        print("alerts:", conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
        print("cleaning_logs:", conn.execute("SELECT COUNT(*) FROM cleaning_logs").fetchone()[0])

        print(f"\nBackup: {os.path.abspath(BACKUP_PATH)}")

    except Exception:
        conn.rollback()
        print("\nMIGRATION FAILED.")
        print("Your original database was NOT committed.")
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    migrate()