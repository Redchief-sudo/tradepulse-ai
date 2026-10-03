# Rev.115: position protection and execution liveness

Rev.115 follows Rev.114 and Rev.113 (options liquidity).

## Findings

- **F1 (high).** An intent left in `risk_approved`, or in `submitted` with no `broker_order_id`, counted as in flight forever. `has_in_flight_intent` then blocked every later order on that asset, protective exits included. A failed pre-submission position re-read left the intent in this state, a crash between `RISK_APPROVED` and acceptance stranded it the same way, and `_recover_inflight_orders` skipped intents without a broker id. Nothing resolved them.
- **F5 (low-medium).** A broker position with no local holding (for example when the lot stage failed) was skipped by the monitor with no per-position signal, so it had no stop and nobody was told.
- **F6 (low).** Monitor comments said a failed lane is never restarted. `_supervised_lane` restarts it with capped backoff. The monitor cadence was also 120 s, so a stalled stop check could go unnoticed for minutes.
- **Plan review: no lanes after a refused activation.** Outside a verification generation, a refused activation returned before the supervisor started, so no lane (monitor and settlement included) ran at all.
- **Plan review: no reconciliation in plain `run`.** Only the soak supervisor ran a reconcile lane, so a plain `run` never swept stranded intents.
- **Scan-lane idle rule.** Scan lanes had no way to wait for a session to become tradable without a restart.

## Changes

### Pre-submission rejection (F1)

`_reject_before_submission` closes the intent as rejected instead of stranding it. `BROKER_POSITIONS_UNAVAILABLE` and `BROKER_EXIT_QUANTITY_CHANGED` now use it. `risk_snapshot.broker_account_number` is recorded at approval so a later sweep can prove which account the intent was approved for.

### Stranded-intent sweep (F1)

`_recover_stranded_intents` runs in reconciliation, before `_recover_inflight_orders`. An intent is resolved only when all five finality conditions hold:

1. The per-asset execution reservation is held, renewed or fenced, and re-verified together with the parent reconcile lease inside the commit.
2. The intent is re-read.
3. An exact `client_order_id` lookup at the broker has run. A matching order is adopted; a definitive 404 closes the intent.
4. Neither the intent nor an adopted order is under an integrity hold.
5. The commit is conditional on the full intent payload.

Reasons recorded:

| Reason | Meaning |
|---|---|
| `STRANDED_BEFORE_SUBMISSION` | Definitive 404 for the `client_order_id`; the intent never reached the broker and is closed |
| `ACCOUNT_IDENTITY_UNPROVEN` | The intent's approval account cannot be proven to match the connected account; left for a human |
| `STRANDED_ORDER_IDENTITY_MISMATCH` | A broker order carries the `client_order_id` but different identity or terms; not adopted |
| `STRANDED_INTENT_CHANGED_CONCURRENTLY` | The intent changed between read and commit; the conditional commit refused |
| `STRANDED_INTENT_UNDER_INTEGRITY_HOLD` | The intent or its adopted order is under an integrity hold; untouched |
| `STRANDED_RESERVATION_LOST` | The reservation or parent lease was lost or fenced; nothing committed |

Per-candidate failures are isolated: each is recorded as drift and the pass continues with the next intent. Only `cli._run_reconcile` passes `reconcile_lease`. `reset-integrity` (`session_commands.py`) and the repair tool (`settlement/repair.py`) pass none; the per-asset execution reservation still gives mutual exclusion between concurrent sweeps.

### Unmanaged-position detection (F5)

The monitor's first pass, before any quote fetch, ratchet or exit, finds broker positions with no local holding. It raises a critical alert `UNMANAGED_POSITION` on first sighting, then at most once per asset per UTC day (dedupe key `unmanaged_position:<asset_key>:<UTC date>`); later sightings are counted, not re-alerted. Opening inventory at its opening quantity is excluded. Detection failures are logged (`monitor_opening_inventory_unavailable`, `monitor_unmanaged_detection_failed`) and never block exits. An unreadable opening checkpoint fails closed for alerting only: the position is alerted, never silently skipped.

**Detection latency.** The scheduler waits 30 s after the previous cycle completes and exits are processed sequentially, so detection runs in a dedicated first pass. Worst-case latency is one previous cycle's duration plus 30 s, which the soak bounds at 120 s through the monitor gap limit. Measured cycles: p99 19.7 s, max 22.5 s.

### Lane liveness

- Every lane always starts. Scan lanes idle (no AI call, no market data, no scan record) until the session is `ACTIVE` or `MARKET_CLOSED` with `trading_active`, re-checking every 30 s (`SCAN_IDLE_POLL_SECONDS`), so `tradepulse start` resumes scanning without a restart.
- Plain `run` has a reconcile lane that calls `_run_reconcile`.
- Outside a verification generation, a refused activation still starts the supervisor.
- `run --resume` never re-activates a stopped or latched session.

### One lane-interval authority (F6)

`tradepulse/config/lanes.py` holds `LANE_INTERVAL_SECONDS` and `LANE_MAX_GAP_SECONDS`. `cli.py` schedules each lane from the first, including `VERIFICATION_RECONCILE_INTERVAL_SECONDS`. The soak's `REQUIRED_LANES` is the same object and `_lane_evidence` bounds silence with the second, so runtime cadence and soak continuity cannot drift apart. `SCAN_IDLE_POLL_SECONDS` is not a lane interval and stays in `cli.py`. The market-session broker-clock tolerance in `verification/soak.py` (`2 * REQUIRED_LANES["reconcile"] + 120`) bounds receipts, not lane silence, and is unchanged.

The two stale comments in `monitor/coordinator.py` now describe `_supervised_lane`'s capped-backoff restart.

| Lane | Interval (s) | Maximum gap (s) |
|---|---|---|
| equity | 900 | 1920 |
| crypto | 600 | 1320 |
| option | 1200 | 2520 |
| monitor | 30 (was 120) | 120 (was 360) |
| settle | 60 | 240 |
| reconcile | 60 | 240 |

A gap is one cycle's duration plus the interval. The monitor limit is 30 s plus a 90 s cycle budget: 4x the longest observed cycle (22.5 s across 676 cycles in two soaks) and several 20 s fill waits. Any protection stall over two minutes fails continuity. A cycle with four or more simultaneous slow exits could exceed it; that fails closed and the soak is re-run.

Stop placement, sizing and every risk rule are unchanged. Because protected source changed, both soaks must be run again from Rev.115.

## Validation

Full Python suite passed. New coverage:
- a failed pre-submission position re-read, and a changed exit quantity, close the intent as rejected; the approval account is recorded
- the stranded-intent sweep: adoption of a matching order, definitive-404 close, each reason string above, per-candidate isolation, and lease/reservation fencing
- unmanaged-position alert on first sighting, once per asset per UTC day, opening inventory excluded, detection failures never blocking exits
- every lane starts; scan lanes idle then resume; plain `run` reconciles; a refused activation still starts the supervisor; `run --resume` does not re-activate
- soak lane criteria equal the runtime intervals, and a monitor silence over 120 s breaks continuity
