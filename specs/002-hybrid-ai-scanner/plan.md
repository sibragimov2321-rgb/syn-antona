# Implementation Plan: Hybrid AI Scanner

## Design

1. Reuse `AIMarketDataReader`, `_frame_payload`, indicator helpers, the GET-only Bybit client, `OpenAICompatibleProvider`, PostgreSQL session factory, worker task lifecycle, and Telegram AI status.
2. Reduce only CORE transport payload candle rows to five and add an explicit deterministic trend value. Indicator warm-up and execution validation remain unchanged.
3. Add `app/ai/market_discovery.py` for paginated public universe reads, coarse all-market filtering, bounded technical enrichment, deterministic ranking, compact top-three prompting, and a discovery-only schema.
4. Add one additive PostgreSQL runtime table and migration for local scan state, top candidates, last Hermes call/signature, and UTC-day count.
5. Run discovery as an independent worker task every five minutes. Its provider output is persisted/validated but never passed to `AIAutonomousExecutionService`.
6. Extend existing AI status output with read-only aggregate fields.

## Fixed Policies

- Local cadence: 300 seconds.
- Market Hermes minimum interval: 1,800 seconds.
- Candidate count: 3.
- Compact candles: 5 per timeframe.
- Deep technical shortlist before ranking: top 30 eligible contracts by turnover.
- Strong event: changed top-three signature and aggregate normalized technical score at least 20% above the last Hermes-sent score; no more than one early call per five-minute slot.
- Minimum turnover: $5,000,000/24h; maximum spread: 0.20%; maximum actual minimum order: $15.

## Constitution Check

- Safety: PASS — market output is discovery-only and cannot mutate Bybit.
- Spec workflow: PASS — specification, plan, tasks precede source changes.
- Minimal compatibility: PASS — existing reader/provider/worker/status are extended; no engine replacement.
- Verification: PASS — deterministic pagination/filter/rank/cadence/payload/status tests plus full regression.
- Durable operations: PASS — additive migration; restart-safe rate gate and counters.

## Rollback

Stop the discovery task while retaining its additive table. CORE compact serialization is independently reversible. No trading or exchange state requires rollback.
