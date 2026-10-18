"""A checkpointed agent loop with fencing and recoverable prepared decisions."""

import asyncio
import contextlib
import copy
import uuid

from pydantic import ValidationError

from .contracts import ARGUMENT_MODELS, Decision, LeaseLost, ToolError, digest
from .providers import ReplayProvider, provider_for
from .store import Store
from .tools import calculate, render_report, search, verify_report


class Engine:
    def __init__(
        self, store: Store, *, factory=provider_for, lease_seconds: float = 90, failpoint=None
    ):
        self.store = store
        self.factory = factory
        self.lease_seconds = lease_seconds
        self.failpoint = failpoint or (lambda _: None)

    async def run(self, run_id: str):
        lease = self.store.claim(run_id, uuid.uuid4().hex, self.lease_seconds)
        if lease is None:
            return
        provider = None

        async def heartbeat():
            while True:
                await asyncio.sleep(max(0.05, self.lease_seconds / 3))
                if not self.store.renew(lease, self.lease_seconds):
                    return

        task = asyncio.create_task(heartbeat())
        try:
            run = self.store.get(run_id)
            if run["state"]["started_at"] is None:
                state = copy.deepcopy(run["state"])
                state["started_at"] = self.store.clock()
                run = self.store.checkpoint(lease, state, "run.started", {})
            while run["status"] == "running":
                state = copy.deepcopy(run["state"])
                if state["pending"] is None:
                    reason = self._budget(run)
                    if reason:
                        state["error"] = reason
                        self.store.checkpoint(
                            lease, state, "budget.exhausted", {"reason": reason}, "failed"
                        )
                        return
                    if not self.store.renew(lease, self.lease_seconds):
                        raise LeaseLost()
                    if provider is None:
                        replay = state.get("replay")
                        provider = (
                            ReplayProvider(replay["decisions"], start=state["steps"])
                            if replay
                            else self.factory(run["request"]["provider"])
                        )
                    elapsed = self.store.clock() - state["started_at"]
                    timeout = max(0.01, min(65, run["request"]["max_seconds"] - elapsed))
                    decision = await asyncio.wait_for(provider.decide(run), timeout=timeout)
                    state["steps"] += 1
                    state["input_tokens"] += decision.input_tokens
                    state["output_tokens"] += decision.output_tokens
                    state["pending"] = decision.model_dump()
                    run = self.store.checkpoint(
                        lease,
                        state,
                        "decision.prepared",
                        {
                            "step": state["steps"],
                            "tool": decision.name,
                            "arguments_digest": digest(decision.arguments),
                            "input_tokens": decision.input_tokens,
                            "output_tokens": decision.output_tokens,
                        },
                    )
                    # A process crash here resumes the persisted decision without asking the model again.
                    self.failpoint("after_prepare")
                state = copy.deepcopy(run["state"])
                decision = Decision.model_validate(state["pending"])
                result, next_status = self._execute(run, state, decision)
                receipt = {
                    "step": state["steps"],
                    "name": decision.name,
                    "arguments": decision.arguments,
                    "result": result,
                    "response_id": decision.response_id,
                    "decision": decision.model_dump(),
                }
                state["receipts"].append(receipt)
                state["pending"] = None
                run = self.store.checkpoint(
                    lease,
                    state,
                    "tool.completed",
                    {
                        "step": state["steps"],
                        "tool": decision.name,
                        "result_digest": digest(result),
                        "ok": "error" not in result,
                        "summary": result.get("summary", result.get("error", "")),
                    },
                    next_status,
                )
                self.failpoint("after_complete")
        except asyncio.CancelledError:
            self.store.release(lease)
            raise
        except LeaseLost:
            return
        except Exception as error:
            # SDK errors may contain sensitive request details: persist only an error class.
            with contextlib.suppress(LeaseLost):
                state = self.store.get(run_id)["state"]
                state["error"] = "provider_or_runtime_error:" + type(error).__name__
                self.store.checkpoint(
                    lease, state, "run.failed", {"error": state["error"]}, "failed"
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if provider:
                await provider.close()

    def _budget(self, run: dict) -> str | None:
        state, request = run["state"], run["request"]
        if state["steps"] >= request["max_steps"]:
            return "step_limit"
        if state["input_tokens"] + state["output_tokens"] >= request["max_tokens"] - 256:
            return "observed_token_limit"
        if self.store.clock() - state["started_at"] >= request["max_seconds"]:
            return "wall_time_limit"
        return None

    def _execute(self, run: dict, state: dict, decision: Decision) -> tuple[dict, str]:
        try:
            if decision.name not in ARGUMENT_MODELS:
                raise ToolError("Tool is not allowlisted")
            args = ARGUMENT_MODELS[decision.name].model_validate(decision.arguments)
            if not state["plan"] and decision.name != "propose_plan":
                raise ToolError("Propose a plan before using tools")
            if decision.name == "propose_plan":
                state["plan"] = args.steps
                return {"plan": args.steps, "summary": "Task plan updated"}, "running"
            if decision.name == "search_evidence":
                hits = search(self.store.chunks(run["snapshot"]), args.query, args.limit)
                state["retrieved"] = list(
                    dict.fromkeys(state["retrieved"] + [h["chunk_id"] for h in hits])
                )[-50:]
                return {
                    "UNTRUSTED_hits": hits,
                    "summary": f"Retrieved {len(hits)} chunks",
                }, "running"
            if decision.name == "read_evidence":
                if args.chunk_id not in state["retrieved"]:
                    raise ToolError("Read denied: chunk was not retrieved in this run")
                if args.chunk_id not in state["evidence"] and len(state["evidence"]) >= 6:
                    raise ToolError("Evidence context limit reached; use the six existing chunks")
                chunks = self.store.chunks(run["snapshot"])
                chunk = next((chunk for chunk in chunks if chunk["id"] == args.chunk_id), None)
                if not chunk:
                    raise ToolError("Chunk is outside the frozen run snapshot")
                state["evidence"][args.chunk_id] = chunk
                return {
                    "UNTRUSTED_evidence": chunk,
                    "summary": "Read " + chunk["source"],
                }, "running"
            if decision.name == "calculate":
                item = {"expression": args.expression, "value": calculate(args.expression)}
                if item not in state["calculations"]:
                    state["calculations"].append(item)
                return item | {"summary": args.expression + " = " + item["value"]}, "running"
            report = args.report
            checks = verify_report(report, state["evidence"], run["request"]["min_sources"])
            state["report"] = report.model_dump() | {"validation": checks}
            next_status = "completed"
            if run["request"]["create_ticket"] and report.status == "supported":
                payload = {
                    "type": "create_local_ticket",
                    "action_key": "ticket:" + run["id"],
                    "run_id": run["id"],
                    "title": report.title,
                    "body": render_report(report.model_dump()),
                    "snapshot": digest(run["snapshot"]),
                }
                state["approval"] = {
                    "payload": payload,
                    "digest": digest(payload),
                    "expires_at": self.store.clock() + 3600,
                }
                next_status = "awaiting_approval"
            return checks | {
                "summary": "Report provenance checked; semantic judgment still requires review"
            }, next_status
        except (ToolError, ValidationError) as error:
            state["tool_errors"] += 1
            return {
                "error": str(error)[:500],
                "summary": "Tool rejected; feedback available for repair",
            }, "running"


class Dispatcher:
    """One process, bounded workers. SQLite remains the durable queue."""

    def __init__(self, store: Store, *, workers: int = 2, factory=provider_for):
        self.store, self.workers = store, workers
        self.engine = Engine(store, factory=factory)
        self.tasks: dict[str, asyncio.Task] = {}

    async def serve(self):
        try:
            while True:
                for run_id in list(self.tasks):
                    task = self.tasks[run_id]
                    if task.done():
                        await task
                        del self.tasks[run_id]
                for _ in range(self.workers):
                    if self.store.deliver_one() is None:
                        break
                available = self.workers - len(self.tasks)
                for run_id in self.store.runnable(max(0, available)):
                    if run_id not in self.tasks:
                        self.tasks[run_id] = asyncio.create_task(self.engine.run(run_id))
                await asyncio.sleep(0.15)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
