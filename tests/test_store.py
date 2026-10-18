import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from evidence_agent.contracts import (
    ApprovalRequest,
    CapacityExceeded,
    Conflict,
    LeaseLost,
    RunRequest,
)
from evidence_agent.engine import Engine
from evidence_agent.evaluation import GOAL
from evidence_agent.store import Store


def finished(store):
    run = store.create(RunRequest(goal=GOAL, create_ticket=True), "finished-task")
    asyncio.run(Engine(store).run(run["id"]))
    return store.get(run["id"])


def approval_for(run, **changes):
    return ApprovalRequest(
        **(
            {
                "expected_version": run["version"],
                "digest": run["state"]["approval"]["digest"],
                "decision": "approve",
            }
            | changes
        )
    )


def test_concurrent_creation_is_idempotent(store):
    request = RunRequest(goal=GOAL)
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(lambda _: store.create(request, "same-request")["id"], range(12)))
    assert len(set(ids)) == 1 and len(store.list_runs()) == 1
    with pytest.raises(Conflict):
        store.create(RunRequest(goal="A different request with the same key"), "same-request")


def test_capacity_is_global_and_cancel_releases_slot(store):
    store.max_active = 1
    first = store.create(RunRequest(goal=GOAL), "capacity-one")
    with pytest.raises(CapacityExceeded):
        store.create(RunRequest(goal=GOAL), "capacity-two")
    with pytest.raises(Conflict):
        store.cancel(first["id"], first["version"] + 1)
    store.cancel(first["id"], first["version"])
    assert store.create(RunRequest(goal=GOAL), "capacity-two")["status"] == "queued"


def test_expired_worker_is_fenced_after_reclaim(store, clock):
    run = store.create(RunRequest(goal=GOAL), "fence-request")
    old = store.claim(run["id"], "worker-a", 1)
    assert old and store.claim(run["id"], "worker-b", 1) is None
    clock.advance(2)
    new = store.claim(run["id"], "worker-b", 1)
    assert new and new.epoch > old.epoch and not store.renew(old)
    with pytest.raises(LeaseLost):
        store.checkpoint(old, store.get(run["id"])["state"], "bad.stale", {})
    store.checkpoint(new, store.get(run["id"])["state"], "good.current", {})


def test_approval_digest_version_expiry_and_duplicate_delivery(store, clock):
    run = finished(store)
    assert store.deliver_one() is None and store.tickets() == []
    for request in [approval_for(run, digest="0" * 64), approval_for(run, expected_version=1)]:
        with pytest.raises(Conflict):
            store.approve(run["id"], request, "wrong-approval")
    request = approval_for(run)
    approved = store.approve(run["id"], request, "approve-once")
    assert approved["status"] == "approved"
    assert store.approve(run["id"], request, "approve-once")["status"] == "approved"
    with pytest.raises(Conflict):
        store.approve(run["id"], approval_for(run, decision="reject"), "approve-once")
    # Restart the local sink after approval, then race delivery attempts.
    restarted = Store(store.path, clock=clock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        deliveries = list(pool.map(lambda _: restarted.deliver_one(), range(8)))
    assert sum(value is not None for value in deliveries) == 1
    assert len(restarted.tickets()) == 1
    assert restarted.get(run["id"])["status"] == "completed"
    assert restarted.verify_audit(run["id"])


@pytest.mark.parametrize("decision", ["reject", "expire", "cancel"])
def test_no_effect_after_rejection_expiry_or_cancel(store, clock, decision):
    run = finished(store)
    if decision == "expire":
        clock.advance(3601)
        with pytest.raises(Conflict):
            store.approve(run["id"], approval_for(run), "expired-request")
    elif decision == "reject":
        store.approve(run["id"], approval_for(run, decision="reject"), "reject-request")
    else:
        run = store.approve(run["id"], approval_for(run), "approve-for-cancel")
        store.cancel(run["id"], run["version"])
    assert store.deliver_one() is None and store.tickets() == []


def test_snapshots_are_immutable_when_corpus_changes(store, tmp_path):
    old = store.create(RunRequest(goal=GOAL), "old-snapshot")
    old_chunks = store.chunks(old["snapshot"])
    corpus = tmp_path / "new-corpus"
    corpus.mkdir()
    (corpus / "new.md").write_text(
        "# New version\n\nAll corpus content changed after the old run was created."
    )
    store.ingest(corpus)
    new = store.create(RunRequest(goal=GOAL), "new-snapshot")
    assert new["snapshot"] != old["snapshot"]
    assert store.chunks(old["snapshot"]) == old_chunks


def test_corpus_admission_limits(store, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        store.ingest(empty)
    (empty / "large.md").write_text("x" * 50001)
    with pytest.raises(ValueError):
        store.ingest(empty)
    (empty / "large.md").unlink()
    (empty / "link.md").symlink_to(tmp_path / "agent.db")
    with pytest.raises(ValueError):
        store.ingest(empty)


def test_audit_chain_detects_database_tampering(store):
    run = store.create(RunRequest(goal=GOAL), "audit-task")
    assert store.verify_audit(run["id"])
    with store.transaction() as db:
        db.execute("UPDATE events SET data_json='{}' WHERE run_id=?", (run["id"],))
    assert not store.verify_audit(run["id"])


def test_graceful_release_is_versioned_and_old_worker_cannot_release_new_lease(store):
    run = store.create(RunRequest(goal=GOAL), "release-task")
    old = store.claim(run["id"], "old-worker")
    before = store.get(run["id"])
    store.release(old)
    released = store.get(run["id"])
    assert released["status"] == "queued" and released["version"] == before["version"] + 1
    assert store.events(run["id"])[-1]["type"] == "run.released"
    new = store.claim(run["id"], "new-worker")
    store.release(old)
    assert store.get(run["id"])["status"] == "running" and store.renew(new)
    assert store.verify_audit(run["id"])
