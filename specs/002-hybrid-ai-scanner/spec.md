# Feature Specification: Hybrid AI Scanner

**Branch**: `position-profit-protector`  
**Created**: 2026-09-06  
**Status**: Approved for implementation

## Scope

Keep the existing CORE eight-symbol AI cycle every five minutes, but send one compact Hermes request containing calculated indicators and at most five closed candles per timeframe. Add a separate five-minute GET-only Bybit Linear Perpetual market discovery cycle. It filters every Trading USDT contract locally, calculates technical ranks for a bounded liquid shortlist, selects three candidates, and sends those three in one Hermes request no more than once per thirty minutes unless a deterministic strong-event threshold is crossed.

Market-discovery decisions are diagnostic only in this change. They cannot enter the existing execution flow because changing the production execution/risk allowlist is explicitly out of scope.

## Requirements

- CORE symbols MUST remain exactly ADA, AVAX, DOGE, LINK, NEAR, SOL, SUI, and XRP USDT perpetuals.
- CORE MUST use one Hermes request for all eight symbols every five-minute slot.
- CORE payload MUST retain RSI, EMA, MACD, ATR, 5m/15m/1h trend, volume, volatility, spread, account context, and at most five latest fully closed candles per timeframe.
- Market discovery MUST use only public GET endpoints and ordinary Python calculations before Hermes.
- Market discovery MUST reject stale data, non-Trading/non-Linear/non-USDT contracts, invalid quotes, excessive spread, insufficient turnover, and actual minimum order notional above $15.
- Market ranking MUST deterministically combine turnover, volatility, momentum, RSI, EMA, MACD, ATR, and multi-timeframe trend.
- Only the top three eligible non-CORE candidates MAY be included in a market Hermes request.
- Market Hermes requests MUST be durably limited to one per thirty minutes, except when a new top-three set has a deterministic strong-event score materially above the preceding set.
- Restart MUST NOT reset the thirty-minute gate or daily call counters.
- Telegram/status MUST show CORE cadence/universe, local market count/cadence, top count, and separate UTC-day CORE and market Hermes call counts.
- Profit Protector, trailing, initial/native SL/TP, execution, risk, symbols eligible for real execution, and Bybit mutation code MUST remain unchanged.
- Profit protection MUST make zero Hermes calls. No real test order may be sent.

## Acceptance Criteria

- The CORE serialized payload contains no more than 120 candle rows total (8 × 3 × 5), while indicator values still use the existing full closed-candle history.
- One CORE cycle produces exactly one provider call and exactly eight validated decisions.
- A market scan evaluates every returned instrument/ticker row locally and performs no Hermes call itself.
- Repeated top-three data inside thirty minutes produces no market Hermes call; an eligible scan after thirty minutes produces exactly one call.
- Market decisions cannot reach order preview or execution functions.
- Existing full regression suite passes and real test orders sent equals zero.

## Non-goals

- No strategy, confidence threshold, risk, leverage, position sizing, execution allowlist, Profit Protector, SL/TP, or position-management change.
- No private-key change and no new Railway secret.
- No claim that discovery candidates are approved for real trading.
