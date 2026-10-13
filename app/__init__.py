import os
from datetime import datetime, timezone


from flask import Flask, jsonify
from sqlalchemy import inspect, text

from config import Config
from app.extensions import cors, db, jwt, limiter, migrate

# Load models so SQLAlchemy and Flask-Migrate can discover them.
import app.models  # noqa: F401

from app.routes.auth import auth_bp
from app.routes.membership import membership_bp
from app.routes.invitations import invitations_bp
from app.routes.notifications import notifications_bp
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

    # Ensure tables exist for a newly provisioned database. This creates
    # missing tables but does not alter existing tables; use migrations for
    # future schema changes.
    with app.app_context():
        db.create_all()

        # Additive migration for organizations already present in the live DB.
        # db.create_all() creates missing tables but does not add new columns.
        with db.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if "users" in tables:
                user_columns = {item["name"] for item in inspect(connection).get_columns("users")}
                termination_columns = {
                    "termination_requested_at": "TIMESTAMP WITH TIME ZONE",
                    "termination_effective_at": "TIMESTAMP WITH TIME ZONE",
                }
                for column, sql_type in termination_columns.items():
                    if column not in user_columns:
                        connection.execute(text(
                            f"ALTER TABLE users ADD COLUMN {column} {sql_type}"
                        ))
            if "organizations" in tables:
                columns = {item["name"] for item in inspect(connection).get_columns("organizations")}
                additive_columns = {
                    "description": "TEXT",
                    "avatar_url": "VARCHAR(500)",
                    "contact_email": "VARCHAR(254)",
                    "policy_text": "TEXT",
                }
                for column, sql_type in additive_columns.items():
                    if column not in columns:
                        connection.execute(text(
                            f"ALTER TABLE organizations ADD COLUMN {column} {sql_type}"
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
                User.account_status == "termination_pending",
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
                        "message": "Termination grace period elapsed. Account access was disabled; historical records were retained.",
                    },
                ))
            if due_members:
                db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not finalize due member terminations.")

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
        }), 200

    @app.get("/api/health/db")
    def database_health():
        try:
            with db.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")

            return jsonify({
                "status": "ok",
                "database": "connected",
            }), 200

        except Exception:
            app.logger.exception("Database health check failed")
            return jsonify({
                "status": "error",
                "database": "unavailable",
            }), 503

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Resource not found"}), 404

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "Request is too large"}), 413

    @app.errorhandler(500)
    def internal_error(_error):
        return jsonify({
            "error": "An internal server error occurred"
        }), 500

    return app