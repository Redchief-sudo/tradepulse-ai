# Fresh end-to-end audit (Rev.114) and the 9/10 target

Audited 2026-10-01 against branch `rev113-options-liquidity` (Rev.114, `468a5ab2`). Ratings come from reading the code, not from earlier conclusions.

## Findings

| # | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | High | An intent left in `risk_approved`, or in `submitted` with no `broker_order_id`, counts as in flight forever. `has_in_flight_intent` then blocks every later order on that asset, protective exits included. Nothing resolves such intents. | `execution/gateway.py:463-472` returns `skipped` after persisting `RISK_APPROVED`. A crash between `RISK_APPROVED` and acceptance strands the intent the same way. `_recover_inflight_orders` skips intents without a broker id. |
| F2 | Medium-high | The dashboard control endpoints have no Origin or Host check. Any web page in the operator's browser can POST to `127.0.0.1` (cross-site request forgery, or DNS rebinding), e.g. `reset-risk` then `start`. | `web/app.py:139-170`; no middleware. |
| F3 | Medium | Protection is software-only: stops are evaluated every 120 s while the process runs, and not at all while it is down. | `cli.py:156`; no broker-side stop orders. |
| F4 | Medium | `max_position_pct` caps each new order, not the position. Repeated buys can concentrate one asset up to the sector cap. | `risk/engine.py`; `max_position_notional` ignores the held position. |
| F5 | Low-medium | A broker position with no local holding (e.g. the lot stage failed) is skipped by the monitor with no per-position signal. | `monitor/coordinator.py:284-285`. |
| F6 | Low | Monitor comments say failed lanes are never restarted; `_supervised_lane` restarts them with backoff. | `monitor/coordinator.py:150-157,171-175` vs `cli.py:680`. |
| F7 | Low | Two tests are timing-sensitive under machine load. | The portfolio-lock test depends on real interleaving; the backlog test makes 1,100 concurrent SQLite writes. |
| F8 | Medium (operations) | A soak can be opened on a day with pre-generation trades or a pending end-of-day fee batch. Those fees later classify as unresolved membership and latch the integrity block. | Observed broker behaviour: fee rows post at end of day with midnight ids (see Rev.111). |

## Target: every area at 9/10

| Area | Now | Target | Closed by |
|---|---|---|---|
| Accounting core | 9 | 9 | (no change) |
| Reconciliation and integrity latches | 9 | 9 | F1 sweep adds coverage |
| Persistence | 9 | 9 | (no change) |
| Order execution | 7.5 | 9 | F1 |
| Session control and kill switches | 8.5 | 9 | F2 |
| Risk engine | 8 | 9 | F4 |
| Broker and market-data parsing | 8 | 9 | F8 preflight uses the activity feed |
| Verification and soak harness | 8 | 9 | F8 |
| Scanner and strategy | 7 | 9 | F4 bounds scale-ins; Rev.113 liquidity. Trade rate is calibration, out of scope |
| Position monitor and closing | 6 | 9 | F1, F5, F6, cadence; F3 needs Phase 4 or an always-on host |
| Dashboard and control-plane security | 5 | 9 | F2 |
| Code and docs consistency | 7 | 9 | F6, F7 |
| Operations | 6 | 9 | F8, service unit and runbook; F3 through Phase 4 |

## Constraints

- While a soak runs from `~/tradepulse-ai`, never edit protected source there. Work in a worktree and merge after the soak ends.
- Fail closed everywhere. No change may weaken a kill switch, an integrity latch, or the live-trading gate.
- Every revision ships with a `docs/revNNN-*.md` note, and the full suite must pass.
