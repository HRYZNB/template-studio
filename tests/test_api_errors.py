"""Regression tests for the unified API error envelope."""

from fastapi.testclient import TestClient

import app.main as main


def test_api_errors_have_stable_code_message_and_trace_id() -> None:
    client = TestClient(main.app)

    response = client.get("/api/v1/template-drafts/not-found")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "DRAFT_NOT_FOUND"
    assert payload["error"]["message"]
    assert payload["error"]["action"]
    assert payload["error"]["traceId"]
    assert payload["detail"] == payload["error"]


def test_request_validation_errors_include_field_details() -> None:
    client = TestClient(main.app)

    response = client.post("/api/v1/materials/search", json={"limit": 0})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "REQUEST_INVALID"
    assert payload["error"]["fields"]
