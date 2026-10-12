import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "cleantrack.db")

print("Database:", os.path.abspath(DB_PATH))
print()

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

tables = conn.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type='table'
    ORDER BY name
""").fetchall()

print("TABLES:")
for table in tables:
    name = table["name"]
    print(f"\n--- {name} ---")

    columns = conn.execute(f"PRAGMA table_info({name})").fetchall()

    for col in columns:
        print(
            f"{col['cid']}: "
            f"{col['name']} | "
            f"{col['type']} | "
            f"NOT NULL={col['notnull']} | "
            f"PK={col['pk']} | "
            f"DEFAULT={col['dflt_value']}"
        )

print("\nROW COUNTS:")
for table in tables:
    name = table["name"]
    try:
        count = conn.execute(
            f"SELECT COUNT(*) AS count FROM {name}"
        ).fetchone()["count"]
        print(f"{name}: {count}")
    except Exception as e:
        print(f"{name}: ERROR - {e}")

conn.close()