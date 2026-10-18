import asyncio
import json
import time

import pytest
from conftest import CORPUS
from fastapi.testclient import TestClient

from evidence_agent.api import BoundaryMiddleware, Settings, create_app
from evidence_agent.contracts import RunRequest
from evidence_agent.engine import Engine
from evidence_agent.evaluation import GOAL

API_TOKEN = "request-role-" + "a" * 32
APPROVAL_TOKEN = "approval-role-" + "b" * 32


def settings(store, **changes):
    return Settings(database=store.path, corpus=str(CORPUS), **changes)


def wait_for(client, run_id, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/runs/{run_id}").json()
        if result["status"] == status:
            return result
        time.sleep(0.02)
    pytest.fail(f"Run did not reach {status}: {result}")


def test_api_idempotency_validation_versions_and_errors(store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(settings(store), store=store, start_dispatcher=False)) as client:
        assert client.get("/healthz").json()["healthy"]
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/unknown.js").status_code == 404
        assert not client.get("/api/config").json()["openai_enabled"]
        assert client.post("/api/runs", json={"goal": GOAL}).status_code == 422
        headers = {"Idempotency-Key": "api-request-001"}
        first = client.post("/api/runs", json={"goal": GOAL}, headers=headers)
        assert first.status_code == 201
        run = first.json()
        assert (
            client.post("/api/runs", json={"goal": GOAL}, headers=headers).json()["id"] == run["id"]
        )
        assert (
            client.post("/api/runs", json={"goal": "Changed goal"}, headers=headers).status_code
            == 409
        )
        assert (
            client.post(
                "/api/runs", json={"goal": GOAL, "extra": True}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/runs", json={"goal": GOAL, "provider": "openai"}, headers=headers
            ).status_code
            == 400
        )
        assert client.get(f"/api/runs/{run['id']}/report").status_code == 409
        assert client.post(f"/api/runs/{run['id']}/replay", headers=headers).status_code == 409
        assert client.get("/api/runs/does-not-exist").status_code == 404
        assert client.get(f"/api/runs/{run['id']}/events?after=-1").status_code == 422
        cancel = f"/api/runs/{run['id']}/cancel"
        assert client.post(cancel, json={"expected_version": 99}).status_code == 409
        assert (
            client.post(cancel, json={"expected_version": run["version"]}).json()["status"]
            == "cancelled"
        )
        assert len(client.get("/api/runs").json()) == 1
        assert client.get(f"/api/runs/{run['id']}/audit").json()["chain_valid"]
        assert "nosniff" in first.headers["x-content-type-options"]
        assert "script-src 'self'" in first.headers["content-security-policy"]


def test_separate_approval_credential_cannot_be_used_as_general_credential(store):
    app = create_app(
        settings(store, api_token=API_TOKEN, approval_token=APPROVAL_TOKEN),
        store=store,
        start_dispatcher=False,
    )
    run = store.create(RunRequest(goal=GOAL, create_ticket=True), "protected-run")
    asyncio.run(Engine(store).run(run["id"]))
    run = store.get(run["id"])
    request_headers = {"Authorization": f"Bearer {API_TOKEN}"}
    approval_headers = {
        "Authorization": f"Bearer {APPROVAL_TOKEN}",
        "Idempotency-Key": "approval-api-001",
    }
    body = {
        "expected_version": run["version"],
        "digest": run["state"]["approval"]["digest"],
        "decision": "approve",
    }
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/api/runs", headers=approval_headers).status_code == 401
        assert client.get("/api/runs", headers=request_headers).status_code == 200
        path = f"/api/runs/{run['id']}/approval"
        assert client.post(path, json=body, headers=request_headers).status_code == 401
        assert client.post(path, json=body, headers=approval_headers).json()["status"] == "approved"
        assert client.post(path, json=body, headers=approval_headers).status_code == 200
        store.deliver_one()
        assert len(client.get("/api/tickets", headers=request_headers).json()) == 1


def test_dispatcher_completion_approval_delivery_and_replay(store):
    with TestClient(create_app(settings(store), store=store)) as client:
        assert client.get("/healthz").json()["dispatcher_enabled"]
        run = client.post(
            "/api/runs",
            json={"goal": GOAL, "create_ticket": True},
            headers={"Idempotency-Key": "background-demo"},
        ).json()
        run = wait_for(client, run["id"], "awaiting_approval")
        assert client.get("/api/tickets").json() == []
        result = client.post(
            f"/api/runs/{run['id']}/approval",
            json={
                "expected_version": run["version"],
                "digest": run["approval"]["digest"],
                "decision": "approve",
            },
            headers={"Idempotency-Key": "background-approve"},
        )
        assert result.status_code == 200
        wait_for(client, run["id"], "completed")
        replay = client.post(
            f"/api/runs/{run['id']}/replay", headers={"Idempotency-Key": "background-replay"}
        ).json()
        replay = wait_for(client, replay["id"], "completed")
        assert replay["provider"] == "replay" and replay["report"] == run["report"]
        assert len(client.get("/api/tickets").json()) == 1
        assert "Atlas" in client.get(f"/api/runs/{run['id']}/report").text


def test_sse_terminal_run_resumes_after_cursor_without_duplicate_events(store):
    run = store.create(RunRequest(goal=GOAL), "sse-demo-001")
    asyncio.run(Engine(store).run(run["id"]))
    events = store.events(run["id"])
    cursor = events[3]["seq"]
    with TestClient(create_app(settings(store), store=store, start_dispatcher=False)) as client:
        response = client.get(f"/api/runs/{run['id']}/events/stream?after={cursor}")
        streamed = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert response.headers["content-type"].startswith("text/event-stream")
        assert [e["seq"] for e in streamed] == [e["seq"] for e in events[4:]]


def test_body_limit_covers_chunked_payload_before_route_parsing():
    reached = []
    sent = []

    async def app(scope, receive, send):
        reached.append(True)

    async def scenario():
        messages = iter(
            [
                {"type": "http.request", "body": b"x" * 40000, "more_body": True},
                {"type": "http.request", "body": b"x" * 30000, "more_body": False},
            ]
        )

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await BoundaryMiddleware(app, Settings())(
            {"type": "http", "method": "POST", "path": "/api/runs", "headers": []}, receive, send
        )

    asyncio.run(scenario())
    assert not reached and sent[0]["status"] == 413


@pytest.mark.parametrize(
    "changes",
    [
        {"bind": "0.0.0.0"},
        {"bind": "0.0.0.0", "api_token": API_TOKEN, "approval_token": API_TOKEN},
        {"api_token": "short"},
        {"port": 0},
        {"api_token": "a" * 25 + "\n"},
    ],
)
def test_network_and_credential_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        Settings(**changes).validate()


def test_capacity_surfaces_as_429(store):
    store.max_active = 1
    with TestClient(create_app(settings(store), store=store, start_dispatcher=False)) as client:
        assert (
            client.post(
                "/api/runs", json={"goal": GOAL}, headers={"Idempotency-Key": "capacity-001"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/runs", json={"goal": GOAL}, headers={"Idempotency-Key": "capacity-002"}
            ).status_code
            == 429
        )
