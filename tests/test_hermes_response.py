import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.live_trader import AIAutonomousTrader, AIBatchDecision, AILiveRepository
from app.ai.service import (
    AIUnavailable, OpenAICompatibleProvider, parse_hermes_response, _hermes_prompt_content,
)
from app.core.config import Settings
from app.db import Base, AILiveScanRecord, ExecutionOrderRecord, ControlledLiveProposalRecord
from app.trading.controlled_universe import SCANNER_CONFIG


def decision(action="WAIT"):
    return {
        "symbol": "SOLUSDT", "action": action, "confidence": 75,
        "stop_loss": None if action == "WAIT" else 98.0 if action == "LONG" else 102.0,
        "take_profit": None if action == "WAIT" else 104.0 if action == "LONG" else 96.0,
        "reason": "deterministic parser fixture, not an execution signal",
    }


def envelope(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


@pytest.mark.parametrize("action", ["LONG", "SHORT", "WAIT"])
@pytest.mark.parametrize("fence", [None, "json", "", "JSON"])
def test_valid_actions_and_single_outer_fence_preserve_values(action, fence):
    value = {"decisions": [decision(action)]}
    text = json.dumps(value)
    if fence is not None:
        text = f"  ```{fence}\n{text}\n```  "
    assert parse_hermes_response(envelope(text), AIBatchDecision) == value


def test_actual_production_response_missing_levels_is_rejected_not_repaired():
    # Captured from Railway Hermes /v1/chat/completions on 2026-09-03.
    path = Path(__file__).parent / "fixtures" / "hermes_production_missing_levels.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    original = copy.deepcopy(payload)
    assert payload["object"] == "chat.completion"
    assert isinstance(payload["choices"][0]["message"]["content"], str)
    with pytest.raises(AIUnavailable):
        parse_hermes_response(payload, AIBatchDecision)
    assert payload == original


def test_actual_production_complete_response_passes_unchanged():
    path = Path(__file__).parent / "fixtures" / "hermes_production_valid_batch.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = json.loads(payload["choices"][0]["message"]["content"])
    parsed = parse_hermes_response(payload, AIBatchDecision)
    assert parsed == raw
    result = AIBatchDecision.model_validate(parsed)
    assert {item.symbol for item in result.decisions} == set(SCANNER_CONFIG.symbols)
    assert len(result.decisions) == 8
    assert all(item.action == "WAIT" for item in result.decisions)
    assert all(item.stop_loss is None and item.take_profit is None for item in result.decisions)


@pytest.mark.parametrize("changes", [
    {"action": "BUY"}, {"action": "long"}, {"confidence": -1}, {"confidence": 101},
    {"confidence": 75.5}, {"symbol": "BTCUSDT"}, {"symbol": "NOT_IN_UNIVERSE"},
    {"action": "LONG", "stop_loss": None}, {"action": "SHORT", "take_profit": None},
    {"unexpected": "field"},
])
def test_invalid_trading_values_are_not_corrected(changes):
    item = decision("LONG") | changes
    with pytest.raises(AIUnavailable):
        parse_hermes_response(envelope(json.dumps({"decisions": [item]})), AIBatchDecision)


@pytest.mark.parametrize("content", [
    'Explanation: {"decisions": []}', '```json\n{"decisions": []}\n```\nExplanation',
    '```json\n{"decisions": []}\n```\n```json\n{}\n```',
    '{"decisions": [],}', '{"decisions": [], "decisions": []}',
    '{"decisions": [], "value": NaN}', '{"decisions": [], "value": Infinity}',
    '[{"decisions": []}]', 'null', '',
])
def test_invalid_json_is_not_salvaged(content):
    with pytest.raises(AIUnavailable):
        parse_hermes_response(envelope(content), AIBatchDecision)


@pytest.mark.parametrize("payload", [
    None, [], {}, {"choices": []}, {"choices": [None]}, envelope(None),
    envelope([{"type": "text", "text": '{"decisions": []}'}]),
    {"output_text": '{"decisions": []}'},
    envelope('{"decisions": []}', "length"),
    envelope('{"decisions": []}', "error"),
    envelope('{"decisions": []}', "tool_calls"),
])
def test_unknown_envelopes_and_truncated_outputs_fail_closed(payload):
    with pytest.raises(AIUnavailable):
        parse_hermes_response(payload, AIBatchDecision)


def test_duplicate_symbols_rejected_and_universe_unchanged():
    with pytest.raises(AIUnavailable):
        parse_hermes_response(
            envelope(json.dumps({"decisions": [decision(), decision()]})), AIBatchDecision,
        )
    assert set(SCANNER_CONFIG.symbols) == {
        "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT",
    }


def test_large_market_payload_survives_actual_hermes_text_part_normalization():
    data = {
        "symbols": [
            {"symbol": symbol, "candles": [["2026-09-03T10:00:00+00:00", "0.1234567890123456789"]] * 350}
            for symbol in SCANNER_CONFIG.symbols
        ],
        "task": 'Analyze every symbol; literal quote " slash \\ and punctuation ,:{}[] are data.',
    }
    prompt = json.dumps(data, separators=(",", ":"))
    assert len(prompt) > 65_536
    parts = _hermes_prompt_content(prompt)
    assert isinstance(parts, list)
    assert len(parts) <= 100
    assert all(part["type"] == "text" and len(part["text"]) <= 32_768 for part in parts)
    assert "".join(part["text"] for part in parts) == prompt
    # Exact behavior observed in Hermes' _normalize_multimodal_content.
    received = "\n".join(part["text"][:65_536] for part in parts[:100])
    assert json.loads(received) == data
    assert [x["symbol"] for x in json.loads(received)["symbols"]] == list(SCANNER_CONFIG.symbols)
    assert json.loads(received)["symbols"][-1]["candles"][-1][1] == "0.1234567890123456789"


@pytest.mark.parametrize("size", [1, 65_536])
def test_small_prompt_unchanged(size):
    prompt = "x" * size
    assert _hermes_prompt_content(prompt) == prompt


@pytest.mark.parametrize("prompt", ["x" * 70_000, json.dumps({"unbroken": "x" * 70_000})],
                         ids=["non-json", "oversized-string-token"])
def test_unsplittable_large_prompt_fails_closed(prompt):
    with pytest.raises(AIUnavailable, match="without truncation"):
        _hermes_prompt_content(prompt)


@pytest.mark.asyncio
async def test_hermes_gets_schema_in_prompt_without_unsupported_response_format(monkeypatch):
    captured = {}
    original_client = httpx.AsyncClient

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope(json.dumps({"decisions": [decision()]})))

    monkeypatch.setattr(
        "app.ai.service.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(ai_api_key="fixture-key", ai_model="hermes-agent",
                        ai_base_url="http://hermes.railway.internal:8642/v1")
    await OpenAICompatibleProvider(settings).complete_json("market fixture", AIBatchDecision)
    assert "response_format" not in captured
    system = captured["messages"][0]["content"]
    assert "Return ONLY valid JSON. No markdown, no code fences, no explanation outside JSON." in system
    assert json.dumps(AIBatchDecision.model_json_schema(), separators=(",", ":")) in system
    assert "EVERY requested symbol" in system
    assert "explicitly as null" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["not json", json.dumps({"decisions": [decision() | {"confidence": 101}]}),
                                   json.dumps({"decisions": [decision() | {"symbol": "BTCUSDT"}]}),
                                   json.dumps({"decisions": [decision()]})])
