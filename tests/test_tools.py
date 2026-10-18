import copy

import pytest

from evidence_agent.contracts import Citation, Claim, Report, ToolError, tool_schemas
from evidence_agent.tools import calculate, search, verify_report


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1200+3*240", "1920"),
        ("1500+3*120", "1860"),
        ("0.1+0.2", "0.3"),
        ("(12-2)/4", "2.5"),
        ("-3 + +2", "-1"),
        ("0.00", "0"),
        ("1/8", "0.125"),
    ],
)
def test_arithmetic(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "2**1000000",
        "True+1",
        "x+1",
        "(1).__class__",
        "1/0",
        "1e999",
        "1e13",
        "2//1",
        "[1][0]",
        "0+" * 20 + "1",
        "x" * 121,
    ],
)
def test_arithmetic_rejects_code_and_resource_abuse(expression):
    with pytest.raises(ToolError):
        calculate(expression)


def test_retrieval_is_bounded_and_fuses_real_corpus(store):
    with store.connection() as db:
        snapshot = [row[0] for row in db.execute("SELECT document_id FROM active_sources")]
    chunks = store.chunks(snapshot)
    hits = search(chunks, "Atlas purchase price annual support", 3)
    assert 1 <= len(hits) <= 3
    assert any("atlas" in hit["source"] for hit in hits)
    assert all(hit["rrf_score"] > 0 for hit in hits)
    assert search(chunks, "zzunavailable9999", 3) == []
    assert search([], "Atlas", 3) == []


def test_strict_tool_schemas_are_closed_and_all_fields_required():
    def check(schema):
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                assert schema["additionalProperties"] is False
                assert set(schema["required"]) == set(schema.get("properties", {}))
            for value in schema.values():
                check(value)
        elif isinstance(schema, list):
            for value in schema:
                check(value)

    for tool in tool_schemas():
        assert tool["strict"] is True
        check(tool["parameters"])


def test_verifier_checks_read_quotes_hashes_and_source_diversity(store):
    with store.connection() as db:
        snapshot = [row[0] for row in db.execute("SELECT document_id FROM active_sources")]
    chunk = next(c for c in store.chunks(snapshot) if "purchase price" in c["text"])
    evidence = {chunk["id"]: chunk}
    report = Report(
        title="Supplier review",
        status="supported",
        recommendation="Review before purchasing",
        claims=[
            Claim(
                text="The source states a supplier price.",
                citations=[
                    Citation(chunk_id=chunk["id"], quote=chunk["text"].split(". ")[0] + ".")
                ],
            )
        ],
        limitations=["Fictional fixture"],
    )
    assert verify_report(report, evidence, 1)["citation_count"] == 1
    for contents, sources in [({}, 1), (evidence, 2)]:
        with pytest.raises(ToolError):
            verify_report(report, contents, sources)
    bad = report.model_copy(deep=True)
    bad.claims[0].citations[0].quote = "A fabricated quotation is not valid."
    with pytest.raises(ToolError):
        verify_report(bad, evidence, 1)
    bad_evidence = copy.deepcopy(evidence)
    bad_evidence[chunk["id"]]["text"] += "tampered"
    with pytest.raises(ToolError):
        verify_report(report, bad_evidence, 1)
    report.status = "insufficient_evidence"
    with pytest.raises(ToolError):
        verify_report(report, evidence, 1)
    report.claims = []
    assert verify_report(report, {}, 2)["check"] == "abstained"
