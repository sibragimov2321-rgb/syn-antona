import asyncio
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import BaseModel

from app.ai.models import AIResult, MarketContext, RiskLevel, RoleResult, Trend
from app.core.config import Settings
from app.domain.models import Decision, Signal


class AIUnavailable(RuntimeError): pass
class AIRateLimited(AIUnavailable): pass
class AIInvalidResponse(AIUnavailable): pass


class AIProvider:
    async def complete(self, prompt: str, schema: type[AIResult]) -> dict: raise NotImplementedError


class MockAIProvider(AIProvider):
    def __init__(self, response: dict | Exception): self.response = response; self.calls = 0
    async def complete(self, prompt: str, schema: type[AIResult]) -> dict:
        self.calls += 1
        if isinstance(self.response, Exception): raise self.response
        return self.response


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _hermes_prompt_content(prompt: str) -> str | list[dict[str, str]]:
    """Preserve large JSON prompts across Hermes' 64-KiB per-text-part cap.

    Hermes accepts standard text content parts and joins them with newlines.
    Split only after JSON punctuation outside strings, where that whitespace
    cannot change any market values. Never truncate or summarize market data.
    """
    if len(prompt) <= 65_536:
        return prompt
    try:
        json.loads(prompt, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        parts = []
        start = boundary = 0
        in_string = escaped = False
        for index, char in enumerate(prompt):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in ",:{}[]":
                boundary = index + 1
            if index + 1 - start >= 32_768:
                if boundary <= start:
                    raise ValueError("JSON token cannot fit in a text part")
                parts.append(prompt[start:boundary])
                start = boundary
        if start < len(prompt):
            parts.append(prompt[start:])
        if len(parts) > 100:
            raise ValueError("Too many text parts")
        return [{"type": "text", "text": part} for part in parts]
    except ValueError as error:
        raise AIUnavailable("Hermes input cannot be transmitted without truncation") from error


def parse_hermes_response(
    payload: object, schema: type[BaseModel], *, expected_symbols: frozenset[str] | None = None,
) -> dict:
    """Parse the observed Chat Completions envelope; never repair trading data."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("Invalid response envelope")
        choice = payload["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("Incomplete or failed completion")
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Content must be text")
        content = content.strip()
        # Remove a single complete outer Markdown fence only. Never extract a
        # JSON-looking substring from prose or repair its trading values.
        fence = re.fullmatch(r"(```|~~~)(?:json)?\s*(.*?)\s*\1", content, re.S | re.I)
        if fence:
            content = fence.group(2)
        result = json.loads(
            content, object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(result, dict):
            raise ValueError("JSON root must be an object")
        schema.model_validate(result)
        if expected_symbols is not None and (
            {item["symbol"] for item in result["decisions"]} != expected_symbols
        ):
            raise ValueError("Incomplete requested symbol universe")
        # Validation is a gate, not a repair: return the original parsed values.
        return result
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
        raise AIInvalidResponse("Hermes response does not match the required JSON schema") from error


class OpenAICompatibleProvider(AIProvider):
    """OpenAI/OpenRouter compatible HTTP client. Output is always parsed as untrusted JSON."""
    def __init__(self, settings: Settings): self.settings = settings
    async def complete(self, prompt: str, schema: type[AIResult]) -> dict:
        return await self.complete_json(prompt, schema)

    async def complete_json(
        self, prompt: str, schema: type[BaseModel], *,
        expected_symbols: frozenset[str] | None = None,
    ) -> dict:
        if not self.settings.ai_api_key: raise AIUnavailable("AI API key is not configured")
        base = (self.settings.ai_base_url or (
            "https://api.openai.com/v1"
            if self.settings.ai_provider == "openai"
            else "https://openrouter.ai/api/v1"
        )).rstrip("/")
        # This exact authenticated endpoint is private to our Railway environment.
        # Keep HTTPS mandatory for every other provider/host (no generic HTTP bypass).
        private_hermes = (
            base == "http://hermes.railway.internal:8642/v1"
            and self.settings.ai_model == "hermes-agent"
        )
        if not base.startswith("https://") and not private_hermes:
            raise AIUnavailable("AI_BASE_URL must use HTTPS or the approved Railway Hermes endpoint")
        headers = {"Authorization": f"Bearer {self.settings.ai_api_key}"}
        body = {
            "model": self.settings.ai_model,
            # OpenRouter otherwise reserves the model's full output window
            # (65k tokens for GPT-5.4) and can reject a small structured
            # request for insufficient credit. Eight compact decisions fit
            # comfortably inside this explicit fail-bounded allowance.
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a market-analysis decision engine. Compare all supplied "
                        "symbols. Return only data matching the JSON schema. Use WAIT when "
                        "there is no defensible setup, but evaluate LONG and SHORT normally."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__.lower(),
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        if private_hermes:
            # The deployed Hermes API ignores response_format (both json_schema
            # and json_object). Pass the schema as text instead of relying on a
            # successful HTTP response to imply structured-output support.
            body.pop("response_format")
            body["messages"][1]["content"] = _hermes_prompt_content(prompt)
            body["messages"][0]["content"] += (
                "\nReturn ONLY valid JSON. No markdown, no code fences, no explanation outside JSON."
                "\nUse only the supplied market snapshot. Do not call tools, browse, execute code,"
                " or delegate. Respond directly in one compact JSON object; keep each reason"
                " to one short sentence. Do not replace analysis with a fabricated decision."
                "\nMatch the complete JSON Schema below. Include EVERY required property,"
                " including nullable properties. Do not add extra fields or guess missing values."
                "\nJSON Schema:\n" + json.dumps(schema.model_json_schema(), separators=(",", ":"))
            )
            if "decisions" in schema.model_json_schema().get("properties", {}):
                body["messages"][0]["content"] += (
                    "\nReturn the top-level decisions array with exactly one decision for EVERY"
                    " requested symbol, not a selected subset or a standalone decision."
                    " WAIT decisions must include stop_loss and take_profit explicitly as null."
                    " LONG/SHORT must include concrete numeric levels from the supplied data."
                )
        try:
            # Real Railway Hermes multi-symbol completions can take ~75s.
            # Bound the response wait separately; keep connection/write/pool
            # timeouts and every downstream fresh-quote/execution gate intact.
            timeout = (
                httpx.Timeout(self.settings.ai_timeout, read=max(120.0, self.settings.ai_timeout))
                if private_hermes else self.settings.ai_timeout
            )
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False
            ) as client:
                # One bounded *AI-only* retry for invalid Hermes output. HTTP,
                # authentication, rate-limit and timeout failures are not retried.
                for attempt in range(2 if private_hermes else 1):
                    response = await client.post(f"{base}/chat/completions", headers=headers, json=body)
                    if response.status_code == 429: raise AIRateLimited("AI rate limited")
                    response.raise_for_status()
                    if not private_hermes:
                        content = response.json()["choices"][0]["message"]["content"]
                        if not isinstance(content, str):
                            raise ValueError("AI response content is not a JSON string")
                        return json.loads(content)
                    try:
                        return parse_hermes_response(
                            response.json(), schema, expected_symbols=expected_symbols,
                        )
                    except (AIInvalidResponse, ValueError) as error:
                        if attempt == 1:
                            raise AIInvalidResponse(
                                "Hermes JSON/schema invalid after one retry; NO ORDER"
                            ) from error
                        # Do not echo the invalid response or infer missing levels.
                        # Retain the same original market data and full schema.
                        body["messages"].append({
                            "role": "user",
                            "content": "RETURN VALID JSON ONLY. Match the supplied schema for every "
                                       "requested symbol. Include all required fields; WAIT levels "
                                       "must be null. Never invent missing trading values.",
                        })
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            reason = {
                400: "invalid provider request",
                401: "authentication failed",
                402: "insufficient AI provider credits",
                403: "provider access forbidden",
                404: "model or endpoint not found",
            }.get(status, "provider request failed")
            raise AIUnavailable(f"AI HTTP {status}: {reason}") from error
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise AIUnavailable("AI provider unavailable or returned invalid data") from error


@dataclass
class AIUsage: requests: int = 0; estimated_cost: Decimal = Decimal(); last_hour: list[float] = None
def default_usage() -> AIUsage: return AIUsage(last_hour=[])


class AIAnalyst:
    prompt_version = "final_v1"
    def __init__(self, provider: AIProvider, settings: Settings, usage: AIUsage | None = None):
        self.provider, self.settings, self.usage, self.cache = provider, settings, usage or default_usage(), {}
    async def analyze(self, context: MarketContext) -> AIResult | None:
        key = context.model_dump_json()
        if key in self.cache: return self.cache[key]
        now = time.time(); self.usage.last_hour = [x for x in self.usage.last_hour if now-x < 3600]
        if len(self.usage.last_hour) >= self.settings.ai_max_requests_per_hour: raise AIRateLimited("Hourly AI request limit")
        for attempt in range(3):
            try:
                raw = await self.provider.complete(context.model_dump_json(), AIResult)
                result = AIResult.model_validate(raw); self._validate_market_prices(result, context)
                self.cache[key] = result; self.usage.requests += 1; self.usage.last_hour.append(now); return result
            except (AIUnavailable, AIRateLimited):
                if attempt == 2:
                    if self.settings.ai_required: return None
                    return AIResult(decision=Decision.WAIT, confidence=0, trend_score=0, momentum_score=0, volatility_score=0, setup_quality=0, reasons=("AI unavailable",))
                await asyncio.sleep(0.05 * (2**attempt))
    @staticmethod
    def _validate_market_prices(result: AIResult, context: MarketContext) -> None:
        if result.decision is Decision.WAIT: return
        if abs(result.suggested_entry-context.current_price) / context.current_price > Decimal("0.03"): raise ValueError("AI entry too distant")
        if abs(result.suggested_entry-result.suggested_stop_loss) > context.atr * Decimal("5"): raise ValueError("AI stop too distant")


class AIConsensusEngine:
    def aggregate(self, technical: Signal, trend: RoleResult, momentum: RoleResult, risk: RoleResult, final: AIResult) -> Signal:
        if risk.risk is RiskLevel.EXTREME: return Signal(technical.symbol, technical.timeframe, Decision.WAIT, 0, 0, 0, 0, ("Extreme AI risk",))
        ai_directions = [Trend.BULLISH if final.decision is Decision.LONG else Trend.BEARISH if final.decision is Decision.SHORT else Trend.NEUTRAL, trend.direction, momentum.direction]
        technical_direction = Trend.BULLISH if technical.decision is Decision.LONG else Trend.BEARISH if technical.decision is Decision.SHORT else Trend.NEUTRAL
        if technical_direction is not Trend.NEUTRAL and sum(item is technical_direction for item in ai_directions) < 2: return Signal(technical.symbol, technical.timeframe, Decision.WAIT, 0, 0, 0, 0, ("AI/technical disagreement",))
        score = int(technical.signal_score*.35 + trend.score*.20 + momentum.score*.15 + final.setup_quality*.20 + risk.score*.10)
        decision = final.decision if score >= 70 and final.decision is technical.decision else Decision.WAIT
        return Signal(technical.symbol, technical.timeframe, decision, score, final.trend_score, final.momentum_score, final.volatility_score, final.reasons, final.suggested_entry, final.suggested_stop_loss, final.suggested_take_profit, Decimal("2"))
