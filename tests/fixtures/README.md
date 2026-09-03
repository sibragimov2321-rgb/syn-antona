# Hermes response regression fixtures

Captured on 2026-09-03 from the actual Railway Hermes service using
`/v1/chat/completions`, `model=hermes-agent`, and real read-only market data.
No executor, proposal writer, or order gateway was invoked by these probes.
Only `object`, `model`, and `choices` are retained; message content is unchanged.
These are historical parser fixtures, not executable trading signals.

- `hermes_production_missing_levels.json`: original response with absent
  required nullable levels and an incomplete symbol set. It must remain rejected.
- `hermes_production_valid_batch.json`: response after sending the schema in
  the prompt and losslessly transporting the full market JSON as text parts.

Deployed Hermes source inspection showed that `response_format` is not consumed
by its Chat Completions handler. It also truncates a scalar text message at
65,536 characters. The original market prompt was about 91,000 characters,
putting NEAR and DOGE beyond that boundary. Supported text parts are normalized
individually and joined with newlines. The compatibility adapter splits only
at JSON punctuation outside strings; tests verify exact value preservation.
