

from flask import Flask, jsonify

from config import Config
from app.extensions import cors, db, jwt, limiter, migrate

# Load models so SQLAlchemy and Flask-Migrate can discover them.
import app.models  # noqa: F401

from app.routes.auth import auth_bp
from app.routes.membership import membership_bp
from app.routes.teams import teams_bp
from app.routes.locations import locations_bp
from app.routes.zones import zones_bp
from app.routes.operations import operations_bp


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

    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
        allow_headers=["Content-Type", "X-CSRF-TOKEN"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    # Register API routes.
    app.register_blueprint(auth_bp)
    app.register_blueprint(membership_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(zones_bp)
    app.register_blueprint(operations_bp)

    @app.get("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "service": "CleanTrack API",
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