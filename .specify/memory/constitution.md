<!--
Sync Impact Report
- Version change: template -> 1.0.0
- Principles added: Safety Before Trading Capability; Mandatory Spec-Driven Workflow;
  Minimal and Backward-Compatible Changes; Evidence-Based Verification;
  Durable State and Operational Integrity
- Sections added: Trading and Security Constraints; Required Development Workflow and Quality Gates
- Sections removed: none
- Deferred TODOs: none
-->
# Сын Антона Constitution

## Core Principles

### I. Safety Before Trading Capability

Protection of funds and exchange state is NON-NEGOTIABLE. Code MUST preserve deterministic risk
checks, duplicate protection, reconciliation, kill-switch behavior, native stop-loss/take-profit,
fail-closed handling, and secret redaction. AI output MUST remain untrusted input and MUST NOT call an
exchange directly or bypass validation, risk, or execution guards. A development or verification
task MUST NOT create a real order unless the user gives explicit, task-specific authorization after
reviewing the exact financial parameters. Existing positions and protective orders MUST NOT be
changed unless the task explicitly authorizes that action.

### II. Mandatory Spec-Driven Workflow

Every future application change MUST follow **Specify → Plan → Tasks → Implement**.
`$speckit-specify` MUST define behavior, safety boundaries, acceptance criteria, and explicit
non-goals before source changes. `$speckit-plan` MUST identify affected components, compatibility and
rollback concerns, migrations, and verification. `$speckit-tasks` MUST produce ordered,
independently verifiable work. `$speckit-implement` MAY begin only after those artifacts exist and
remain mutually consistent. Clarification, checklist, and analysis steps MUST be added when ambiguity
or risk makes the four required steps insufficient.

### III. Minimal and Backward-Compatible Changes

Implementations MUST make the smallest change that completely satisfies the approved specification.
Existing architecture and verified components MUST be reused before a new engine, profile, service,
table, or abstraction is introduced. Public behavior, Telegram handlers, database history, execution
gateway contracts, deployment roles, and safety gates MUST remain compatible unless an approved
specification explicitly requires a breaking change. Unrelated refactoring, strategy optimization,
parameter changes, and cosmetic rewrites MUST NOT be bundled with functional work. Existing user
changes and dirty-worktree content MUST be preserved.

### IV. Evidence-Based Verification

Every implementation MUST include tests proportional to its risk and MUST demonstrate that prior
behavior still works. Trading, accounting, execution, recovery, persistence, and time-series changes
require deterministic unit and integration tests, including LONG and SHORT paths where applicable.
Tests MUST verify failure behavior, not only success. Before handoff, the established test suite,
lint, migration checks, and a relevant startup smoke check MUST pass. Real exchange mutations MUST
NOT be used as tests when mocks, fixtures, paper execution, testnet, or read-only checks can establish
correctness. Failed or unverified safety checks MUST block deployment.

### V. Durable State and Operational Integrity

Persistent state is authoritative across process, container, and server restarts. Database changes
MUST be additive by default, migration-controlled, idempotent where data import is involved, and
verified for upgrade safety. Existing production history, protocol locks, ledgers, and recovery
markers MUST NOT be silently reset, deleted, or reconstructed. Network timeout and unknown exchange
state MUST fail closed and reconcile before retry. Deployment, Railway variables, credentials,
production flags, and external resources are separate operational changes and MUST require explicit
scope and verification; implementing code does not authorize deployment.

## Trading and Security Constraints

- Secrets MUST live outside Git and MUST NOT appear in logs, specifications, plans, tasks, fixtures,
  reports, or assistant output.
- Withdraw and transfer permissions MUST remain disabled for trading credentials. Any uncertainty
  about a permission MUST be reported rather than inferred.
- Closed-candle and multi-timeframe logic MUST prevent look-ahead. Market-data freshness, missing
  candles, duplicate candles, and exchange-specific instrument limits MUST be validated.
- Quantity, notional, leverage, fees, spread, slippage, risk/reward, and planned loss MUST be
  recalculated deterministically immediately before execution. Strategy or AI claims are not proof.
- Features that can change real behavior MUST default off behind explicit feature flags unless the
  approved specification states otherwise. A disabled feature MUST preserve existing behavior.
- LIVE, testnet, paper, shadow, research, and backtest state MUST remain visibly separated. Results
  from one mode or venue MUST NOT be presented as evidence for another.

## Required Development Workflow and Quality Gates

1. **Specify**: inspect the repository and operational state; document the request, invariants,
   non-goals, safety boundary, and measurable acceptance scenarios without editing application code.
2. **Plan**: identify exact contracts and files, reused components, migration and rollback strategy,
   security implications, production impact, and the test matrix.
3. **Tasks**: split work into ordered units with explicit tests and stopping conditions. Destructive
   or production-mutating operations MUST be separate tasks requiring explicit authorization.
4. **Implement**: make only planned changes, review the diff for scope creep and secrets, and preserve
   all unrelated work.
5. **Verify**: run targeted tests, the full relevant suite, lint, migration validation, and a
   non-mutating startup smoke check. Record exact results.
6. **Release decision**: report blockers, warnings, and readiness. No push, merge, deployment,
   production-variable change, live arming, or real order follows automatically from passing tests.

Any exception MUST be documented in the plan with necessity, bounded risk, rollback, and explicit
user approval. Convenience, speed, or desired PnL are not sufficient exceptions.

## Governance

This Constitution governs all specifications, plans, tasks, implementations, reviews, and release
decisions. When another project document conflicts with it, the safer constitutional requirement
prevails unless the Constitution is formally amended.

Amendments MUST be explicit, reviewed as standalone governance changes, accompanied by a Sync Impact
Report, and versioned semantically: MAJOR for incompatible removal or redefinition of a principle,
MINOR for a new principle or materially expanded obligation, and PATCH for non-semantic clarification.
The ratification date MUST remain unchanged. Each specification and plan MUST include a Constitution
Check; each implementation handoff MUST cite verification evidence and unresolved deviations.

**Version**: 1.0.0 | **Ratified**: 2026-09-05 | **Last Amended**: 2026-09-05
