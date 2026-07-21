"""Phase 6 integration tests — approval API and page."""

import json

from fastapi.testclient import TestClient

from src.api.app import app
from src.database import init_database
from src.migration.plan_store import save_plan
from src.models.schemas import MigrationPlan, MigrationRisk

# Module-level TestClient does not trigger the lifespan; init tables.
init_database()

client = TestClient(app)


def _fresh_plan_id() -> int:
    assert client.post("/api/discovery/scan").status_code == 200
    assert client.post("/api/assessment/run").status_code == 200
    generate = client.post("/api/plans/generate")
    assert generate.status_code == 200
    return generate.json()["plan_id"]


def test_approval_flow():
    plan_id = _fresh_plan_id()

    req = client.post(
        f"/api/plans/{plan_id}/request-approval",
        json={"user": "alice", "comment": "please review"},
    )
    assert req.status_code == 200
    approval_id = req.json()["approval_id"]
    assert req.json()["status"] == "PENDING"

    # Pending -> deployment blocked.
    status = client.get(f"/api/plans/{plan_id}/approval-status").json()
    assert status["status"] == "PENDING"
    assert status["can_deploy"] is False

    # Approve -> deployment allowed.
    ok = client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
    assert ok.status_code == 200
    status2 = client.get(f"/api/plans/{plan_id}/approval-status").json()
    assert status2["status"] == "APPROVED"
    assert status2["can_deploy"] is True

    fetched = client.get(f"/api/approvals/{approval_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "APPROVED"


def test_reject_flow():
    plan_id = _fresh_plan_id()
    approval_id = client.post(
        f"/api/plans/{plan_id}/request-approval", json={"user": "alice"}
    ).json()["approval_id"]
    rej = client.post(f"/api/approvals/{approval_id}/reject", json={"user": "bob"})
    assert rej.status_code == 200
    status = client.get(f"/api/plans/{plan_id}/approval-status").json()
    assert status["status"] == "REJECTED"
    assert status["can_deploy"] is False


def test_duplicate_decision_returns_409():
    plan_id = _fresh_plan_id()
    approval_id = client.post(
        f"/api/plans/{plan_id}/request-approval", json={"user": "alice"}
    ).json()["approval_id"]
    client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
    dup = client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
    assert dup.status_code == 409


def test_non_executable_plan_returns_409():
    rec = save_plan(
        MigrationPlan(executable=False, overall_risk=MigrationRisk.CRITICAL),
        assessment_id=88888,
    )
    resp = client.post(
        f"/api/plans/{rec['id']}/request-approval", json={"user": "alice"}
    )
    assert resp.status_code == 409


def test_unknown_plan_and_approval_return_404():
    assert (
        client.post("/api/plans/999999/request-approval", json={"user": "a"}).status_code
        == 404
    )
    assert (
        client.post("/api/approvals/999999/approve", json={"user": "a"}).status_code
        == 404
    )
    assert client.get("/api/approvals/999999").status_code == 404
    assert client.get("/api/plans/999999/approval-status").status_code == 404


def test_approval_page_loads():
    page = client.get("/approval")
    assert page.status_code == 200
    assert "Approval" in page.text
    assert client.get("/approval.js").status_code == 200
    assert client.get("/styles.css").status_code == 200


def test_blank_user_rejected():
    plan_id = _fresh_plan_id()
    # Empty and whitespace-only users are rejected with 400.
    assert (
        client.post(
            f"/api/plans/{plan_id}/request-approval", json={"user": ""}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/plans/{plan_id}/request-approval", json={"user": "   "}
        ).status_code
        == 400
    )
    # Decision endpoints enforce it too (checked before approval lookup).
    assert (
        client.post("/api/approvals/1/approve", json={"user": ""}).status_code == 400
    )
    assert (
        client.post("/api/approvals/1/reject", json={"user": "  "}).status_code == 400
    )


def test_favicon_returns_204():
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204
    assert resp.content == b""


def test_approval_js_has_button_state_logic():
    js = client.get("/approval.js").text
    # Button-state management is present and status-driven.
    assert "setButtonStates" in js
    assert "disabled" in js
    for status in ("PENDING", "APPROVED", "REJECTED", "INVALIDATED"):
        assert status in js


def test_approval_js_requires_nonblank_user():
    js = client.get("/approval.js").text
    assert "requireUser" in js
    assert "non-blank" in js


def test_no_secrets_in_approval_api():
    plan_id = _fresh_plan_id()
    client.post(f"/api/plans/{plan_id}/request-approval", json={"user": "alice"})
    status = client.get(f"/api/plans/{plan_id}/approval-status")
    serialized = json.dumps(status.json())
    for token in (
        "password",
        "client_secret",
        "accountKey",
        "connectionString",
        "accessToken",
        "servicePrincipalKey",
    ):
        assert token not in serialized
