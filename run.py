import os

from dotenv import load_dotenv

# Local development keeps secrets in back.env. Load them BEFORE importing
# the application package, because config.py reads DATABASE_URL at import time.
# Render continues to use its configured environment variables.
# On a developer machine, back.env is the authoritative local configuration.
# On Render, platform environment variables remain authoritative.
_is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"))
load_dotenv("back.env", override=not _is_render)

from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )
