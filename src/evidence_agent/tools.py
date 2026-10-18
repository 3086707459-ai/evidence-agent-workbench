"""Bounded retrieval, safe arithmetic and deterministic citation checks."""

import ast
import hashlib
import math
import re
from collections import Counter
from decimal import Decimal, DecimalException, localcontext

from .contracts import Report, ToolError


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(sequence[i : i + 2] for i in range(max(1, len(sequence) - 1)))
    return words


def grams(text: str) -> set[str]:
    text = re.sub(r"\s+", " ", text.casefold())
    return {text[i : i + 3] for i in range(max(0, len(text) - 2))}


def search(chunks: list[dict], query: str, limit: int) -> list[dict]:
    # No embeddings or external search are hidden behind this lexical hybrid.
    chunks = [chunk for chunk in chunks if len(chunk["text"]) >= 40]
    if not chunks:
        return []
    query_tokens, query_grams = set(tokens(query)), grams(query)
    counts = [Counter(tokens(chunk["text"])) for chunk in chunks]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, len(lengths)) or 1
    frequencies = Counter(token for count in counts for token in count)
    lexical, character = [], []
    for index, chunk in enumerate(chunks):
        score = 0.0
        for token in query_tokens:
            frequency = counts[index][token]
            if frequency:
                idf = math.log(
                    1 + (len(chunks) - frequencies[token] + 0.5) / (frequencies[token] + 0.5)
                )
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
                )
        if score:
            lexical.append((score, index))
        document_grams = grams(chunk["text"])
        similarity = len(query_grams & document_grams) / max(1, len(query_grams | document_grams))
        if similarity >= 0.015:
            character.append((similarity, index))
    fused: Counter = Counter()
    for ranking in (lexical, character):
        for rank, (_, index) in enumerate(sorted(ranking, reverse=True), 1):
            fused[index] += 1 / (60 + rank)
    return [
        {
            "chunk_id": chunks[index]["id"],
            "source": chunks[index]["source"],
            "title": chunks[index]["title"],
            "snippet": chunks[index]["text"][:180],
            "rrf_score": round(score, 6),
        }
        for index, score in fused.most_common(limit)
    ]


def calculate(expression: str) -> str:
    if len(expression) > 120:
        raise ToolError("Expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
        if len(list(ast.walk(tree))) > 32:
            raise ToolError("Expression too complex")

        def visit(node):
            if isinstance(node, ast.Expression):
                result = visit(node.body)
            elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
                result = Decimal(str(node.value))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                result = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
            ):
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Add):
                    result = left + right
                elif isinstance(node.op, ast.Sub):
                    result = left - right
                elif isinstance(node.op, ast.Mult):
                    result = left * right
                else:
                    result = left / right
            else:
                raise ToolError("Only decimal arithmetic + - * / is permitted")
            if not result.is_finite() or abs(result) > Decimal("1e12"):
                raise ToolError("Arithmetic result is out of bounds")
            return result

        with localcontext() as context:
            context.prec = 24
            result = visit(tree)
            return (
                format(result, "f").rstrip("0").rstrip(".")
                if "." in format(result, "f")
                else str(result)
            )
    except (SyntaxError, ValueError, DecimalException, RecursionError) as error:
        raise ToolError("Invalid or undefined arithmetic expression") from error


def verify_report(report: Report, evidence: dict, min_sources: int) -> dict:
    if report.status == "insufficient_evidence":
        if report.claims:
            raise ToolError("An insufficient-evidence report must not assert supported claims")
        return {"citation_count": 0, "source_count": 0, "check": "abstained"}
    if not report.claims:
        raise ToolError("A supported report needs evidence-backed claims")
    sources, citation_count = set(), 0
    for claim in report.claims:
        for citation in claim.citations:
            chunk = evidence.get(citation.chunk_id)
            if not chunk:
                raise ToolError("Citation must refer to evidence read in this run")
            if citation.quote not in chunk["text"]:
                raise ToolError("Citation quote is not a verbatim substring of the evidence")
            if hashlib.sha256(chunk["text"].encode()).hexdigest() != chunk["sha256"]:
                raise ToolError("Evidence integrity mismatch")
            sources.add(chunk["document_id"])
            citation_count += 1
    if len(sources) < min_sources:
        raise ToolError(f"Need at least {min_sources} distinct source documents")
    return {
        "citation_count": citation_count,
        "source_count": len(sources),
        "check": "provenance_passed",
    }


def render_report(report: dict) -> str:
    lines = [
        "# " + report["title"],
        "",
        "Status: " + report["status"],
        "",
        report["recommendation"],
        "",
    ]
    for index, claim in enumerate(report["claims"], 1):
        lines += [f"## Claim {index}", claim["text"]]
        for citation in claim["citations"]:
            lines += [f"> {citation['quote']}", "Source chunk: " + citation["chunk_id"]]
        lines.append("")
    lines += ["## Limitations", *["- " + item for item in report["limitations"]]]
    return "\n".join(lines) + "\n"
