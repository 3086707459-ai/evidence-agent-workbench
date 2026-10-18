"""Exercise a running service. Creates a demo run, approves one local ticket and replays it."""

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument("--output", default="out/http-smoke.json")
    parser.add_argument("--sample-run", default=None)
    args = parser.parse_args()
    token = os.getenv("AGENT_API_TOKEN", "")
    approval_token = os.getenv("AGENT_APPROVAL_TOKEN", "") or token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    key = "smoke-" + uuid.uuid4().hex

    with httpx.Client(base_url=args.url, timeout=10, headers=headers, trust_env=False) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response

        def wait(run_id, status):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = request("GET", f"/api/runs/{run_id}").json()
                if run["status"] == status:
                    return run
                if run["status"] in {"failed", "cancelled", "rejected"}:
                    raise RuntimeError(f"Run stopped: {run['status']}")
                time.sleep(0.05)
            raise TimeoutError(f"Run did not reach {status}")

        health = request("GET", "/healthz").json()
        assert "开始研究" in request("GET", "/").text
        run = request(
            "POST",
            "/api/runs",
            headers={"Idempotency-Key": key},
            json={
                "goal": "比较 Atlas 与 Boreal 的离线部署、审计和三年成本，引用原文",
                "provider": "demo",
                "create_ticket": True,
            },
        ).json()
        run = wait(run["id"], "awaiting_approval")
        assert not any(t["run_id"] == run["id"] for t in request("GET", "/api/tickets").json())
        approval_headers = {"Idempotency-Key": key + "-approve"}
        if approval_token:
            approval_headers["Authorization"] = "Bearer " + approval_token
        request(
            "POST",
            f"/api/runs/{run['id']}/approval",
            headers=approval_headers,
            json={
                "expected_version": run["version"],
                "digest": run["approval"]["digest"],
                "decision": "approve",
            },
        )
        run = wait(run["id"], "completed")
        trace = request("GET", f"/api/runs/{run['id']}/events/stream").text
        assert "decision.prepared" in trace and "tool.completed" in trace
        replay = request(
            "POST", f"/api/runs/{run['id']}/replay", headers={"Idempotency-Key": key + "-replay"}
        ).json()
        replay = wait(replay["id"], "completed")
        assert replay["report"] == run["report"] and not replay["ticket_id"]
        assert sum(t["run_id"] == run["id"] for t in request("GET", "/api/tickets").json()) == 1
        assert not any(t["run_id"] == replay["id"] for t in request("GET", "/api/tickets").json())
        result = {
            "actual_http_server": True,
            "health": health,
            "static_ui_served": True,
            "approval_and_delivery": "passed",
            "sse_trace": "passed",
            "replay_no_extra_ticket": "passed",
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        if args.sample_run:
            Path(args.sample_run).write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
