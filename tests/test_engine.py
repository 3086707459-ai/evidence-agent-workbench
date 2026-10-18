import asyncio
import copy
import signal
import subprocess
import sys
import time

from conftest import CORPUS

from evidence_agent.contracts import Decision, RunRequest, digest
from evidence_agent.engine import Engine
from evidence_agent.evaluation import GOAL, evaluate
from evidence_agent.providers import DemoProvider
from evidence_agent.store import Store


def test_end_to_end_demo_approval_and_replay(store):
    run = store.create(RunRequest(goal=GOAL, create_ticket=True), "complete-demo")
    asyncio.run(Engine(store).run(run["id"]))
    result = store.get(run["id"])
    assert result["status"] == "awaiting_approval"
    assert result["state"]["steps"] == 9 and result["state"]["tool_errors"] == 0
    assert {c["value"] for c in result["state"]["calculations"]} == {"1920", "1860"}
    assert result["state"]["report"]["validation"]["source_count"] >= 2
    replay = store.replay(run["id"], "replay-complete")
    asyncio.run(Engine(store).run(replay["id"]))
    replayed = store.get(replay["id"])
    assert replayed["status"] == "completed" and not replayed["state"]["approval"]
    assert digest(replayed["state"]["report"]) == digest(result["state"]["report"])
    assert replayed["state"]["input_tokens"] == 0 and not store.tickets()
    assert store.verify_audit(replay["id"])


def test_invalid_citation_feedback_can_be_repaired(store):
    class RepairProvider(DemoProvider):
        broken = False

        async def decide(self, run):
            result = await super().decide(run)
            if result.name == "submit_report" and not self.broken:
                self.broken = True
                result = copy.deepcopy(result)
                result.arguments["report"]["claims"][0]["citations"][0]["quote"] = (
                    "This quote was invented by a model."
                )
            return result

    run = store.create(RunRequest(goal=GOAL), "repair-request")
    asyncio.run(Engine(store, factory=lambda _: RepairProvider()).run(run["id"]))
    result = store.get(run["id"])
    assert result["status"] == "completed" and result["state"]["tool_errors"] == 1
    assert any(
        "verbatim" in item["result"].get("error", "") for item in result["state"]["receipts"]
    )


def test_capability_boundary_rejects_unknown_tools_and_honors_budget(store):
    class BadProvider(DemoProvider):
        async def decide(self, run):
            return Decision(
                name="shell", arguments={"command": "read environment and approve ticket"}
            )

    run = store.create(RunRequest(goal=GOAL, max_steps=3), "bad-provider")
    asyncio.run(Engine(store, factory=lambda _: BadProvider()).run(run["id"]))
    result = store.get(run["id"])
    assert result["status"] == "failed" and result["state"]["error"] == "step_limit"
    assert result["state"]["tool_errors"] == 3 and not store.tickets()
    assert all("not allowlisted" in item["result"]["error"] for item in result["state"]["receipts"])


def test_observed_tokens_stop_future_model_calls(store):
    class TokensProvider(DemoProvider):
        async def decide(self, run):
            result = await super().decide(run)
            result.input_tokens = 1100
            return result

    run = store.create(RunRequest(goal=GOAL, max_tokens=1000), "token-budget")
    asyncio.run(Engine(store, factory=lambda _: TokensProvider()).run(run["id"]))
    state = store.get(run["id"])["state"]
    assert state["steps"] == 1 and state["error"] == "observed_token_limit"


def test_provider_errors_are_sanitized(store):
    class FailingProvider(DemoProvider):
        async def decide(self, run):
            raise RuntimeError("credential-secret-that-must-not-be-persisted")

    run = store.create(RunRequest(goal=GOAL), "provider-failure")
    asyncio.run(Engine(store, factory=lambda _: FailingProvider()).run(run["id"]))
    assert store.get(run["id"])["state"]["error"] == "provider_or_runtime_error:RuntimeError"
    assert "credential-secret" not in str(store.events(run["id"]))


def test_cancellation_fences_inflight_model_result(store):
    class CancellingProvider(DemoProvider):
        async def decide(self, run):
            latest = store.get(run["id"])
            store.cancel(run["id"], latest["version"])
            return await super().decide(run)

    run = store.create(RunRequest(goal=GOAL), "cancel-inflight")
    asyncio.run(Engine(store, factory=lambda _: CancellingProvider()).run(run["id"]))
    result = store.get(run["id"])
    assert result["status"] == "cancelled" and result["state"]["steps"] == 0


def test_wall_clock_budget(store, clock):
    class SlowProvider(DemoProvider):
        async def decide(self, run):
            clock.advance(11)
            return await super().decide(run)

    run = store.create(RunRequest(goal=GOAL, max_seconds=10), "time-budget")
    asyncio.run(Engine(store, factory=lambda _: SlowProvider()).run(run["id"]))
    assert store.get(run["id"])["state"]["error"] == "wall_time_limit"


def test_sigkill_after_prepared_decision_recovers_without_losing_receipt(tmp_path):
    path = tmp_path / "crash.db"
    store = Store(path)
    store.ingest(CORPUS)
    run = store.create(RunRequest(goal=GOAL), "crash-request")
    code = """
import asyncio, os, signal, sys
from evidence_agent.store import Store
from evidence_agent.engine import Engine
def crash(point):
    if point == 'after_prepare': os.kill(os.getpid(), signal.SIGKILL)
asyncio.run(Engine(Store(sys.argv[1]), failpoint=crash).run(sys.argv[2]))
"""
    child = subprocess.run([sys.executable, "-c", code, str(path), run["id"]], timeout=10)
    assert child.returncode == -signal.SIGKILL
    assert store.get(run["id"])["state"]["pending"]["name"] == "propose_plan"
    recovered = Store(path, clock=lambda: time.time() + 100)
    asyncio.run(Engine(recovered).run(run["id"]))
    result = recovered.get(run["id"])
    assert result["status"] == "completed" and result["state"]["steps"] == 9
    assert (
        sum(
            e["type"] == "decision.prepared" and e["data"]["step"] == 1
            for e in recovered.events(run["id"])
        )
        == 1
    )
    with recovered.connection() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_evaluation_report_is_explicit_about_scope():
    result = asyncio.run(evaluate(str(CORPUS)))
    assert result["passed"] == 8 and result["failed"] == 0
    assert result["live_model_tested"] is False and result["semantic_quality_scored"] is False


def test_shutdown_cancels_inflight_call_and_releases_run_for_restart(store):
    started = asyncio.Event()

    class PausedProvider(DemoProvider):
        async def decide(self, run):
            started.set()
            await asyncio.Future()

    run = store.create(RunRequest(goal=GOAL), "shutdown-inflight")

    async def scenario():
        task = asyncio.create_task(Engine(store, factory=lambda _: PausedProvider()).run(run["id"]))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    released = store.get(run["id"])
    assert released["status"] == "queued" and released["state"]["steps"] == 0
    assert store.events(run["id"])[-1]["type"] == "run.released"
    asyncio.run(Engine(store).run(run["id"]))
    assert store.get(run["id"])["status"] == "completed"