async def test_bad_or_incomplete_batch_never_reaches_executor_or_ledger(monkeypatch, value):
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(ai_api_key="fixture-key", ai_model="hermes-agent",
                        ai_base_url="http://hermes.railway.internal:8642/v1")
    AILiveRepository(sessions).initialize(settings)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.ai.service.httpx.AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope(value))),
            **kwargs,
        ),
    )
    monkeypatch.setattr("app.ai.live_trader.build_ai_prompt", lambda market, fees: ("fixture", "hash"))
    reader = AsyncMock()
    trader = AIAutonomousTrader(settings, sessions, OpenAICompatibleProvider(settings), reader,
                               AsyncMock(), AsyncMock())
    trader._execute_candidates = AsyncMock()
    trader._fee_rates = AsyncMock(return_value={})
    result = await trader.cycle()
    assert result["status"] == "FAILED"
    trader._execute_candidates.assert_not_awaited()
    reader.fee_rates.assert_not_awaited()
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ExecutionOrderRecord)) == 0
        assert db.scalar(select(func.count()).select_from(ControlledLiveProposalRecord)) == 0
        assert db.scalar(select(AILiveScanRecord.status)) == "FAILED"
    engine.dispose()


@pytest.mark.parametrize("fenced", [
    '```json {json}```', '```{json}```', '~~~json\n{json}\n~~~',
    '```JSON\r\n{json}\r\n```',
])
def test_markdown_wrapper_only_is_removed(fenced):
    value = {"decisions": [decision()]}
    content = fenced.replace("{json}", json.dumps(value))
    assert parse_hermes_response(envelope(content), AIBatchDecision) == value


