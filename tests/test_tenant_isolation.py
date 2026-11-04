from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from config import Config
from app import create_app
from app.extensions import db
from app.models.organization import Organization
from app.models.user import User
from app.models.team import Team
from app.models.operation import Operation
from app.models.operation_execution import OperationExecution
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



def _prediction_admin_headers(tenant_app, monkeypatch):
    import app.routes.analytics as analytics_routes

    # These tests exercise the prediction calculations, not billing entitlement.
    monkeypatch.setattr(analytics_routes, "_analytics_access_error", lambda _admin: None)
    ids = tenant_app.config["TENANT_TEST_IDS"]
    return {"Authorization": "Bearer " + ids["admin_a_token"]}


def test_prediction_endpoints_keep_all_queries_within_the_current_tenant(tenant_app, monkeypatch):
    headers = _prediction_admin_headers(tenant_app, monkeypatch)
    now = datetime.now(timezone.utc)
    with tenant_app.app_context():
        admin_a = User.query.filter_by(email="admin-a@example.test").one()
        staff_a = User.query.filter_by(email="staff-a@example.test").one()
        admin_b = User.query.filter_by(email="admin-b@example.test").one()
        staff_b = User.query.filter_by(email="staff-b@example.test").one()
        old = now - timedelta(days=12)
        due_past = now - timedelta(days=2)
        due_future = now + timedelta(days=2)
        team_a = Team(name="Tenant A Team", organization_id=admin_a.organization_id)
        db.session.add(team_a)
        db.session.flush()
        open_a = Operation(
            title="Tenant A open overdue", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id, team_id=team_a.id,
            status="Assigned", created_at=old, due_at=due_past,
        )
        completed_without_timestamp = Operation(
            title="Tenant A completed without timestamp", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id, team_id=team_a.id,
            status="Completed", created_at=old, due_at=due_past,
        )
        future_a = Operation(
            title="Tenant A future operation", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id, team_id=team_a.id,
            status="Assigned", created_at=old, due_at=due_future,
        )
        rejected_a = Operation(
            title="Tenant A rejected operation awaiting rework", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id, team_id=team_a.id,
            status="Rejected", created_at=old, due_at=due_future,
            submitted_at=now - timedelta(hours=2), reviewed_at=now - timedelta(hours=1),
        )
        rejected_past_a = Operation(
            title="Tenant A rejected past-deadline attempt", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id, team_id=team_a.id,
            status="Rejected", created_at=old, due_at=due_past,
            submitted_at=now - timedelta(days=3), reviewed_at=now - timedelta(days=2),
        )
        future_b = Operation(
            title="Tenant B secret future operation", organization_id=admin_b.organization_id,
            created_by_id=admin_b.id, assigned_to_id=staff_b.id,
            status="Assigned", created_at=old, due_at=due_future,
        )
        # Historical work must remain in training data after a staff account is deactivated.
        staff_a.account_status = "inactive"
        db.session.add_all([open_a, completed_without_timestamp, future_a, rejected_a, rejected_past_a, future_b])
        db.session.flush()
        execution_a = OperationExecution(
            operation_id=future_a.id, performed_by_id=staff_a.id,
            attempt_number=1, status="Submitted", started_at=now - timedelta(hours=3),
            submitted_at=now - timedelta(hours=2), created_at=now - timedelta(hours=3),
        )
        rejected_execution_a = OperationExecution(
            operation_id=rejected_a.id, performed_by_id=staff_a.id,
            attempt_number=1, status="Rejected", started_at=now - timedelta(hours=5),
            submitted_at=now - timedelta(hours=2), created_at=now - timedelta(hours=5),
        )
        execution_b = OperationExecution(
            operation_id=future_b.id, performed_by_id=staff_b.id,
            attempt_number=1, status="Submitted", started_at=now - timedelta(hours=5),
            submitted_at=now - timedelta(hours=4), created_at=now - timedelta(hours=5),
        )
        execution_b_retry = OperationExecution(
            operation_id=future_b.id, performed_by_id=staff_b.id,
            attempt_number=2, status="Submitted", started_at=now - timedelta(hours=3),
            submitted_at=now - timedelta(hours=2), created_at=now - timedelta(hours=3),
        )
        db.session.add_all([execution_a, rejected_execution_a, execution_b, execution_b_retry])
        db.session.commit()
        future_a_id = future_a.id
        rejected_a_id = rejected_a.id

    client = tenant_app.test_client()
    workload = client.get("/api/analytics/predictions", headers=headers)
    assert workload.status_code == 200
    workload_payload = workload.get_json()
    assert workload_payload["status"] == "available"
    # Five historical Tenant A operations; Tenant B's operation is excluded.
    # Rejected work remains part of incoming workload volume.
    assert workload_payload["sample_count"] == 5

    risk = client.get("/api/analytics/predictions/overdue-risk", headers=headers)
    assert risk.status_code == 200
    risk_payload = risk.get_json()
    assert risk_payload["historical_sample_count"] == 1
    assert all(row["title"] != "Tenant B secret future operation" for row in risk_payload["at_risk_operations"])
    assert any(row["operation_id"] == future_a_id for row in risk_payload["at_risk_operations"])
    assert any(row["operation_id"] == rejected_a_id for row in risk_payload["at_risk_operations"])

    duration = client.get("/api/analytics/predictions/completion-time", headers=headers)
    assert duration.status_code == 200
    duration_payload = duration.get_json()
    assert duration_payload["historical_execution_sample_count"] == 1
    assert all(row["title"] != "Tenant B secret future operation" for row in duration_payload["predictions"])
    assert any(row["operation_id"] == rejected_a_id for row in duration_payload["predictions"])

    workforce = client.get("/api/analytics/predictions/workforce-demand", headers=headers)
    assert workforce.status_code == 200
    workforce_payload = workforce.get_json()
    assert workforce_payload["status"] == "available"
    assert workforce_payload["historical_sample_count"] == 5
    assert [row["team_name"] for row in workforce_payload["teams"]] == ["Tenant A Team"]
    assert workforce_payload["teams"][0]["historical_operations"] == 5
    team_forecast = workforce_payload["teams"][0]
    assert len(team_forecast["forecast_by_day"]) == 7
    assert all(day["lower_estimate"] <= day["expected_operations"] <= day["upper_estimate"] for day in team_forecast["forecast_by_day"])
    assert all(day["weekday_sample_count"] >= 7 for day in team_forecast["forecast_by_day"])
    assert team_forecast["validation"]["training_days"] == 42
    assert team_forecast["validation"]["holdout_days"] == 14
    assert team_forecast["validation"]["model_mae"] is not None
    assert "not a confidence interval" in team_forecast["range_method"]

    anomalies = client.get("/api/analytics/predictions/anomalies", headers=headers)
    assert anomalies.status_code == 200
    assert "Tenant B secret future operation" not in str(anomalies.get_json()["findings"])


