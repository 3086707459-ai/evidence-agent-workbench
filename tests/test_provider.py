import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from evidence_agent.contracts import RunRequest
from evidence_agent.evaluation import GOAL
from evidence_agent.providers import OpenAIProvider


def response_fixture(**changes):
    return {
        "id": "resp_mock_001",
        "object": "response",
        "created_at": 1700000000,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "function_call",
                "id": "fc_mock_001",
                "call_id": "call_mock_001",
                "name": "propose_plan",
                "arguments": json.dumps({"steps": ["Find evidence", "Read and verify"]}),
                "status": "completed",
            }
        ],
        "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
        **changes,
    }


def test_real_sdk_serializes_native_strict_tools_and_decodes_usage(store):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=response_fixture())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AsyncOpenAI(
                api_key="test-only-not-a-real-key", http_client=http, max_retries=0
            )
            provider = OpenAIProvider(client=client, model="test-model")
            run = store.create(RunRequest(goal=GOAL), "sdk-serialization")
            decision = await provider.decide(run)
            assert decision.name == "propose_plan"
            assert (decision.input_tokens, decision.output_tokens) == (123, 45)
            assert decision.response_id == "resp_mock_001"
            run["state"]["plan"] = ["Already planned"]
            await provider.decide(run)
            await client.close()

    asyncio.run(scenario())
    assert requests[0]["store"] is False and requests[0]["parallel_tool_calls"] is False
    assert requests[0]["tool_choice"] == {"type": "function", "name": "propose_plan"}
    assert requests[1]["tool_choice"] == "required"
    assert all(t["strict"] and t["type"] == "function" for t in requests[0]["tools"])
    assert "UNTRUSTED_read_evidence" in requests[0]["input"][0]["content"]


@pytest.mark.parametrize(
    "changes",
    [{"status": "incomplete"}, {"output": []}, {"output": response_fixture()["output"] * 2}],
)
def test_sdk_protocol_rejects_incomplete_or_non_single_tool_responses(store, changes):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response_fixture(**changes))
            )
        ) as http:
            client = AsyncOpenAI(
                api_key="test-only-not-a-real-key", http_client=http, max_retries=0
            )
            provider = OpenAIProvider(client=client, model="test-model")
            with pytest.raises(ValueError, match="exactly one"):
                await provider.decide(store.create(RunRequest(goal=GOAL), "sdk-invalid"))
            await client.close()

    asyncio.run(scenario())


def test_live_configuration_requires_model_and_key(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        OpenAIProvider()
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProvider(model="test-model")
