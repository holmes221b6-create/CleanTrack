import pytest
from flask_jwt_extended import create_access_token

from config import Config
from app import create_app
from app.extensions import db
from app.models.organization import Organization
from app.models.user import User
from app.models.team import Team
from app.models.operation import Operation
from app.services.permissions import VALID_ROLES


class TenantTestConfig(Config):
    TESTING = True
    SECRET_KEY = "tenant-test-secret"
    JWT_SECRET_KEY = "tenant-test-jwt-secret"
    JWT_TOKEN_LOCATION = ["headers"]
    JWT_COOKIE_CSRF_PROTECT = False
    CORS_ORIGINS = []
    RATELIMIT_STORAGE_URI = "memory://"


@pytest.fixture()
def tenant_app(tmp_path, monkeypatch):
    import app as app_module

    # Do not start background threads in an isolated request/authorization test.
    monkeypatch.setattr(app_module, "start_announcement_scheduler", lambda _app: None)
    monkeypatch.setattr(app_module, "start_operation_scheduler", lambda _app: None)

    class TestConfig(TenantTestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "tenant-isolation.db")
        SQLALCHEMY_ENGINE_OPTIONS = {}

    flask_app = create_app(TestConfig)
    with flask_app.app_context():
        org_a = Organization(name="Tenant A")
        org_b = Organization(name="Tenant B")
        db.session.add_all([org_a, org_b])
        db.session.flush()

        admin_a = User(
            email="admin-a@example.test", full_name="Admin A", role="Admin",
            account_status="active", organization_id=org_a.id, password_hash="unused",
        )
        admin_b = User(
            email="admin-b@example.test", full_name="Admin B", role="Admin",
            account_status="active", organization_id=org_b.id, password_hash="unused",
        )
        staff_a = User(
            email="staff-a@example.test", full_name="Staff A", role="Staff",
            account_status="active", organization_id=org_a.id, password_hash="unused",
        )
        staff_b = User(
            email="staff-b@example.test", full_name="Staff B", role="Staff",
            account_status="active", organization_id=org_b.id, password_hash="unused",
        )
        db.session.add_all([admin_a, admin_b, staff_a, staff_b])
        db.session.flush()

        team_b = Team(name="Tenant B Team", organization_id=org_b.id)
        db.session.add(team_b)
        db.session.flush()

        operation_a = Operation(
            title="Tenant A operation", organization_id=org_a.id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id,
            status="Assigned",
        )
        operation_b = Operation(
            title="Tenant B operation", organization_id=org_b.id,
            created_by_id=admin_b.id, assigned_to_id=staff_b.id,
            team_id=team_b.id, status="Assigned",
        )
        db.session.add_all([operation_a, operation_b])
        db.session.commit()

        def token(user):
            return create_access_token(
                identity=str(user.id),
                additional_claims={"user_id": user.id},
            )

        ids = {
            "operation_a": operation_a.id,
            "operation_b": operation_b.id,
            "team_b": team_b.id,
            "admin_a_token": token(admin_a),
            "staff_a_token": token(staff_a),
        }

    flask_app.config["TENANT_TEST_IDS"] = ids
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def test_only_the_three_canonical_roles_are_valid():
    assert VALID_ROLES == {"Admin", "Supervisor", "Staff"}


def test_admin_cannot_read_or_modify_another_organizations_operation(tenant_app):
    client = tenant_app.test_client()
    ids = tenant_app.config["TENANT_TEST_IDS"]
    headers = {"Authorization": "Bearer " + ids["admin_a_token"]}

    listing = client.get("/api/operations", headers=headers)
    assert listing.status_code == 200
    payload = listing.get_json()
    rows = payload if isinstance(payload, list) else payload.get("operations", [])
    returned_ids = {row["id"] for row in rows}
    assert ids["operation_a"] in returned_ids
    assert ids["operation_b"] not in returned_ids

    detail = client.get(f'/api/operations/{ids["operation_b"]}', headers=headers)
    assert detail.status_code == 404

    update = client.patch(
        f'/api/operations/{ids["operation_b"]}',
        headers=headers,
        json={"title": "Unauthorized cross-tenant change"},
    )
    assert update.status_code == 404


def test_staff_cannot_read_another_organizations_operation(tenant_app):
    client = tenant_app.test_client()
    ids = tenant_app.config["TENANT_TEST_IDS"]
    headers = {"Authorization": "Bearer " + ids["staff_a_token"]}

    listing = client.get("/api/operations", headers=headers)
    assert listing.status_code == 200
    payload = listing.get_json()
    rows = payload if isinstance(payload, list) else payload.get("operations", [])
    returned_ids = {row["id"] for row in rows}
    assert ids["operation_a"] in returned_ids
    assert ids["operation_b"] not in returned_ids

    detail = client.get(f'/api/operations/{ids["operation_b"]}', headers=headers)
    assert detail.status_code == 404


def test_admin_cannot_assign_another_organizations_team_to_an_operation(tenant_app):
    client = tenant_app.test_client()
    ids = tenant_app.config["TENANT_TEST_IDS"]
    headers = {"Authorization": "Bearer " + ids["admin_a_token"]}

    response = client.post(
        "/api/operations",
        headers=headers,
        json={
            "title": "Cross-tenant team assignment attempt",
            "task_type": "General Operations",
            "team_id": ids["team_b"],
        },
    )
    assert response.status_code == 400
