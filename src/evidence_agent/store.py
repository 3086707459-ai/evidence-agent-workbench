"""SQLite checkpoints, worker fencing, immutable evidence, audit chain and local outbox."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    TERMINAL,
    ApprovalRequest,
    CapacityExceeded,
    Conflict,
    LeaseLost,
    NotFound,
    RunRequest,
    canonical,
    digest,
)


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    epoch: int


class Store:
    def __init__(self, path: str | Path, *, clock=time.time, max_active: int = 16):
        self.path = str(path)
        self.clock = clock
        self.max_active = max_active
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported database schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                  id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                  content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active_sources(
                  source TEXT PRIMARY KEY, document_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks(
                  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, source TEXT NOT NULL,
                  title TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, request_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                  state_json TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                  version INTEGER NOT NULL, type TEXT NOT NULL, data_json TEXT NOT NULL,
                  timestamp REAL NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                CREATE TABLE IF NOT EXISTS idempotency(
                  key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                  action_key TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets(
                  id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
                  run_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def ingest(self, directory: str | Path) -> dict[str, Any]:
        paths = sorted(Path(directory).glob("*.md"))
        if not paths or len(paths) > 100:
            raise ValueError("Corpus must contain 1..100 Markdown files")
        documents = []
        for path in paths:
            if path.is_symlink() or path.stat().st_size > 50000:
                raise ValueError("Corpus files must be regular, <= 50000 bytes")
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError("Empty corpus document")
            document_id = hashlib.sha256((path.name + "\n" + text).encode()).hexdigest()
            title = text.splitlines()[0].lstrip("# ")[:200]
            # A bounded paragraph chunker, not a hidden external embedding service.
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            chunks = [p[i : i + 1200] for p in paragraphs for i in range(0, len(p), 1200)]
            documents.append((document_id, path.name, title, text, chunks))
        with self.transaction() as db:
            db.execute("DELETE FROM active_sources")
            for doc_id, source, title, text, chunks in documents:
                db.execute(
                    "INSERT OR IGNORE INTO documents VALUES(?,?,?,?)", (doc_id, source, title, text)
                )
                db.execute("INSERT INTO active_sources VALUES(?,?)", (source, doc_id))
                for index, chunk in enumerate(chunks):
                    db.execute(
                        "INSERT OR IGNORE INTO chunks VALUES(?,?,?,?,?,?)",
                        (
                            f"{doc_id}:{index}",
                            doc_id,
                            source,
                            title,
                            chunk,
                            hashlib.sha256(chunk.encode()).hexdigest(),
                        ),
                    )
        return {"documents": len(documents), "snapshot": digest([d[0] for d in documents])}

    @staticmethod
    def _row(row) -> dict[str, Any]:
        if row is None:
            raise NotFound("Run not found")
        result = dict(row)
        for key in ("request", "snapshot", "state"):
            result[key] = json.loads(result.pop(key + "_json"))
        return result

    def get(self, run_id: str) -> dict[str, Any]:
        with self.connection() as db:
            return self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                self._row(row)
                for row in db.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

    def chunks(self, snapshot: list[str]) -> list[dict[str, Any]]:
        if not snapshot:
            return []
        marks = ",".join("?" for _ in snapshot)
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM chunks WHERE document_id IN ({marks}) ORDER BY id", snapshot
                )
            ]

    def create(
        self, request: RunRequest, key: str, *, replay_source: dict | None = None
    ) -> dict[str, Any]:
        request_data = request.model_dump()
        request_hash = digest(
            {"request": request_data, "replay_of": replay_source["id"] if replay_source else None}
        )
        with self.transaction() as db:
            cached = self._cached(db, "create:" + key, request_hash)
            if cached:
                return self._row(
                    db.execute("SELECT * FROM runs WHERE id=?", (cached["id"],)).fetchone()
                )
            count = db.execute(
                "SELECT count(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','rejected')"
            ).fetchone()[0]
            if count >= self.max_active:
                raise CapacityExceeded("Active run capacity reached")
            snapshot = (
                replay_source["snapshot"]
                if replay_source
                else [
                    row[0]
                    for row in db.execute("SELECT document_id FROM active_sources ORDER BY source")
                ]
            )
            if not snapshot:
                raise Conflict("Ingest a corpus before creating a run")
            run_id, now = uuid.uuid4().hex, self.clock()
            state = {
                "plan": [],
                "retrieved": [],
                "evidence": {},
                "calculations": [],
                "receipts": [],
                "pending": None,
                "report": None,
                "approval": None,
                "steps": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "tool_errors": 0,
                "started_at": None,
                "error": None,
                "replay": {
                    "source_run": replay_source["id"],
                    "decisions": [item["decision"] for item in replay_source["state"]["receipts"]],
                }
                if replay_source
                else None,
            }
            db.execute(
                """INSERT INTO runs
                (id,request_json,snapshot_json,state_json,status,version,created_at,updated_at)
                VALUES(?,?,?,?,'queued',1,?,?)""",
                (run_id, canonical(request_data), canonical(snapshot), canonical(state), now, now),
            )
            self._event(
                db,
                run_id,
                1,
                "run.created",
                {"provider": request.provider, "snapshot": digest(snapshot)},
            )
            self._cache(db, "create:" + key, request_hash, {"id": run_id})
            return self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def replay(self, run_id: str, key: str) -> dict[str, Any]:
        original = self.get(run_id)
        if not original["state"]["report"] or original["state"].get("replay"):
            raise Conflict("Replay requires a finished original report")
        request = RunRequest.model_validate(original["request"] | {"create_ticket": False})
        return self.create(request, key, replay_source=original)

    @staticmethod
    def _cached(db, key: str, request_hash: str):
        row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise Conflict("Idempotency key reused with a different request")
        return json.loads(row["response_json"])

    @staticmethod
    def _cache(db, key, request_hash, response):
        db.execute(
            "INSERT INTO idempotency VALUES(?,?,?)", (key, request_hash, canonical(response))
        )

    def _event(self, db, run_id: str, version: int, kind: str, data: dict):
        previous = db.execute(
            "SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        event = {
            "run_id": run_id,
            "version": version,
            "type": kind,
            "data": data,
            "timestamp": self.clock(),
            "previous_hash": previous[0] if previous else "0" * 64,
        }
        db.execute(
            """INSERT INTO events
            (run_id,version,type,data_json,timestamp,previous_hash,hash) VALUES(?,?,?,?,?,?,?)""",
            (
                run_id,
                version,
                kind,
                canonical(data),
                event["timestamp"],
                event["previous_hash"],
                digest(event),
            ),
        )

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.get(run_id)
        with self.connection() as db:
            events = []
            for row in db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            ):
                event = dict(row)
                event["data"] = json.loads(event.pop("data_json"))
                events.append(event)
            return events

    def verify_audit(self, run_id: str) -> bool:
        previous, cursor = "0" * 64, 0
        while events := self.events(run_id, cursor):
            for event in events:
                body = {key: value for key, value in event.items() if key not in ("seq", "hash")}
                if event["previous_hash"] != previous or digest(body) != event["hash"]:
                    return False
                previous, cursor = event["hash"], event["seq"]
        return True

    def runnable(self, limit: int = 4) -> list[str]:
        with self.connection() as db:
            return [
                row[0]
                for row in db.execute(
                    """SELECT id FROM runs WHERE status='queued'
                OR (status='running' AND lease_until<?) ORDER BY created_at LIMIT ?""",
                    (self.clock(), limit),
                )
            ]

    def claim(self, run_id: str, owner: str, ttl: float = 90) -> Lease | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ("queued", "running"):
                return None
            if row["status"] == "running" and row["lease_until"] > self.clock():
                return None
            epoch, version = row["epoch"] + 1, row["version"] + 1
            db.execute(
                """UPDATE runs SET status='running',owner=?,epoch=?,lease_until=?,
                version=?,updated_at=? WHERE id=?""",
                (
                    owner,
                    epoch,
                    self.clock() + ttl,
                    version,
                    self.clock(),
                    run_id,
                ),
            )
            self._event(db, run_id, version, "run.claimed", {"epoch": epoch})
            return Lease(run_id, owner, epoch)

    def renew(self, lease: Lease, ttl: float = 90) -> bool:
        with self.transaction() as db:
            return (
                db.execute(
                    """UPDATE runs SET lease_until=? WHERE id=? AND owner=? AND epoch=?
                AND status='running' AND lease_until>?""",
                    (
                        self.clock() + ttl,
                        lease.run_id,
                        lease.owner,
                        lease.epoch,
                        self.clock(),
                    ),
                ).rowcount
                == 1
            )

    def checkpoint(
        self, lease: Lease, state: dict, kind: str, data: dict, status: str = "running"
    ) -> dict[str, Any]:
        serialized = canonical(state)
        if len(serialized.encode()) > 256000:
            raise ValueError("Run checkpoint exceeds 256 KiB")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (lease.run_id,)).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["lease_until"] <= self.clock()
            ):
                raise LeaseLost("Worker lease lost")
            version = row["version"] + 1
            db.execute(
                "UPDATE runs SET state_json=?,status=?,version=?,updated_at=? WHERE id=?",
                (
                    serialized,
                    status,
                    version,
                    self.clock(),
                    lease.run_id,
                ),
            )
            self._event(db, lease.run_id, version, kind, data)
            return self._row(
                db.execute("SELECT * FROM runs WHERE id=?", (lease.run_id,)).fetchone()
            )

    def release(self, lease: Lease):
        with self.transaction() as db:
            changed = db.execute(
                """UPDATE runs SET status='queued',owner=NULL,lease_until=0,
                version=version+1,updated_at=?
                WHERE id=? AND owner=? AND epoch=? AND status='running'""",
                (
                    self.clock(),
                    lease.run_id,
                    lease.owner,
                    lease.epoch,
                ),
            ).rowcount
            if changed:
                version = db.execute(
                    "SELECT version FROM runs WHERE id=?", (lease.run_id,)
                ).fetchone()[0]
                self._event(db, lease.run_id, version, "run.released", {"epoch": lease.epoch})

    def cancel(self, run_id: str, version: int) -> dict[str, Any]:
        with self.transaction() as db:
            row = self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if row["version"] != version or row["status"] in TERMINAL:
                raise Conflict("Run version or status changed")
            db.execute(
                """UPDATE runs SET status='cancelled',version=version+1,epoch=epoch+1,
                lease_until=0,updated_at=? WHERE id=?""",
                (self.clock(), run_id),
            )
            db.execute(
                "UPDATE outbox SET status='cancelled' WHERE run_id=? AND status='pending'",
                (run_id,),
            )
            self._event(db, run_id, version + 1, "run.cancelled", {})
            return self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def approve(self, run_id: str, request: ApprovalRequest, key: str) -> dict[str, Any]:
        request_hash = digest(request.model_dump())
        cache_key = f"approval:{run_id}:{key}"
        with self.transaction() as db:
            cached = self._cached(db, cache_key, request_hash)
            if cached:
                return self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            run = self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            approval = run["state"].get("approval")
            if run["status"] != "awaiting_approval" or run["version"] != request.expected_version:
                raise Conflict("Approval is stale or run is not awaiting approval")
            if not approval or approval["digest"] != request.digest:
                raise Conflict("Approval payload digest mismatch")
            if approval["expires_at"] <= self.clock():
                raise Conflict("Approval expired; create a new run")
            payload = approval["payload"]
            if digest(payload) != request.digest:
                raise Conflict("Stored action payload integrity failure")
            status = "approved" if request.decision == "approve" else "rejected"
            if status == "approved":
                db.execute(
                    "INSERT INTO outbox VALUES(?,?,?,?,'pending')",
                    (
                        payload["action_key"],
                        run_id,
                        canonical(payload),
                        request.digest,
                    ),
                )
            db.execute(
                "UPDATE runs SET status=?,version=version+1,updated_at=? WHERE id=?",
                (
                    status,
                    self.clock(),
                    run_id,
                ),
            )
            self._event(
                db,
                run_id,
                run["version"] + 1,
                "approval." + request.decision,
                {"digest": request.digest},
            )
            self._cache(db, cache_key, request_hash, {"id": run_id})
            return self._row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def deliver_one(self) -> str | None:
        """Exactly-once *local SQLite* ticket sink. No claim about external services."""
        with self.transaction() as db:
            row = db.execute("""SELECT o.*,r.state_json,r.version FROM outbox o
                JOIN runs r ON r.id=o.run_id WHERE o.status='pending' AND r.status='approved'
                ORDER BY r.created_at LIMIT 1""").fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            if digest(payload) != row["digest"]:
                raise Conflict("Outbox payload integrity failure")
            ticket_id = "ticket-" + row["run_id"]
            db.execute(
                "INSERT OR IGNORE INTO tickets VALUES(?,?,?,?,?)",
                (
                    ticket_id,
                    row["action_key"],
                    row["run_id"],
                    row["payload_json"],
                    self.clock(),
                ),
            )
            db.execute(
                "UPDATE outbox SET status='delivered' WHERE action_key=?", (row["action_key"],)
            )
            state = json.loads(row["state_json"])
            state["ticket_id"] = ticket_id
            db.execute(
                "UPDATE runs SET status='completed',state_json=?,version=version+1,updated_at=? WHERE id=?",
                (
                    canonical(state),
                    self.clock(),
                    row["run_id"],
                ),
            )
            self._event(
                db, row["run_id"], row["version"] + 1, "effect.delivered", {"ticket_id": ticket_id}
            )
            return ticket_id

    def tickets(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(row) | {"payload": json.loads(row["payload_json"])}
                for row in db.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 100")
            ]


def public_run(run: dict) -> dict:
    state = run["state"]
    return {
        "id": run["id"],
        "goal": run["request"]["goal"],
        "provider": "replay" if state.get("replay") else run["request"]["provider"],
        "status": run["status"],
        "version": run["version"],
        "created_at": run["created_at"],
        "snapshot": digest(run["snapshot"]),
        "steps": state["steps"],
        "usage": {"input_tokens": state["input_tokens"], "output_tokens": state["output_tokens"]},
        "plan": state["plan"],
        "report": state["report"],
        "approval": state["approval"],
        "evidence": list(state["evidence"].values()),
        "calculations": state["calculations"],
        "tool_errors": state["tool_errors"],
        "error": state["error"],
        "ticket_id": state.get("ticket_id"),
    }
