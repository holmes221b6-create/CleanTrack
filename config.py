import os
from datetime import timedelta


def _database_url():
    url = os.getenv("DATABASE_URL", "sqlite:///cleantrack_dev.db").strip()

    # Render supplies PostgreSQL URLs as postgresql://, while this project
    # installs Psycopg 3 (psycopg[binary]), whose SQLAlchemy dialect is
    # postgresql+psycopg://.
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-before-deployment")

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {}

    JWT_SECRET_KEY = os.getenv(
        "JWT_SECRET_KEY",
        "dev-only-change-this-before-deployment",
    )
    # Prefer explicit Bearer tokens for the SPA; cookies remain a fallback.
    JWT_TOKEN_LOCATION = ["headers", "cookies"]
    JWT_ACCESS_COOKIE_NAME = "cleantrack_access"
    JWT_REFRESH_COOKIE_NAME = "cleantrack_refresh"
    # The SPA and API run on separate Render hostnames. Force Secure
    # cross-origin cookies in Render so browsers send the session to the API.
    _is_render = (
        os.getenv("RENDER", "").lower() == "true"
        or bool(os.getenv("RENDER_EXTERNAL_URL"))
    )
    JWT_COOKIE_SECURE = (
        True if _is_render
        else os.getenv("COOKIE_SECURE", "false").lower() == "true"
    )
    JWT_COOKIE_HTTPONLY = True
    JWT_COOKIE_SAMESITE = (
        "None" if _is_render
        else os.getenv("COOKIE_SAMESITE", "Lax")
    )
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=14)
    JWT_COOKIE_CSRF_PROTECT = True

    # Allow the existing 20 MB announcement-attachment batch limit plus multipart overhead.
    # Individual zone floor-plan uploads are independently capped at 10 MB.
    MAX_CONTENT_LENGTH = 22 * 1024 * 1024

    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True

    CORS_ORIGINS = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5500,http://localhost:5500",
        ).split(",")
        if origin.strip()
    ]
