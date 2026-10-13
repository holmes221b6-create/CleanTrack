import os
from datetime import datetime, timezone


from flask import Flask, jsonify, request
from sqlalchemy import inspect, text

from config import Config
from app.extensions import cors, db, jwt, limiter, migrate

# Load models so SQLAlchemy and Flask-Migrate can discover them.
import app.models  # noqa: F401

from app.routes.auth import auth_bp
from app.routes.membership import membership_bp
from app.routes.invitations import invitations_bp
from app.routes.notifications import notifications_bp
from app.routes.announcements import announcements_bp, _process_due_schedules, _process_escalations\nfrom app.services.permissions import get_current_user
from app.services.announcement_scheduler import start_announcement_scheduler
from app.routes.teams import teams_bp
from app.routes.locations import locations_bp
from app.routes.zones import zones_bp
from app.routes.operations import operations_bp
from app.routes.organization import organization_bp

from app.models.audit_log import AuditLog
from app.models.user import User


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)

    # Create missing tables, then apply idempotent additive changes for the
    # existing PostgreSQL schema. User-uploaded announcement bytes stay in
    # protected database tables rather than an ephemeral Render filesystem.
    with app.app_context():
        db.create_all()
        with db.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if "users" in tables:
                user_columns = {item["name"] for item in inspect(connection).get_columns("users")}
                termination_columns = {
                    "termination_requested_at": "TIMESTAMP WITH TIME ZONE",
                    "termination_effective_at": "TIMESTAMP WITH TIME ZONE",
                    "avatar_url": "VARCHAR(500)",
                    "preferences": "JSON",
                }
                for column, sql_type in termination_columns.items():
                    if column not in user_columns:
                        connection.execute(text(f"ALTER TABLE users ADD COLUMN {column} {sql_type}"))
            if "teams" in tables:
                team_columns = {item["name"] for item in inspect(connection).get_columns("teams")}
                if "capacity" not in team_columns:
                    connection.execute(text("ALTER TABLE teams ADD COLUMN capacity INTEGER"))
                if "whatsapp_group_url" not in team_columns:
                    connection.execute(text("ALTER TABLE teams ADD COLUMN whatsapp_group_url VARCHAR(500)"))
            if "zones" in tables:
                zone_columns = {item["name"] for item in inspect(connection).get_columns("zones")}
                if "team_id" not in zone_columns:
                    connection.execute(text("ALTER TABLE zones ADD COLUMN team_id INTEGER REFERENCES teams(id)"))
            if "organizations" in tables:
                columns = {item["name"] for item in inspect(connection).get_columns("organizations")}
                additive_columns = {
                    "description": "TEXT",
                    "avatar_url": "VARCHAR(500)",
                    "contact_email": "VARCHAR(254)",
                    "policy_text": "TEXT",
                    "whatsapp_supervisors_group_url": "VARCHAR(500)",
                }
                for column, sql_type in additive_columns.items():
                    if column not in columns:
                        connection.execute(text(f"ALTER TABLE organizations ADD COLUMN {column} {sql_type}"))
            if "announcements" in tables:
                columns = {item["name"] for item in inspect(connection).get_columns("announcements")}
                had_status = "status" in columns
                additive_columns = {
                    "target_location_id": "INTEGER REFERENCES locations(id)",
                    "target_zone_id": "INTEGER REFERENCES zones(id)",
                    "audience_scope": "VARCHAR(20) NOT NULL DEFAULT 'organization'",
                    "severity": "VARCHAR(16) NOT NULL DEFAULT 'info'",
                    "status": "VARCHAR(20) NOT NULL DEFAULT 'published'",
                    "alert_status": "VARCHAR(20) NOT NULL DEFAULT 'open'",
                    "is_pinned": "BOOLEAN NOT NULL DEFAULT FALSE",
                    "requires_acknowledgement": "BOOLEAN NOT NULL DEFAULT FALSE",
                    "escalate_to_admin": "BOOLEAN NOT NULL DEFAULT FALSE",
                    "escalation_after_minutes": "INTEGER",
                    "escalated_at": "TIMESTAMP WITH TIME ZONE",
                    "resolved_at": "TIMESTAMP WITH TIME ZONE",
                    "resolved_by_id": "INTEGER REFERENCES users(id)",
                    "scheduled_at": "TIMESTAMP WITH TIME ZONE",
                    "archived_at": "TIMESTAMP WITH TIME ZONE",
                }
                for column, sql_type in additive_columns.items():
                    if column not in columns:
                        connection.execute(text(f"ALTER TABLE announcements ADD COLUMN {column} {sql_type}"))
                # Preserve the publish/draft state of legacy announcement rows.
                # Repair legacy rows if the first additive deployment ran
                # with a default status before draft/published reconciliation.
                connection.execute(text(
                    "UPDATE announcements SET status = CASE WHEN published = TRUE THEN 'published' ELSE 'draft' END "
                    "WHERE (status = 'published' AND published = FALSE) OR status IS NULL OR status = ''"
                ))

    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-CSRF-TOKEN"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    # Register API routes.
    app.register_blueprint(auth_bp)
    app.register_blueprint(membership_bp)
    app.register_blueprint(invitations_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(announcements_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(zones_bp)
    app.register_blueprint(operations_bp)
    app.register_blueprint(organization_bp)

    @app.before_request
    def _finalize_due_member_terminations():
        # Finalize elapsed seven-day termination schedules on the first server
        # request after their deadline, without deleting historical records.
        try:
            now = datetime.now(timezone.utc)
            due_members = User.query.filter(
                User.account_status.in_(("termination_pending", "suspended")),
                User.termination_effective_at.isnot(None),
                User.termination_effective_at <= now,
            ).all()
            for member in due_members:
                member.account_status = "terminated"
                db.session.add(AuditLog(
                    actor_id=None,
                    organization_id=member.organization_id,
                    action="organization.member_termination_completed",
                    resource_type="User",
                    resource_id=str(member.id),
                    details={
                        "member_id": member.id,
                        "member_name": member.full_name,
                        "email": member.email,
                        "termination_effective_at": member.termination_effective_at.isoformat() if member.termination_effective_at else None,
                        "message": "Termination deadline elapsed. Account access was disabled; historical records were retained.",
                    },
                ))
            if due_members:
                db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not finalize due member terminations.")

    @app.before_request
    def _process_due_announcements_and_escalations():
        # Run due communication checks during ordinary authenticated activity.
        # This avoids requiring the announcements screen itself to stay open.
        if not request.path.startswith("/api/") or request.path.startswith("/api/health"):
            return None
        try:
            user = get_current_user()
            if user is not None and user.organization_id:
                _process_due_schedules(user.organization_id)
                _process_escalations(user.organization_id)
        except Exception:
            # Anonymous/login requests and expired sessions should continue to
            # their normal handler. Do not turn a missed scheduler check into a
            # failed unrelated API request.
            db.session.rollback()
        return None

    @app.get("/api/health")
    def health():
        registered_routes = {rule.rule for rule in app.url_map.iter_rules()}
        return jsonify({
            "status": "ok",
            "service": "CleanTrack API",
            "revision": os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "unknown")),
            "organization_api_registered": (
                "/api/organizations/current" in registered_routes
                and "/api/organizations/members" in registered_routes
                and "/api/organizations/directory-options" in registered_routes
            ),
            "announcements_api_registered": (
                "/api/announcements" in registered_routes
                and "/api/announcements/options" in registered_routes
            ),
            "announcements_scheduler_running": bool(
                getattr(app.extensions.get("cleantrack_announcement_scheduler"), "running", False)
            ),
        }), 200

    @app.get("/api/health/db")
    def database_health():
        try:
            with db.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return jsonify({"status": "ok", "database": "connected"}), 200
        except Exception:
            app.logger.exception("Database health check failed")
            return jsonify({"status": "error", "database": "unavailable"}), 503

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Resource not found"}), 404

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "Request is too large"}), 413

    @app.errorhandler(500)
    def internal_error(_error):
        return jsonify({"error": "An internal server error occurred"}), 500

    start_announcement_scheduler(app)

    return app
