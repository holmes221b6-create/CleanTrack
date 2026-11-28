import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# Render uses its configured PostgreSQL database. Local development deliberately
# uses the known populated SQLite file so the app can remain usable while Neon
# is unavailable. Never silently create a new empty local database.
_is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"))
load_dotenv("back.env", override=not _is_render)

if not _is_render:
    _project_dir = Path(__file__).resolve().parent
    _local_db = _project_dir / "instance" / "cleantrack_dev.db"
    if not _local_db.is_file():
        raise FileNotFoundError(
            "The existing local MECHNAR database was not found at "
            f"{_local_db}. No new database was created. Restore that file "
            "to this location before starting the local backend."
        )

    # Preserve the original database before the first compatibility migration.
    _backup_db = _local_db.with_name(_local_db.name + ".pre_mechnar_compat.bak")
    if not _backup_db.exists():
        shutil.copy2(_local_db, _backup_db)

    # Use an absolute SQLite URI locally; keep back.env and Render's Neon URL
    # untouched so reconnecting to Neon later requires no configuration repair.
    _sqlite_path = str(_local_db.resolve()).replace("\\", "/")
    os.environ["DATABASE_URL"] = f"sqlite:///{_sqlite_path}"

from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )
