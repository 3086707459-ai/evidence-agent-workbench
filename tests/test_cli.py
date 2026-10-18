import json
import subprocess
import sys

from conftest import CORPUS


def invoke(*args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "evidence_agent.cli", *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected, result.stderr
    return json.loads(result.stdout)


def test_cli_run_inspect_bound_approval_and_replay(tmp_path):
    database = str(tmp_path / "cli.db")
    run = invoke(
        "run",
        "Compare Atlas and Boreal with source evidence",
        "--ticket",
        "--db",
        database,
        "--corpus",
        str(CORPUS),
    )
    assert run["status"] == "awaiting_approval"
    assert invoke("inspect", run["id"], "--db", database)["approval"] == run["approval"]
    approved = invoke(
        "approve",
        run["id"],
        "--version",
        str(run["version"]),
        "--digest",
        run["approval"]["digest"],
        "--decision",
        "approve",
        "--db",
        database,
    )
    assert approved["status"] == "completed" and approved["ticket_id"]
    replay = invoke("replay", run["id"], "--db", database)
    assert replay["status"] == "completed" and replay["report"] == run["report"]
    assert replay["ticket_id"] is None


def test_cli_evaluation_and_budget_failure_have_machine_readable_results(tmp_path):
    output = tmp_path / "evaluation.json"
    evaluation = invoke("evaluate", "--corpus", str(CORPUS), "--output", str(output))
    assert evaluation["passed"] == 8 and evaluation == json.loads(output.read_text())
    failed = invoke(
        "run",
        "Compare Atlas and Boreal",
        "--max-steps",
        "1",
        "--db",
        str(tmp_path / "failed.db"),
        "--corpus",
        str(CORPUS),
        expected=2,
    )
    assert failed["status"] == "failed" and failed["error"] == "step_limit"
