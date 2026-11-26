import os

from dotenv import load_dotenv

# Local development keeps secrets in back.env. Load them BEFORE importing
# the application package, because config.py reads DATABASE_URL at import time.
# Render continues to use its configured environment variables.
load_dotenv("back.env", override=False)

from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )
