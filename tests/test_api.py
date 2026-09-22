"""The HTTP surface. No model calls -- the provider is scripted.

What matters here is the contract the UI depends on: the run/advance cycle, that
auth is actually enforced, that a missing run is a 404 rather than a 500, and
that the trace comes back in the shape the viewer expects.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import runtime
from agent.findings import SUBMIT_FINDINGS
from api.app import app
from core import config
from llm import fake

from .conftest import SAMPLE_FINDINGS as FINDINGS

HEADERS = {"X-API-Key": config.app_api_key() or "local"}


@pytest.fixture
def client(monkeypatch, own_conn):
    # The API opens its own pooled connections; point them at the test session's
    # connection so a test can clean up what it created.
    from contextlib import contextmanager

    @contextmanager
    def one_connection():
        yield own_conn

    monkeypatch.setattr("api.app.connection", one_connection)
    yield TestClient(app)
    own_conn.rollback()
    own_conn.execute(
        "delete from ops.agent_runs where actor in ('operator','local')")
    own_conn.commit()


def scripted(monkeypatch, *completions):
    provider = fake.ScriptedProvider(script=list(completions))
    monkeypatch.setattr(runtime.factory, "create", lambda name=None: provider)
    return provider


def test_health_reports_configuration_without_calling_anything(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "coe_gateway_configured" in body
    assert body["auth_required"] is True


def test_auth_is_enforced(client):
    assert client.post("/api/runs", json={"question": "why did it drop?"}
                       ).status_code == 401
    assert client.get("/api/runs/whatever").status_code == 401


def test_a_short_question_is_rejected_before_a_run_is_created(client):
    response = client.post("/api/runs", json={"question": "why"}, headers=HEADERS)
    assert response.status_code == 422


def test_the_full_run_advance_read_cycle(client, monkeypatch):
    scripted(monkeypatch,
             fake.calls(fake.call("list_metrics", {}), text="Orienting."),
             fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)))

    created = client.post("/api/runs", json={"question": "why did it drop?"},
                          headers=HEADERS)
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    first = client.post(f"/api/runs/{run_id}/advance", headers=HEADERS).json()
    assert first["status"] == "running"
    assert len(first["steps"]) == 1
    assert first["steps"][0]["assistant_text"] == "Orienting."
    assert first["tool_calls"][0]["tool"] == "list_metrics"

    second = client.post(f"/api/runs/{run_id}/advance", headers=HEADERS).json()
    assert second["status"] == "completed"
    assert second["final_answer"]["verdict"] == "confirmed"

    # Advancing a finished run is a no-op, not an error: the UI polls.
    again = client.post(f"/api/runs/{run_id}/advance", headers=HEADERS).json()
    assert again["status"] == "completed"
    assert len(again["steps"]) == 2

    fetched = client.get(f"/api/runs/{run_id}", headers=HEADERS).json()
    assert fetched["run_id"] == run_id
    assert fetched["cost_usd"] == pytest.approx(0.0, abs=1e-6)  # fake provider


def test_a_run_can_be_cancelled_mid_flight(client, monkeypatch):
    scripted(monkeypatch, fake.calls(fake.call("list_metrics", {})))
    run_id = client.post("/api/runs", json={"question": "why did it drop?"},
                         headers=HEADERS).json()["run_id"]
    client.post(f"/api/runs/{run_id}/advance", headers=HEADERS)

    cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=HEADERS).json()
    assert cancelled["status"] == "cancelled"


def test_missing_run_is_a_404(client):
    import uuid
    response = client.get(f"/api/runs/{uuid.uuid4()}", headers=HEADERS)
    assert response.status_code == 404


def test_listing_runs_omits_the_trace(client, monkeypatch):
    scripted(monkeypatch, fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)))
    client.post("/api/runs", json={"question": "why did it drop?"},
                headers=HEADERS)
    runs = client.get("/api/runs?limit=5", headers=HEADERS).json()["runs"]
    assert runs and "steps" not in runs[0]
    assert {"run_id", "question", "status", "cost_usd"} <= set(runs[0])
