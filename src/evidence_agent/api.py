"""Single-process FastAPI service, bounded dispatcher and resumable SSE traces."""

import asyncio
import contextlib
import hmac
import ipaddress
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .contracts import (
    TERMINAL,
    ApprovalRequest,
    CancelRequest,
    CapacityExceeded,
    Conflict,
    NotFound,
    RunRequest,
    canonical,
)
from .engine import Dispatcher
from .providers import provider_for
from .store import Store, public_run
from .tools import render_report


@dataclass
class Settings:
    database: str = "data/agent.db"
    corpus: str = "corpus"
    bind: str = "127.0.0.1"
    port: int = 8090
    api_token: str = ""
    approval_token: str = ""

    @classmethod
    def environment(cls):
        return cls(
            database=os.getenv("AGENT_DB", "data/agent.db"),
            corpus=os.getenv("AGENT_CORPUS", "corpus"),
            bind=os.getenv("AGENT_BIND", "127.0.0.1"),
            port=int(os.getenv("AGENT_PORT", "8090")),
            api_token=os.getenv("AGENT_API_TOKEN", ""),
            approval_token=os.getenv("AGENT_APPROVAL_TOKEN", ""),
        )

    def validate(self):
        address = ipaddress.ip_address(self.bind)
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be 1..65535")
        for token in (self.api_token, self.approval_token):
            if token and (
                len(token) < 24
                or len(token) > 256
                or not token.isascii()
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
            ):
                raise ValueError("Tokens must be 24..256 printable ASCII characters without spaces")
        if not address.is_loopback:
            if (
                not self.api_token
                or not self.approval_token
                or self.api_token == self.approval_token
            ):
                raise ValueError("Non-loopback serving requires distinct API and approval tokens")


class BoundaryMiddleware:
    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        protected = path.startswith(("/api/", "/docs", "/redoc", "/openapi.json"))
        headers = dict(scope["headers"])
        token = self.settings.api_token
        if method == "POST" and path.endswith("/approval"):
            token = self.settings.approval_token or token
        if (
            protected
            and token
            and not hmac.compare_digest(
                headers.get(b"authorization", b""), ("Bearer " + token).encode()
            )
        ):
            return await JSONResponse({"detail": "Unauthorized"}, 401)(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'",
                        ),
                    ]
                )
            await send(message)

        # Bound the entire body before FastAPI parses it. A receive-time exception
        # inside a route can otherwise be converted to a generic parsing error.
        if method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 65536:
                    return await JSONResponse({"detail": "Request body exceeds 64 KiB"}, 413)(
                        scope, receive, secure_send
                    )
                if not message.get("more_body", False):
                    break
            supplied = False

            async def buffered_receive():
                nonlocal supplied
                if not supplied:
                    supplied = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, buffered_receive, secure_send)
        await self.app(scope, receive, secure_send)


Key = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    start_dispatcher: bool = True,
    factory=provider_for,
) -> FastAPI:
    settings = settings or Settings.environment()
    settings.validate()
    store = store or Store(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        store.ingest(settings.corpus)
        dispatcher = Dispatcher(store, factory=factory)
        task = asyncio.create_task(dispatcher.serve()) if start_dispatcher else None
        app.state.dispatcher = dispatcher
        app.state.dispatcher_task = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Evidence Agent Workbench", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(BoundaryMiddleware, settings=settings)

    @app.exception_handler(NotFound)
    async def not_found(request, error):
        return JSONResponse({"detail": str(error)}, 404)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(CapacityExceeded)
    async def capacity(request, error):
        return JSONResponse({"detail": str(error)}, 429)

    @app.get("/healthz")
    async def health():
        task = getattr(app.state, "dispatcher_task", None)
        try:
            with store.connection() as db:
                database_ok = db.execute("SELECT count(*) FROM runs").fetchone()[0] >= 0
        except Exception:
            database_ok = False
        healthy = database_ok and (not start_dispatcher or (task is not None and not task.done()))
        return JSONResponse(
            {"healthy": healthy, "database": database_ok, "dispatcher_enabled": start_dispatcher},
            status_code=200 if healthy else 503,
        )

    @app.get("/")
    async def index():
        return FileResponse(str(files("evidence_agent").joinpath("static/index.html")))

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(str(files("evidence_agent").joinpath("static/" + name)))

    @app.get("/api/config")
    async def config():
        return {
            "demo_enabled": True,
            "openai_enabled": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
            "separate_approval_token": bool(settings.approval_token),
            "demo_notice": "Demo is a deterministic fixture; live model quality has not been benchmarked.",
        }

    @app.post("/api/runs", status_code=201)
    async def create(body: RunRequest, idempotency_key: Key):
        if body.provider == "openai" and not (
            os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")
        ):
            raise HTTPException(400, "Server requires OPENAI_API_KEY and OPENAI_MODEL")
        return public_run(store.create(body, idempotency_key))

    @app.get("/api/runs")
    async def run_list():
        return [public_run(run) for run in store.list_runs()]

    @app.get("/api/runs/{run_id}")
    async def get(run_id: str):
        return public_run(store.get(run_id))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, body: CancelRequest):
        return public_run(store.cancel(run_id, body.expected_version))

    @app.post("/api/runs/{run_id}/approval")
    async def approval(run_id: str, body: ApprovalRequest, idempotency_key: Key):
        return public_run(store.approve(run_id, body, idempotency_key))

    @app.post("/api/runs/{run_id}/replay", status_code=201)
    async def replay(run_id: str, idempotency_key: Key):
        return public_run(store.replay(run_id, idempotency_key))

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(0, ge=0)):
        return store.events(run_id, after)

    @app.get("/api/runs/{run_id}/audit")
    async def audit(run_id: str):
        return {
            "chain_valid": store.verify_audit(run_id),
            "trust": "local integrity check, not a signed audit log",
        }

    @app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
    async def report(run_id: str):
        report_data = store.get(run_id)["state"]["report"]
        if not report_data:
            raise HTTPException(409, "Report is not available")
        return render_report(report_data)

    @app.get("/api/runs/{run_id}/events/stream")
    async def stream(run_id: str, request: Request, after: int = Query(0, ge=0)):
        store.get(run_id)

        async def generate():
            cursor, elapsed = after, 0
            while elapsed < 300 and not await request.is_disconnected():
                batch = store.events(run_id, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: trace\ndata: {canonical(event)}\n\n"
                if not batch and store.get(run_id)["status"] in TERMINAL:
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
                elapsed += 0.5

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/tickets")
    async def tickets():
        return store.tickets()

    return app
