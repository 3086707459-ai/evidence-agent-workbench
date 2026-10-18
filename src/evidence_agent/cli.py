import argparse
import asyncio
import json
import uuid
from pathlib import Path

from .api import Settings, create_app
from .contracts import ApprovalRequest, RunRequest
from .engine import Engine
from .store import Store, public_run


def main():
    parser = argparse.ArgumentParser(description="Durable evidence-agent workbench")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "run", "inspect", "approve", "replay", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--db", default=None)
        command.add_argument("--corpus", default=None)
        if name == "serve":
            command.add_argument("--host", default=None)
            command.add_argument("--port", type=int, default=None)
        elif name == "run":
            command.add_argument("goal")
            command.add_argument("--provider", choices=["demo", "openai"], default="demo")
            command.add_argument("--ticket", action="store_true")
            command.add_argument("--max-steps", type=int, default=18)
        elif name in ("inspect", "approve", "replay"):
            command.add_argument("run_id")
            if name == "approve":
                command.add_argument("--version", type=int, required=True)
                command.add_argument("--digest", required=True)
                command.add_argument("--decision", choices=["approve", "reject"], required=True)
        elif name == "evaluate":
            command.add_argument("--output", default="out/evaluation.json")
            command.add_argument("--provider", choices=["demo", "openai"], default="demo")
    args = parser.parse_args()
    settings = Settings.environment()
    settings.database = args.db or settings.database
    settings.corpus = args.corpus or settings.corpus
    if args.command == "serve":
        import uvicorn

        settings.bind = args.host or settings.bind
        settings.port = args.port or settings.port
        settings.validate()
        uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, workers=1)
        return
    if args.command == "evaluate":
        from .evaluation import evaluate

        result = asyncio.run(evaluate(settings.corpus, provider=args.provider))
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["failed"] == 0 else 2)
    store = Store(settings.database)
    if args.command == "run":
        store.ingest(settings.corpus)
        request = RunRequest(
            goal=args.goal,
            provider=args.provider,
            create_ticket=args.ticket,
            max_steps=args.max_steps,
        )
        run = store.create(request, uuid.uuid4().hex)
        asyncio.run(Engine(store).run(run["id"]))
        result = store.get(run["id"])
    elif args.command == "replay":
        run = store.replay(args.run_id, uuid.uuid4().hex)
        asyncio.run(Engine(store).run(run["id"]))
        result = store.get(run["id"])
    elif args.command == "approve":
        request = ApprovalRequest(
            expected_version=args.version, digest=args.digest, decision=args.decision
        )
        store.approve(args.run_id, request, uuid.uuid4().hex)
        store.deliver_one()
        result = store.get(args.run_id)
    else:
        result = store.get(args.run_id)
    print(json.dumps(public_run(result), ensure_ascii=False, indent=2))
    raise SystemExit(2 if result["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
