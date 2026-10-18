"""Reproducible structural/engineering checks; never a semantic leaderboard score."""

import tempfile
from pathlib import Path

from .contracts import ApprovalRequest, Conflict, RunRequest, digest
from .engine import Engine
from .store import Store
from .tools import calculate, verify_report

GOAL = "比较 Atlas 与 Boreal 网关的离线部署、审计和三年成本，引用原文并列出待确认项。"


async def evaluate(corpus: str, *, provider: str = "demo") -> dict:
    results = []

    def record(name, check):
        try:
            detail = check()
            results.append({"name": name, "passed": True, "detail": detail})
        except Exception as error:
            results.append(
                {
                    "name": name,
                    "passed": False,
                    "detail": type(error).__name__ + ": " + str(error)[:250],
                }
            )

    with tempfile.TemporaryDirectory(prefix="evidence-eval-") as directory:
        store = Store(Path(directory) / "eval.db")
        store.ingest(corpus)
        run = store.create(
            RunRequest(goal=GOAL, provider=provider, create_ticket=True), "comparison-eval"
        )
        await Engine(store).run(run["id"])
        run = store.get(run["id"])

        def report_contract():
            from .contracts import Report

            report = dict(run["state"]["report"] or {})
            report.pop("validation", None)
            checks = verify_report(Report.model_validate(report), run["state"]["evidence"], 2)
            assert checks["source_count"] >= 2
            return checks

        def approval_gate():
            assert run["status"] == "awaiting_approval"
            assert store.deliver_one() is None and not store.tickets()
            return "No ticket exists before approval"

        def digest_binding():
            try:
                store.approve(
                    run["id"],
                    ApprovalRequest(
                        expected_version=run["version"], digest="0" * 64, decision="approve"
                    ),
                    "bad-digest-eval",
                )
            except Conflict:
                return "Changed action payload denied"
            raise AssertionError("Invalid digest accepted")

        def arithmetic():
            assert calculate("1200 + 3 * 240") == "1920"
            assert calculate("1500 + 3 * 120") == "1860"
            assert calculate("0.1 + 0.2") == "0.3"
            return "Decimal arithmetic matches synthetic fixture costs"

        record("verbatim_citations_and_source_diversity", report_contract)
        record("approval_blocks_side_effect", approval_gate)
        record("approval_binds_payload_digest", digest_binding)
        record("bounded_calculator", arithmetic)

        if run["state"]["approval"]:
            approval = run["state"]["approval"]
            request = ApprovalRequest(
                expected_version=run["version"], digest=approval["digest"], decision="approve"
            )

            def deliver_once():
                store.approve(run["id"], request, "approve-eval")
                store.approve(run["id"], request, "approve-eval")
                first = store.deliver_one()
                assert first and store.deliver_one() is None and len(store.tickets()) == 1
                return "One local ticket after duplicate approval and delivery attempts"

            record("local_outbox_idempotency", deliver_once)
            replay = store.replay(run["id"], "replay-eval")
            await Engine(store).run(replay["id"])

            def replay_equivalent():
                replayed = store.get(replay["id"])
                assert replayed["status"] == "completed"
                assert digest(replayed["state"]["report"]) == digest(run["state"]["report"])
                assert replayed["state"]["input_tokens"] == 0 and len(store.tickets()) == 1
                return "Same report on frozen evidence; zero model calls; no replayed side effect"

            record("deterministic_receipt_replay", replay_equivalent)

        def audit_chain():
            assert store.verify_audit(run["id"])
            return "All persisted events form a valid local hash chain"

        record("audit_chain_integrity", audit_chain)
        unknown = store.create(
            RunRequest(
                goal="Missing evidence: report the audited 2035 market share", provider=provider
            ),
            "unknown-eval",
        )
        await Engine(store).run(unknown["id"])

        def abstention():
            state = store.get(unknown["id"])["state"]
            assert state["report"]["status"] == "insufficient_evidence"
            assert not state["report"]["claims"]
            return "Unsupported fixture question produced an explicit abstention"

        record("missing_evidence_abstention", abstention)
    return {
        "category": "structural_and_engineering_evaluation",
        "provider": provider,
        "live_model_tested": provider == "openai",
        "semantic_quality_scored": False,
        "notice": "Demo is rule-based. These checks are not model accuracy, factuality or prompt-injection robustness scores.",
        "passed": sum(row["passed"] for row in results),
        "failed": sum(not row["passed"] for row in results),
        "checks": results,
    }