def _full_batch():
    return {"decisions": [decision() | {"symbol": symbol} for symbol in SCANNER_CONFIG.symbols]}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "not json", '{"decisions":[]}',
    json.dumps({"decisions": [decision("LONG") | {"stop_loss": None}]}),
    json.dumps({"decisions": [decision() | {"confidence": 101}]}),
    json.dumps({"decisions": [decision()]}),
])
async def test_invalid_schema_or_incomplete_universe_has_one_ai_only_retry(monkeypatch, bad):
    requests = []
    original_client = httpx.AsyncClient
    valid = _full_batch()

    def handler(request):
        requests.append(json.loads(request.content))
        content = bad if len(requests) == 1 else "```json\n" + json.dumps(valid) + "\n```"
        return httpx.Response(200, json=envelope(content))

    monkeypatch.setattr("app.ai.service.httpx.AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    settings = Settings(ai_api_key="fixture-key", ai_model="hermes-agent",
                        ai_base_url="http://hermes.railway.internal:8642/v1")
    result = await OpenAICompatibleProvider(settings).complete_json(
        "original closed market data", AIBatchDecision,
        expected_symbols=frozenset(SCANNER_CONFIG.symbols),
    )
    assert result == valid
    assert len(requests) == 2
    assert requests[1]["messages"][:-1] == requests[0]["messages"]
    assert requests[1]["messages"][-1]["content"].startswith("RETURN VALID JSON ONLY")
    assert "not json" not in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_second_invalid_response_is_no_order_without_third_request(monkeypatch):
    requests = []
    original_client = httpx.AsyncClient
    invalid = _full_batch()
    invalid["decisions"][0].update(action="LONG", stop_loss=None, take_profit=None)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(json.dumps(invalid)))

    monkeypatch.setattr("app.ai.service.httpx.AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    settings = Settings(ai_api_key="fixture-key", ai_model="hermes-agent",
                        ai_base_url="http://hermes.railway.internal:8642/v1")
    with pytest.raises(AIUnavailable, match="after one retry; NO ORDER"):
        await OpenAICompatibleProvider(settings).complete_json(
            "market", AIBatchDecision, expected_symbols=frozenset(SCANNER_CONFIG.symbols),
        )
    assert len(requests) == 2
    assert invalid["decisions"][0]["stop_loss"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_non_schema_http_failures_do_not_trigger_schema_retry(monkeypatch, status):
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "private detail must not be logged"})

    monkeypatch.setattr("app.ai.service.httpx.AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    settings = Settings(ai_api_key="fixture-key", ai_model="hermes-agent",
                        ai_base_url="http://hermes.railway.internal:8642/v1")
    with pytest.raises(AIUnavailable) as error:
        await OpenAICompatibleProvider(settings).complete_json("market", AIBatchDecision)
    assert "private detail" not in str(error.value)
    assert len(requests) == 1