def test_anomaly_detection_excludes_completed_records_without_terminal_timestamps(tenant_app, monkeypatch):
    headers = _prediction_admin_headers(tenant_app, monkeypatch)
    now = datetime.now(timezone.utc)
    with tenant_app.app_context():
        admin_a = User.query.filter_by(email="admin-a@example.test").one()
        staff_a = User.query.filter_by(email="staff-a@example.test").one()
        old = now - timedelta(days=12)
        db.session.add_all([
            Operation(
                title="Open overdue outcome", organization_id=admin_a.organization_id,
                created_by_id=admin_a.id, assigned_to_id=staff_a.id,
                status="Assigned", created_at=old, due_at=now - timedelta(days=2),
            ),
            Operation(
                title="Completed but un-timestamped", organization_id=admin_a.organization_id,
                created_by_id=admin_a.id, assigned_to_id=staff_a.id,
                status="Completed", created_at=old, due_at=now - timedelta(days=2),
            ),
        ])
        db.session.commit()

    response = tenant_app.test_client().get("/api/analytics/predictions/anomalies", headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    # Only the still-open overdue operation has a defensible matured outcome.
    assert payload["matured_deadline_outcomes"] == 1
    assert "1.8x" in payload["method"]
    assert "15 percentage points" in payload["method"]
    assert "screening thresholds" in payload["method"]
    # A rejected attempt is not a final operation deadline outcome.
    with tenant_app.app_context():
        admin_a = User.query.filter_by(email="admin-a@example.test").one()
        staff_a = User.query.filter_by(email="staff-a@example.test").one()
        now = datetime.now(timezone.utc)
        db.session.add(Operation(
            title="Rejected attempt, not final outcome", organization_id=admin_a.organization_id,
            created_by_id=admin_a.id, assigned_to_id=staff_a.id,
            status="Rejected", created_at=now - timedelta(days=12),
            due_at=now - timedelta(days=2), submitted_at=now - timedelta(days=3),
            reviewed_at=now - timedelta(days=2),
        ))
        db.session.commit()
    second_response = tenant_app.test_client().get("/api/analytics/predictions/anomalies", headers=headers)
    assert second_response.status_code == 200
    assert second_response.get_json()["matured_deadline_outcomes"] == 1



def test_workload_forecast_returns_no_history_when_tenant_has_no_prior_operations(tenant_app, monkeypatch):
    headers = _prediction_admin_headers(tenant_app, monkeypatch)
    response = tenant_app.test_client().get("/api/analytics/predictions", headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "no_history"
    assert payload["forecast"] == []

    workforce = tenant_app.test_client().get(
        "/api/analytics/predictions/workforce-demand", headers=headers
    )
    assert workforce.status_code == 200
    workforce_payload = workforce.get_json()
    assert workforce_payload["status"] == "no_history"
    assert workforce_payload["historical_sample_count"] == 0
    assert workforce_payload["teams"] == []
