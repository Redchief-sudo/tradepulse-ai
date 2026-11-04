# Rev.147 — Verification run wind-down and generation handover

**Status:** design, rev. 3, for forensic review. No production code changes and no orders until the spec is approved.
**Rev. 3 (2026-11-03):** answers the Rev.147 rev. 2 review (REV147-R2-01..04 and the two decisions). Findings map to sections as follows:
- **R2-01, stop preservation:** carried positions keep their full protective state, and the successor never rebuilds a lower stop (§7.1, §7.2.1).
- **R2-02, monitoring continuity:** an exact protection-responsibility rule and an operator-acceptance condition (§7.5). `WIND_DOWN_COMPLETE` no longer implies protection.
- **R2-03, inventory completion:** an explicit four-class completion invariant, plus tracking of every observed order to a terminal state (§5.1).
- **R2-04, late fees:** attribution by activity ID and broker link, duplicate and correction handling, and immutable sealed evidence (§4.5, §7.6). Rev. 2 cited the wrong mechanism, and this revision corrects it (§4.5).
- **Decision 1:** wind-down exits count in financial accounting, but are reported apart from strategy edge (§4.3).
- **Decision 2:** the 60-minute escalation is an alert only (§5).

**Rev. 2 (2026-11-01):**
- distinct, persisted `PAUSED` and `WINDING_DOWN` run states;
- an idempotent `end-run`;
- resume checks;
- reuse of the existing order-recovery and settlement code.

**Builds on:** Rev.140 (sellable inventory, the sell gate), Rev.142 (adoption), Rev.144 (calendar), Rev.146 (DBAE, merged before this Rev is built).
**Operator decisions (2026-11-01):**
- a verification run ends at a planned time;
- before it ends, the system closes everything the next run cannot adopt;
- long equities and options carry over to the next run;
- a crash or power loss must recover safely.

**Reviewer decision (2026-11-01):** keep the original position and exit obligation. Reconcile, and retry only when it is independently safe. Block any next run that cannot account for it. Never force a duplicate sell, and never claim the account is flat without broker proof.
**Reviewer standard (2026-11-03):** a successful wind-down must prove what happened to every position and order, not merely that the shutdown procedure ran.

## 1. Goal

At the end of a verification run, TradePulse either closes every non-adoptable position safely, or records each remaining one as an explicit, reconciled obligation. Either way, the next run can neither adopt nor ignore it by mistake.

Success means four things:
- the run's last state is `WIND_DOWN_COMPLETE`, proven by the inventory invariant of §5.1;
- a sealed **handover** lists every position (with its full protective state), every order and every fee obligation that remains;
- the next generation's freeze and startup refuse unless the broker state matches that handover exactly, and the inherited protection is restored without loss (§7.2.1);
- the handover states the protection interval honestly: when monitoring was released, and whether the successor took it over before the protection could matter (§7.5).

## 2. Scope

**In scope.**
- **Planned end:** a planned end time frozen into the generation.
- **Wind-down state:** a durable wind-down state machine, recovered after a crash or restart.
- **Stopping entries:** at wind-down start, entries stop and the run's open entry orders are cancelled.
- **Closing crypto:** every crypto position the run owns is sold through the existing gateway and the Rev.140 sell gate.
- **Handover:** a sealed handover file with each carried position's protective state, a next-run eligibility check, and a protection-continuity record.
- **Fees after the seal:** attribution of predecessor fees that arrive after the seal.
- **Operator commands:** end a run early, view status, resolve a wind-down that cannot complete, amend a handover between runs, and accept a protection gap.

**Out of scope.**
- **Selling carried positions:** equities and options are never sold by the wind-down. They carry over (operator decision) and keep their normal protective exits until the run ends.
- **Broker-held backup stops:** a separate future Rev.
- **Adopting crypto:** still refused (TP145-02).
- **Covering shorts or quarantined liabilities:** never done automatically (Rev.140).
- **Cloud hosting.**

## 3. The planned end

### 3.1 Frozen end time

- `tradepulse verification freeze` takes a new required argument, `--end-at <ISO time with offset>`.
- The value is stored in the frozen manifest's policy as `"wind_down": {"planned_end_at", "lead_minutes"}`.
- `lead_minutes` is a constant, `WIND_DOWN_LEAD_MINUTES = 30`.
- Both values are part of the generation's identity. Changing either one is `frozen_configuration_mismatch`.
- A generation frozen before Rev.147, with no `wind_down` key, refuses with `generation_predates_wind_down`. This follows Rev.146's `generation_predates_dbae`.

**Freeze refuses** when any of these holds:
- `planned_end_at` is not timezone-aware: `wind_down_end_not_aware`.
- It is not in the future: `wind_down_end_not_future`.
- It falls inside an XNYS session, or within the 15 minutes before an open: `wind_down_end_inside_session`. This is the same rule as `scripts/a0_verification_window.py`, which keeps the valuation snapshot from moving at the boundary. Ending outside equity hours also means no equity or option exit can be pending at the end, and it is the basis of the continuity rule in §7.5.
- The calendar cannot verify it: `wind_down_end_calendar_unverified`.

### 3.2 Phases

| Time | Phase |
|---|---|
| before `planned_end_at − lead` | normal run |
| from `planned_end_at − lead` | wind-down: no new entries; the crypto close runs |
| after `planned_end_at` | the wind-down continues until it reaches a final state (§5). The run never seals in a non-final state |

### 3.3 Stop signals, crashes and the early end

- **SIGINT or SIGTERM** (Ctrl+C, and the `tradepulse-run` service's own `KillSignal=SIGINT`) **pauses the run.** It never starts a wind-down, never cancels an order and never liquidates. This matters because the service uses the same signal for restarts; if a signal triggered liquidation, every restart or deploy would liquidate the run. Approved by the operator and the reviewer on 2026-11-01. On the signal:
  1. The runtime writes a durable `run_paused` event (§3.4) and stops scheduling new scans and new entries.
  2. It lets every in-flight gateway call finish its current step. A submission already sent to the broker is never abandoned halfway: the intent ends in its true state (`SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED`, or `SUBMISSION_UNKNOWN` if the response never arrived). It is never marked cancelled.
  3. It then exits, within the service's existing `TimeoutStopSec`.
- **While paused, positions are exposed.** Stops are held in TradePulse, not at the broker, so the run plan states this, and the operator should keep pauses short. A pause inside a run is not a generation handover; §7.5 applies only between generations.
- **Ending early is explicit:** `tradepulse verification end-run --generation G --reason "<text>"`.
  - It appends a durable `end_run_requested` event with the time, the reason and the OS user.
  - **It is idempotent.** If a request or any `WINDING_DOWN` state already exists for the generation, a repeat appends nothing and only reports the current state. It never causes a second census-to-order pass, because every sell is still gated per asset by §4.3.
  - From that event on, **new entries are permanently disabled for the generation.** No command re-enables them.
  - A running supervisor sees it within one watch cycle (≤ 30 s) and enters wind-down. A paused run enters wind-down on its next start (§6.2).
  - Before any orders, the same window rule as §3.1 is checked against the current time:
    - inside an XNYS session, the request is accepted and the crypto close runs;
    - the seal is held until the session ends, so the valuation boundary rule and §7.5 still hold;
    - the status shows `seal_deferred_until`.
- **After a crash or power loss,** the restarted runtime (`run --resume`) reloads the run state before any lane starts (§6).

### 3.4 Run state

The generation's run state is persisted in the append-only `wind_down_events` table (§6.1), and derived as the latest event on every read. It is separate from the existing session state (`SessionState`), which keeps governing risk latches.

| Run state | Entered by | Entries | Scan lanes | Monitor, settlement, reconciliation | Leaves to |
|---|---|---|---|---|---|
| `RUNNING` | first start | allowed (subject to every existing gate) | run | run | `PAUSED`, `WINDING_DOWN` |
| `PAUSED` | SIGINT/SIGTERM (`run_paused`) | refused (`RUN_PAUSED`) | stopped | stopped (process exited) | `RUNNING` (resume, §6.2), `WINDING_DOWN` |
| `WINDING_DOWN` | `planned_end_at − lead`, or `end_run_requested` | refused permanently (`RUN_WIND_DOWN_ACTIVE`) | never run again | run | sealed (§5, §7) |

- **A crash leaves no event.** The state stays whatever it last was, and `run --resume` handles it exactly like `PAUSED`.
- **`WINDING_DOWN` is terminal for trading.** No event, restart or command returns the generation to `RUNNING`. Section 5's wind-down states (`REQUESTED` through `UNRESOLVED`) are sub-states of `WINDING_DOWN`.
- **The gateway's entry refusal** reads the run state inside the fenced `RISK_APPROVED` transaction (§8). `PAUSED` gives `RUN_PAUSED`, and `WINDING_DOWN` gives `RUN_WIND_DOWN_ACTIVE`.

## 4. What the wind-down does

### 4.1 Census

Before any order, the wind-down takes a **census**. The pattern is the opening checkpoint's capture: activities, then account, positions and open orders, then activities again. A change in between invalidates the census, which is retried. The census records:
- every broker position: identity, quantity, average entry price, `received_at`;
- every broker open order, taken from the broker's open-order list and never from local intents: client order ID, broker order ID, side, quantity, status;
- every local open intent, holding and lot, with each holding's protective state (§7.1);
- the Rev.140 sellable decision for each crypto asset.

**Observed-order register.** Every broker order ID seen in any census, and every order ID of a local intent that was non-terminal at wind-down start, enters an append-only register in `wind_down_events`. An order leaves the register only when the broker reports a terminal state for that ID (`get_order` by broker or client order ID), so an order cannot drop out of the record just because it is no longer in the open-order list. An order that is missing from the open list and has no fetched terminal state keeps the wind-down `BLOCKED` (`wind_down_order_unaccounted`).

### 4.2 Classification

Each broker position falls into exactly one class. A position that fits none of the first three rules is `unproven` by default, so every position has a class.

| Class | Rule | Wind-down action |
|---|---|---|
| `carry_adoptable` | long equity or option, fully managed by this run (holding and lots match the broker quantity), with a valid protective state: `stop_loss` present and, if `current_stop` is set, `current_stop ≥ stop_loss` | none; protective exits stay active until the end; recorded in the handover with the full protective state |
| `close_crypto` | long crypto, fully managed by this run, Rev.140 sellable = broker quantity | sell the sellable quantity through the gateway (§4.3) |
| `crypto_residual` | long crypto managed by this run, quantity > 0 but below the asset's `min_order_size`, or not a multiple of `min_trade_increment` after a sell, and its local lots' remaining quantity equals the broker quantity | none (unsellable); recorded as a residual |
| `unproven` | everything else, including: partially managed; quarantined; a short or a Rev.140 liability; unmanaged crypto; an adopted asset with an `ADOPTED_EXIT_REFUSED` outstanding; a managed equity or option with a missing or invalid protective state (`carried_stop_missing`, `carried_stop_invalid`); residual dust whose local lots disagree with the broker | none, ever; a critical alert; recorded; makes the wind-down `UNRESOLVED` and blocks the next run until resolved (§7.3) |

### 4.3 The crypto close

- **Request:** each `close_crypto` asset gets one SELL `ExecutionRequest`, with `exit_reason = "run_wind_down"`, quantity equal to Rev.140's sellable quantity, a market order and `gtc` (crypto).
- **Path:** the request goes through the existing gateway as a protective exit. It therefore passes the Rev.140 sell reservation, the existing client-order-ID idempotency and the account-qualified identities. DBAE is never called, because protective exits bypass it (Rev.146 §9).
- **One sell at a time per asset.** No new sell is built while that asset has a sell intent in `RISK_APPROVED`, `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED` or `SUBMISSION_UNKNOWN`.
- **After a partial fill,** the remainder is re-sized from a fresh census and sold only after the first order is terminal.
- **Accounting (decision 1, 2026-11-03):**
  - **Financial accounting:** wind-down fills and their fees are recorded in full. They close the run's own lots, keep their strategy attribution, and count in the run's realized P&L, cash and equity.
  - **Strategy edge:** they are excluded from the strategy's normal entry-and-exit edge measurements, such as `prove_edge`, win rate and R-multiple distributions, because a forced end-of-window exit is not the strategy's own exit decision.
  - **Reporting:** they are reported in their own `wind_down_excluded` section, built like Rev.142's `adopted_excluded` section in `verification/evidence.py`: realized P&L, cash fees, fill count, and per-strategy attribution.
  - **Totals:** the run's total realized P&L is the sum of the edge-eligible, `adopted_excluded` and `wind_down_excluded` sections, so no money is left out of the totals.
  - **Classification key:** `exit_reason = "run_wind_down"`. No other exit is classed `wind_down_excluded`, and protective exits that fire during the lead window keep their own reasons and stay edge-eligible.

### 4.4 Open orders

- **At wind-down start,** the run's own open BUY orders are cancelled through `cancel_order`. These are orders with an intent in this database whose client order ID matches the account-qualified scheme.
- **A cancel is not proof.** The order counts as closed only when the broker reports a terminal state (`canceled`, `expired`, `rejected` or `filled`) for its ID (§4.1 register). A fill that arrives during the cancel is settled normally. A resulting crypto position is then classified again (§4.2), and a resulting equity position becomes `carry_adoptable` if its protective state is valid.
- **Protective SELL orders in flight are never cancelled.** They are reconciled to a terminal state.
- **An open order that isn't this run's** (no matching intent) is never cancelled. It gets a critical alert and is recorded as `foreign_open_order`, which makes the wind-down `UNRESOLVED` and blocks the next run (§7.3). A foreign order is taken from the broker's open-order list, so the absence of a local intent cannot hide it.

### 4.5 Fee obligations

Alpaca posts FEE and CFEE activities dated at midnight, after the fill (`broker/simulated.py`). A crypto sell's USD fee may therefore arrive after the run is otherwise done.

**The wait.** For each wind-down fill, the wind-down waits for its fee activity until `FEE_WAIT_MINUTES = 15` after the fill. Any activity already visible is used.
- `FEE_WAIT_MINUTES` is **provisional**. It only decides how long the run waits before sealing with a fee still pending. It is not a settlement boundary, and it never declares fee accounting complete. G1 (§9) replaces it with measured Alpaca behaviour before it is treated as anything more.
- **Fee recognized in time:** recorded in the run's accounting as usual, through the existing exactly-once generation cash ledger. Its cash entry ID is `broker:fee:<activity_id>` (`reconciliation/generation_fees.py`).
- **Fee not yet posted:** recorded in the handover as a `pending_fee` (§7.1). It does not block `WIND_DOWN_COMPLETE`. It stays a durable predecessor obligation until it is attributed (§7.6) or resolved by the operator (§7.3). No timeout closes it.

**Correction to rev. 2.** Rev. 2 said a late fee would be attributed through `verification/soak.py::_late_fee_reopenings`. That is wrong: the function links a new fee to a superseded checkpoint *inside one generation*, and it does nothing once a generation is sealed. Today, a predecessor fee that reaches the successor after the successor's opening checkpoint, dated at midnight before it, is classed `unresolved_generation_membership` by `reconciliation/membership.py` (`new_receipt_predates_opening` or `before_opening_cursor_without_generation_link`). §7.6 defines the missing rule as one new membership class in that same classifier, not as a separate fee pipeline.

## 5. Wind-down states

The wind-down state is durable (§6). It moves only forward through these states, except for the retry edge.

| State | Meaning | Entered when |
|---|---|---|
| `WIND_DOWN_REQUESTED` | entries stopped; census pending | `planned_end_at − lead` reached, or `end-run` received |
| `WIND_DOWN_EXECUTING` | cancels and crypto sells in progress; reconciling | the first census succeeds |
| `WIND_DOWN_BLOCKED` | a safe step is impossible right now. The obligation is kept, and retries continue on a schedule | the broker is unreachable; a definitive rejection; an asset not tradable (crypto maintenance); a `SUBMISSION_UNKNOWN` not yet reconciled; a census changed during capture; `wind_down_order_unaccounted` |
| `WIND_DOWN_COMPLETE` | proven final | the inventory invariant of §5.1 holds on a final census |
| `WIND_DOWN_UNRESOLVED` | operator action is needed; never automatic success | an `unproven` position or a `foreign_open_order` exists in the latest census |

**Rules.**
- `BLOCKED` → `EXECUTING` only after a fresh census proves the blocking condition has cleared:
  - the broker is reachable again;
  - the order that was `SUBMISSION_UNKNOWN` has been reconciled by its client order ID to a terminal state;
  - the asset is tradable again.
- **Time never changes the outcome.** No timeout produces `COMPLETE`, and no timeout produces `UNRESOLVED`. Only census facts move the state.
- **Escalation is an alert only (decision 2, 2026-11-03).**
  - When `BLOCKED` persists past `planned_end_at + WIND_DOWN_ESCALATE_MINUTES` (60), the wind-down appends an `escalated` event and sends a critical alert, then repeats the alert hourly.
  - The state stays `BLOCKED`. Retries and reconciliation continue unchanged.
  - The threshold never decides whether a retry is safe, whether a position is resolved, or whether the generation can seal.
- **Retries:**
  - in `BLOCKED` and `UNRESOLVED`, the runtime keeps running in wind-down mode;
  - monitoring, settlement, reconciliation and the protective exits of carried positions all continue;
  - retries run every `WIND_DOWN_RETRY_SECONDS = 60` where the cause allows;
  - `UNRESOLVED` sends a critical alert when it is entered, then hourly.
- **Sealing:** the run seals only in one of two ways.
  - On `COMPLETE`.
  - With `tradepulse verification wind-down resolve --generation G --reason "<text>"`. The command records the operator's acceptance and seals with outcome `UNRESOLVED_ACCEPTED`, listing every open item in the handover.
    - It can run from `BLOCKED` or `UNRESOLVED`, but only after `planned_end_at`.
    - It is refused with `wind_down_resolve_order_pending` while any intent of the run is non-terminal, or the observed-order register (§4.1) has an entry with no broker terminal state. A seal must not happen while an order's existence or outcome is unknown, because the handover could not then say what the broker holds.
- **Each failure has a defined behaviour:**

| Failure | Behaviour |
|---|---|
| definitive rejection | the intent is closed `REJECTED` with the broker reason; the position stays; `BLOCKED`; retried after a fresh census only if Rev.140 allows a new sell |
| timeout or unknown outcome | `SUBMISSION_UNKNOWN`; no second sell for that asset until reconciliation by client order ID proves a terminal state |
| broker unreachable | `BLOCKED`; nothing fabricated; reconnect and census on schedule |
| partial fill | the remainder follows §4.3 |
| crash mid-step | §6 |

### 5.1 Inventory invariant (REV147-R2-03)

Let **B** be the final census's broker positions, as (asset identity key, signed quantity), and **O** its broker open orders. Every element of B carries exactly one class from §4.2. Completion is checked against those classes, not against a general statement about local holdings.

**`WIND_DOWN_COMPLETE` holds only when all of these are true on one final census:**

1. **Partition:** B = Carried ⊎ Residual ⊎ Unresolved, with each asset in exactly one part, and with **no position unclassified**.
2. **Nothing unresolved:** Unresolved = ∅. There is no `unproven` position and no `foreign_open_order`.
3. **Nothing left to close:** the `close_crypto` class is empty. Every asset that was `close_crypto` at any census is now at broker quantity 0, or has been reclassified as `crypto_residual` by a fresh census after its last sell became terminal.
4. **Carried positions match:** for each carried asset, the broker quantity = the local holding quantity = the sum of the remaining quantities of the local open lots = the handover's `carried` entry. The protective state in the handover equals the holding and lot rows exactly (§7.1).
5. **Residuals match:** for each residual asset, the broker quantity = the remaining quantity of its local lots = the handover's `residual` entry.
6. **No orders open:** O = ∅, every local intent of the run is terminal, and the observed-order register (§4.1) is empty: every order ever observed has a broker terminal state.
7. **Fills accounted:** every wind-down fill is settled and attributed. Each fill's fee is either recognized or listed as a `pending_fee`.

**For `UNRESOLVED_ACCEPTED`:**
- items 1 and 4–7 must still hold;
- Unresolved may be non-empty, but each element is listed in the handover with its class, quantity and reason;
- item 6's register condition is enforced by the resolve refusal above.

No outcome seals with an unclassified position, an unaccounted order, or a carried entry that differs from the local rows.

## 6. Durability and crash recovery

### 6.1 Storage

The wind-down state lives in the generation's database, in a new append-only table `wind_down_events` with `BEFORE UPDATE`/`DELETE` triggers. It is checked at startup like Rev.140's tables (`SCHEMA_TRIGGER_MISSING`).
- **Columns:** event ID, generation, state, cause, census digest, request reason, and occurrence time.
- **Event kinds** include the run-state events, the wind-down states, `escalated`, and the observed-order register entries and exits (§4.1).
- **The current state** is the latest state event, derived on every read. There is no mutable state row.

### 6.2 After a crash or restart

Whether the cause is a crash, a power loss, a service restart or a signal, `run --resume` does these steps in order, before any scan lane starts:
1. Load the frozen `planned_end_at`, plus the latest `wind_down_events` row and any `end-run` request.
2. Run the existing startup reconciliation, including reconciliation of every `SUBMISSION_UNKNOWN` intent by its original client order ID.
3. Decide the phase:
   - If the current time is at or past `planned_end_at − lead`, or a `WINDING_DOWN` state or `end_run_requested` event exists, enter wind-down at the recorded sub-state. Scan lanes never start. Monitoring, settlement and reconciliation start.
   - Otherwise, the run is `PAUSED` (or crashed), and it returns to `RUNNING` only after all of these pass, in order. Any failure keeps it paused and refuses with the existing reason:
     - the existing integrity check (`Verification.check`), including the Rev.146 DBAE fingerprint;
     - broker identity (`startup_broker_account_identity_changed`);
     - the frozen configuration (`frozen_configuration_mismatch`);
     - the startup reconciliation from step 2, with every outstanding obligation reconciled.

     On success, a `run_resumed` event is appended, and only then do scan lanes start.
4. In wind-down, take a fresh census before any new order. A sell is never generated because an earlier submission timed out. Only a reconciled terminal state permits the next sell.

The previous generation's accounting and its evidence boundary are never touched. A late predecessor fee is attributed through §7.6.

### 6.3 What recovery cannot cover

If the machine stays off, nothing runs. Crypto keeps trading, and no stop protects anything until the runtime returns. Inside a run, this is the paused or crashed case (§3.3). Between runs, §7.5 defines the protection responsibility exactly. Broker-held backup stops (a future Rev) and cloud hosting are the mitigations.

## 7. Handover and next-run eligibility

### 7.1 The handover file

At seal, the generation writes `handover.json` into its directory. It is included in the sealed evidence digest. After the seal it is never edited; everything later goes into `handover-amendments.jsonl` (§7.3, §7.6). It contains:

- `generation`, `outcome` (`COMPLETE` or `UNRESOLVED_ACCEPTED`), `sealed_at`, the final census digest, `account_identity_digest`, and `handover_schema_version: 1`.
- **`carried`:** one entry per `carry_adoptable` position, holding its **full protective state** (REV147-R2-01). A ratcheted stop alone is not enough: the exit ladder measures R from `average_price − stop_loss`, and the time and stall exits count from each lot's `opened_at` (`monitor/coordinator.py::plan_ladder_exit`). Each entry contains:
  - `asset` (identity), `quantity`, and `broker_avg_entry_price` (from the final census, for comparison only);
  - `holding`, copied exactly from the final holding row:
    - `average_price` and `sector`;
    - `stop_loss`: the **immutable initial stop**, taken from the governing entry;
    - `current_stop`: the **authoritative ratcheted stop**, or `null` if break-even never triggered;
    - `target_price` and `scaled_out`;
    - `updated_at`;
  - `effective_stop`: `current_stop` if it is set, otherwise `stop_loss`. This is the same rule as `monitor/coordinator.py`;
  - `stop_state`: `initial`, `break_even_stop` or `trailing_stop`, using the same rule as the monitor's `_exit_reason`;
  - `lots`: for each open lot, `lot_id`, `originating_fill_id`, `opened_quantity`, `remaining_quantity`, `acquisition_price`, `opened_at`, `mfe_price` and `mae_price`;
  - `protective_state_digest`: SHA-256 over the canonical encoding of `holding` and `lots`;
  - `stop_provenance`:
    - the source generation;
    - the holding and lot record IDs;
    - the stop's origin: `entry` for a stop from this run's own entry, or `inherited:<generation>` for a stop carried in from an earlier handover;
  - `adoption_policy_version: "rev147-handover-v1"`.
- **`residuals`:** each `crypto_residual`, with its quantity, its lot IDs and the reason.
- **`unresolved`:** each `unproven` position and each `foreign_open_order`, with its class, quantity and reason. It is empty unless the outcome is `UNRESOLVED_ACCEPTED`.
- **`pending_fees`:** for each fill whose fee had not posted:
  - `pending_fee_id`;
  - the local fill ID, the broker fill (activity) ID and the broker order ID;
  - the asset, the fill time and the expected kind (USD CFEE for a crypto sell, FEE for a regulatory equity fee);
  - `origin_generation`. This is this generation, or the earlier one for a still-open fee inherited through a chain (§7.6).
- **`protection`** (§7.5):
  - `monitoring_released_at`: when the monitor loop stopped, which is before the seal;
  - `next_session_open`: from the Rev.144 calendar;
  - `carried_count`.

### 7.2 Freeze and startup of the next generation

- `tradepulse verification freeze` gains `--predecessor <generation>`. It is required when the most recently sealed generation in the store was frozen under Rev.147 (its manifest has a `wind_down` key). A generation sealed before Rev.147 has no handover and needs no predecessor. The named predecessor must be that most recently sealed generation, or freeze refuses with `predecessor_not_latest`.
- **Freeze refuses** in each of these cases:
  - the predecessor's handover is missing or its digest fails: `handover_missing` or `handover_digest_invalid`;
  - the predecessor's `unresolved` list is not empty and not covered by an amendment (§7.3): `handover_unproven_items`;
  - the account identity differs: `handover_account_mismatch`;
  - a carried entry's protective state fails validation (§7.2.1): `handover_stop_invalid` or `handover_stop_unverified`.
- **When the opening checkpoint is captured,** the broker state must equal the handover plus amendments, exactly:
  - every broker position must be either a `carried` entry or a `residual` entry with the same asset and quantity (`handover_position_mismatch`);
  - there must be no open orders (`handover_open_orders_present`).

  Any mismatch refuses the freeze. Startup (`runtime_started`) repeats the same comparison against the bound checkpoint, and then applies §7.5.
- **Carried positions** are then `opening_inventory` under Rev.142, so they are never classified `unexpected_external`. They are adopted through §7.2.1, not through Rev.142's broker-average reconstruction.
  - Their lots keep `origin = "adopted"`, so their closes stay out of the successor's strategy edge, exactly as Rev.142 does.
  - Their adoption rows record the predecessor generation, the handover digest and the `protective_state_digest` as provenance.
- **Residual crypto** is valued and reported, but neither managed nor adopted (TP145-02).
- **Pending fees** follow §7.6.

### 7.2.1 Restoring the protective state (REV147-R2-01)

For a carried position, the successor's adoption takes the protective state from the handover. It does not recompute a stop.
- **Holding:** the adopted holding takes `stop_loss`, `current_stop`, `target_price`, `scaled_out` and `average_price` exactly from the handover `holding`.
- **Lots:** each adopted lot takes `acquisition_price`, `opened_at`, `mfe_price` and `mae_price` from the handover lot.
- **Provenance:** `basis_provenance = "predecessor_handover"` and `stop_source = "predecessor_handover"`.
- **Why the stop is not recomputed:**
  - Rev.142's `entry_stop_loss(broker_avg_entry_price, …)` is still computed, but only as an audit value. It is recorded as `reconstructed_stop` and never applied.
  - The inherited stop is applied exactly. Raising it would diverge from what a continuous run would hold, and lowering it is forbidden.
  - The monitor's existing ratchet (`_ratchet_stop`, `max` for a long position) then continues from the inherited `current_stop`, so the stop can only rise from there.
- **Validation at freeze.** Each check that fails refuses the freeze and raises a critical integrity alert, and the position is not adopted automatically:
  1. Values are positive Decimals, and `current_stop` is either `null` or ≥ `stop_loss` (the long ratchet invariant). A failure gives `handover_stop_invalid`.
  2. `effective_stop` and `stop_state` agree with `holding` under the monitor's rules: `handover_stop_invalid`.
  3. `protective_state_digest` recomputes from the entry: `handover_stop_unverified`.
  4. The entry equals the predecessor's final holding and lot rows. These are read from the predecessor database, which is opened read-only and verified against its sealed evidence digest. A failure gives `handover_stop_unverified`.
  5. The lots' remaining quantities sum to `quantity`: `handover_stop_unverified`.
- **Cost basis.** `broker_avg_entry_price` may differ from `average_price` (for example, Alpaca's averaging after a partial exit). This difference is recorded in the adoption row and does not refuse adoption, because the protective state comes from TradePulse's own fills, which the predecessor's sealed evidence proves.
- **No amendment can set or change a stop.** An operator who doubts a carried stop closes the position outside TradePulse, then amends the expected quantity with broker proof (§7.3).
- **Successor startup:** a carried position whose mark is at or below its inherited `effective_stop` exits on the first regular-session monitor cycle. This is the existing behaviour, and it gets no special case.

### 7.3 Operator amendment between runs

`tradepulse verification handover amend --predecessor G --reason "<text>"`, with one of these:
- `--position <asset>=<qty>`;
- `--resolve <item-id>`;
- `--resolve-fee <pending_fee_id> --activity <id>`;
- `--accept-gap`.

How it works:
- It appends an operator-attributed amendment record (reason, time, OS user) next to the sealed handover: a new file `handover-amendments.jsonl`, append-only with a per-line digest chain. It never edits `handover.json`.
- **Use 1:** declare a deliberate change between runs. Example: the 1 inherited share bought for the next run's adoption test. The amended expectation is what §7.2 compares against. A newly declared position is not a carried one: it has no inherited protective state and goes through ordinary Rev.142 adoption.
- **Use 2:** record how an `unresolved` item was resolved outside TradePulse. Each `--resolve` must cite the broker activity or order ID that proves it. The amend command verifies that ID against the broker's activity history (read-only) before appending.
- **Use 3:** `--resolve-fee` attributes an unlinked fee to a pending fee (§7.6).
- **Use 4:** `--accept-gap` accepts a protection gap (§7.5).
- Every amendment is shown in the next generation's freeze output and copied into its manifest.

### 7.4 The first Rev.147 run

The account has been flat since 2026-10-29, and no Rev.147 handover exists. The first generation frozen under Rev.147 therefore has no predecessor. Freeze still requires an empty open-order list. Its opening positions are whatever the operator has deliberately arranged, such as the 1 inherited share, and Rev.142 adopts them as today, with a stop rebuilt from the broker's average entry.

### 7.5 Protection continuity between generations (REV147-R2-02)

**`WIND_DOWN_COMPLETE` proves inventory and orders. It does not prove continuous protection.** Between the predecessor's monitor stopping and the successor's monitor starting, no TradePulse process protects a carried position. The handover states that interval; it does not hide it.

**Why the monitors cannot overlap.** The reviewer preferred an overlap, with the successor monitoring before the predecessor releases. It is not possible with the current design, and it would not be safe:
- One runtime per account is enforced by the account lease (`runtime_lease.py`), so the two processes cannot both hold it.
- Two monitors on one account could both act on one stop, which would mean a duplicate sell.
- An exit by the predecessor after its seal would be a fill outside its sealed evidence.

**The responsibility rule** rests on two facts:
- stock and option exits act on regular-session prices only (`monitor/coordinator.py`, the out-of-session rule);
- the predecessor seals outside an XNYS session (§3.1, §3.3).

So between the seal and the next regular-session open, the predecessor's monitor would have taken no equity or option action even if it had kept running.

1. **The predecessor** is responsible until `monitoring_released_at`, which it records in the handover (§7.1) together with `next_session_open`.
2. **The successor** becomes responsible at `monitoring_assumed_at`. That is the moment its monitor has completed one evaluation of every carried position with the restored protective state, after the §7.2 startup comparison. The successor records it in its `runtime_started` evidence.
3. **Continuity class**, recorded in the successor's start record and reported in both generations' evidence:
   - `no_session_gap`: `monitoring_assumed_at` < `next_session_open`. Protection is equivalent to a continuous run. The interval is still recorded.
   - `session_gap`: otherwise. The record lists each regular session, or part of one, during which no monitor ran.
   - `no_carried_positions`: `carried_count = 0`. There is nothing to protect.
4. **Operator acceptance.** If the successor's startup would produce `session_gap`, startup refuses with `handover_protection_gap_unaccepted`, unless an `--accept-gap` amendment exists. That amendment is appended after the predecessor's `next_session_open` has passed, and it states the operator's reason. A `session_gap` start then records the accepted interval and the amendment ID.
5. **Gap audit.** On a `session_gap` start, for each carried position, the successor compares the regular-session low of each missed session (from the existing bar provider) against the inherited `effective_stop`.
   - If any low was at or below the stop, it raises a critical alert, `CARRIED_STOP_BREACHED_UNMONITORED`, and reports it in the run evidence. The exit itself is the normal one (§7.2.1).
   - If bars are unavailable, the audit records `gap_audit_unverified`. It does not guess.
6. **Run plans** for successive generations schedule the successor's start before `next_session_open`, so the planned result is `no_session_gap`.

**Crypto residuals** are unmanaged in both generations (TP145-02), so they are outside this rule. They are already listed as residuals.

### 7.6 Predecessor fees after the seal (REV147-R2-04)

**Immutability.** A sealed generation's database, `handover.json` and evidence digest never change. A fee that belongs to the predecessor but arrives after the seal is appended to the predecessor's `handover-amendments.jsonl` as a **system-written** `fee_attribution` record, in the same digest chain as the operator amendments. Each record cites:
- the activity ID and the SHA-256 of the raw activity;
- the `pending_fee_id`;
- the observing generation;
- the time.

The successor database keeps a mirroring reconciliation record. The predecessor's reports show the sealed figures, plus a separate `post_seal_adjustments` section built from these records. A sealed figure is never recomputed in place.

**Matching** happens by broker identity only, never by timing, amount or rate:
- **Linked fee.** The activity carries a broker `fill_id` or `order_id`, the same fields as the existing `authoritative_fill_fee` and `authoritative_order_fee` relationships in `generation_fees.py`. It is matched to a `pending_fee` with that broker fill or order ID and the same asset, if the activity names one.
- **Unlinked fee.** The activity carries neither. It is never attributed to the predecessor automatically: it is handled exactly as today in the successor. The operator may attribute it with `handover amend --resolve-fee <pending_fee_id> --activity <id>`. The command checks, read-only against the broker history, that the activity:
  - exists;
  - is a USD FEE or CFEE debit;
  - was created on or after the fill's date;
  - is not already attributed anywhere.

  The `pending_fee` stays open until then. G1 establishes whether Alpaca's crypto sell CFEE carries a link.

**Where it plugs in:** one rule in the existing membership classifier (`reconciliation/membership.py::_classify`), and no new fee pipeline.
- **The new class.** A FEE or CFEE activity that is absent from the successor's opening population and matches an open `pending_fee` (linked, or named by a `--resolve-fee` amendment) is classified **`predecessor_obligation`**. The rule is checked before the `unresolved_generation_membership` fallbacks.
- **Exclusion from the successor.** `predecessor_obligation` is not in `ELIGIBLE_MEMBERSHIPS`. The successor's cash check, cash ledger and accounting therefore exclude it, and the fee is never posted to two generations.
- **A fee posted before the successor's freeze.** It is in the successor's opening population and is already `pre_generation` (excluded). Freeze matches it to the pending fee by the same rule and appends the `fee_attribution` record.

**Duplicates and conflicts:**
- **Key:** the activity ID is the attribution key.
- **The same ID with an identical raw digest** is an idempotent no-op. It is neither refused nor posted again, which mirrors the exactly-once `broker:fee:<activity_id>` rule.
- **The same ID with a different raw digest** is refused with `fee_attribution_conflict` and raises a critical alert. Neither version is posted.
- **Several fees for one fill:** one `pending_fee` may be satisfied by more than one linked activity, for example a split fee. Each activity is attributed once by its own ID. The pending fee is marked `observed` at the first one and stays matchable for the rest.

**Corrections and reversals:**
- Some linked activities reference a predecessor fill but are not a USD debit, such as a credit or a reversal. The existing code refuses these with `GENERATION_FEE_NOT_A_DEBIT`.
- Such an activity is recorded as a `fee_correction` attribution against the predecessor, citing the original `pending_fee_id` and, if any, the fee activity it reverses.
- It is never netted against or written over the original record, and it is excluded from the successor's cash check.
- It raises an alert for operator review. No cash entry is posted automatically, which matches today's refusal to post non-debits.

**Chains:**
- A `pending_fee` still open when the successor seals is copied into the successor's handover `pending_fees`, keeping its `origin_generation`.
- An attribution always targets the origin generation's amendments file.
- A pending fee is closed only by attribution or by a verified `--resolve-fee`. Time never closes it.

## 8. Entry stop

In wind-down, `ExecutionGateway` refuses every BUY that is not a protective exit, with `RUN_WIND_DOWN_ACTIVE`. The check runs in the same fenced authorization as `RISK_APPROVED`, alongside Rev.146's freshness check, so a buy authorized just before wind-down began cannot be persisted after it. Scan lanes also idle in wind-down, but the gateway check is the guarantee.

## 9. Gates

- **G1 (read-only, before the build):** from the account's existing activity history, find whether past crypto sell fills have a USD CFEE activity, its delay, and whether it carries a `fill_id` or `order_id` link (§7.6). If there are no crypto sells in the history, record that, and keep §4.5 and §7.6 unchanged. Until G1 is recorded, `FEE_WAIT_MINUTES` is labelled provisional in the record and the run plan.
- **G2 (the first real wind-down):** the first 24 h run under Rev.147 is the first live exercise. Its run plan lists:
  - the expected census, classification and final handover, including each carried position's protective state;
  - the planned successor start, before `next_session_open`.

  The run's acceptance criteria include `WIND_DOWN_COMPLETE` and the §5.1 invariant.

## 10. Tests

All on the simulator, with no broker calls.

- **Happy path:** crypto positions sold, entry orders cancelled, equities carried. The result is `COMPLETE`, the handover lists the carried positions with their protective state, and §5.1 holds.
- **Partial fills:**
  - a partial fill of the wind-down sell, with the remainder sold after a terminal state;
  - a partial fill of an entry order during its cancel.
- **Rejection:** definitive rejection → `BLOCKED` → cleared → `COMPLETE`.
- **Unknown submission:** `SUBMISSION_UNKNOWN` on the sell. The test proves no second sell until reconciliation by client order ID, then completion.
- **Broker outage:**
  - an outage through `planned_end_at` → `BLOCKED`;
  - after 60 min, an `escalated` event and an alert, with the state still `BLOCKED`, and no change to retries or to the seal;
  - then reconnect, completion, and no seal before completion.
- **Crypto maintenance:** an asset not tradable → `BLOCKED` → retried.
- **Crash at each state.** At each of `REQUESTED`, `EXECUTING`, `BLOCKED` and `UNRESOLVED`:
  - the restart resumes the state;
  - no scan lane starts;
  - no duplicate sell is sent;
  - the previous accounting is unchanged.
- **Signal and pause:**
  - SIGINT before the lead time writes `run_paused`, cancels nothing and sells nothing.
  - SIGINT during an in-flight submission ends with the intent in its true state, never cancelled.
  - A restart returns the run to `RUNNING` only after the resume checks pass. Each failing check keeps it paused.
  - SIGINT during `WINDING_DOWN` makes the restart resume the wind-down, never `RUNNING`.
- **`end-run` idempotency:**
  - repeated `end-run` appends one event and sends no extra sells;
  - `end-run` on a paused run starts the wind-down on the next start;
  - no path returns a wound-down generation to `RUNNING`.
- **Entries while paused:** a BUY during `PAUSED` is refused with `RUN_PAUSED` inside the fenced transaction.
- **Early end:**
  - `end-run` outside a session runs the wind-down;
  - `end-run` inside a session holds the seal until the session ends.
- **Inventory invariant (R2-03):**
  - **class coverage:** for a census with one position of each class, every position gets exactly one class, and an unmatched shape defaults to `unproven`;
  - **carried mismatch:** a carried holding whose lot sum differs from the broker quantity is not `COMPLETE`;
  - **residual mismatch:** residual dust whose local lots disagree with the broker becomes `unproven`;
  - **vanished order:** an order missing from the open list with no fetched terminal state keeps `BLOCKED` (`wind_down_order_unaccounted`);
  - **foreign order:** a foreign open order with no local intent still appears and leads to `UNRESOLVED`;
  - **resolve refusal:** `resolve` is refused with `wind_down_resolve_order_pending` while a `SUBMISSION_UNKNOWN` is unreconciled.
- **Stop preservation (R2-01):**
  - **break-even:** a position whose stop moved to break-even is carried, and the successor's holding has `current_stop = average_price`, the original `stop_loss` unchanged, and `reconstructed_stop` recorded but unused;
  - **trailing:** a trailing stop above break-even is restored exactly, and the next in-session ratchet only raises it;
  - **lower rebuild:** a broker average that would rebuild a lower stop has no effect;
  - **scale-out:** `scaled_out = true` survives, so no second scale-out happens;
  - **lot dates:** `opened_at` survives, so the time stop and stall exit count from the original entry;
  - **tampered state:** a tampered `current_stop`, a `current_stop < stop_loss`, a digest mismatch, and a handover that differs from the predecessor database each refuse the freeze with `handover_stop_invalid` or `handover_stop_unverified`;
  - **breached at start:** a mark at or below the inherited stop exits on the first in-session cycle.
- **Continuity (R2-02):**
  - **no gap:** a successor started before `next_session_open` records `no_session_gap`;
  - **gap refused:** one started after it refuses with `handover_protection_gap_unaccepted`;
  - **gap accepted:** with `--accept-gap`, it starts, records the interval and runs the gap audit. A missed-session low through the stop raises `CARRIED_STOP_BREACHED_UNMONITORED`, and missing bars record `gap_audit_unverified`;
  - **no carried positions:** zero carried positions record `no_carried_positions`, and no acceptance is needed.
- **Late fees (R2-04):**
  - **inside the wait:** the fee is recognized in the run;
  - **posted before the successor's freeze:** it is matched at freeze and appended as `fee_attribution`;
  - **after the successor's opening:** a linked fee is classified `predecessor_obligation`, excluded from the successor's cash check, and attributed;
  - **unlinked:** it is not attributed automatically, and `--resolve-fee` attributes it after read-only verification;
  - **duplicates:** the same activity seen twice is a no-op, and the same ID with different content gives `fee_attribution_conflict`;
  - **split fee:** two fees on one fill are each attributed once;
  - **reversal:** it is recorded as `fee_correction`, not posted, and alerted;
  - **chain:** a fee still pending at the successor's seal carries forward with its `origin_generation`;
  - **immutability:** the predecessor's database, `handover.json` and sealed digest are byte-identical before and after every attribution.
- **Reporting (decision 1):**
  - wind-down fills appear in realized P&L and cash, and are absent from the edge statistics;
  - the `wind_down_excluded` section reconciles with the totals;
  - a stop that fires inside the lead window stays edge-eligible.
- **Residuals and unproven positions:**
  - dust below `min_order_size` becomes `crypto_residual`;
  - unmanaged crypto, a quarantined liability, a short and a managed equity with no stop each become `unproven`, are never sold, and lead to `UNRESOLVED`.
- **Next-run refusals:** each §7.2 refusal:
  - a predecessor that is missing or not the latest;
  - a digest that fails;
  - unproven items;
  - a position mismatch;
  - open orders present;
  - an account mismatch;
  - a stop that is invalid or unverified.
- **Amendments:**
  - one declaring 1 share permits freeze, and that share is adopted under ordinary Rev.142 rules;
  - an amendment with an unverifiable `--resolve` ID is refused;
  - no amendment can change a carried stop.
- **Entry stop race:** a BUY whose authorization straddles the wind-down start is refused inside the fenced transaction.
- **Frozen policy:** `generation_predates_wind_down`, and `frozen_configuration_mismatch` on a changed `planned_end_at`.
- **Property tests:**
  - over random sequences of fills, rejections, timeouts, outages and crashes, check that:
    - at most one open wind-down sell exists per asset;
    - `COMPLETE` implies every element of the §5.1 invariant;
    - no state is reached from `UNRESOLVED` except through the operator command or a proven completion;
    - time alone never changes the wind-down state;
  - over random ratchet histories, the successor's effective stop equals the predecessor's final effective stop, and it never decreases afterwards.

## 11. Constraints

- Fix at the core: no `# type: ignore`, no `cast`, no `assert` used for narrowing.
- Money is Decimal.
- Commits are GPG-signed.
- Every new refusal string is listed in this spec and in the record.
- New refusal and state strings:
  - `generation_predates_wind_down`, `wind_down_end_not_aware`, `wind_down_end_not_future`, `wind_down_end_inside_session`, `wind_down_end_calendar_unverified`;
  - `predecessor_not_latest`, `handover_missing`, `handover_digest_invalid`, `handover_unproven_items`, `handover_account_mismatch`, `handover_position_mismatch`, `handover_open_orders_present`;
  - `RUN_WIND_DOWN_ACTIVE`;
  - the five `WIND_DOWN_*` states;
  - the outcome `UNRESOLVED_ACCEPTED`.
- **No second order-recovery or settlement implementation.**
  - Wind-down sells, cancels and their recovery go only through the existing `ExecutionGateway`, its client-order-ID idempotency and `SUBMISSION_UNKNOWN` reconciliation, the Rev.140 sell gate, `SettlementProcessor`, and the reconciliation coordinator.
  - Late predecessor fees use the existing membership classifier and generation cash ledger (§7.6).
  - Rev.147 adds only the run state, the census, the observed-order register, the classification, the per-asset sequencing rule, the handover, the protective-state restore, the continuity record and the next-run checks.
- New refusal strings in rev. 2: `RUN_PAUSED`. New run-state events: `run_paused`, `run_resumed`, `end_run_requested`.
- New strings in rev. 3:
  - refusals: `handover_stop_invalid`, `handover_stop_unverified`, `handover_protection_gap_unaccepted`, `wind_down_resolve_order_pending`, `wind_down_order_unaccounted`, `fee_attribution_conflict`;
  - classification reasons: `carried_stop_missing`, `carried_stop_invalid`;
  - alerts: `CARRIED_STOP_BREACHED_UNMONITORED`;
  - audit outcomes: `gap_audit_unverified`;
  - continuity classes: `no_session_gap`, `session_gap`, `no_carried_positions`;
  - membership class: `predecessor_obligation`;
  - records and sections: `fee_attribution`, `fee_correction`, `post_seal_adjustments`, `wind_down_excluded`;
  - events: `escalated`;
  - provenance: `predecessor_handover`, `rev147-handover-v1`.
- The build happens on its own branch and worktree, after Rev.146 is merged. Nothing is merged without review and the operator's approval.

## 12. Open questions for review

1. ~~§3.3 (stop signals).~~ Decided in rev. 2: Ctrl+C pauses; `end-run` and the scheduled end start the wind-down.
2. ~~§4.3 (reporting).~~ Decided 2026-11-03: included in financial accounting, excluded from edge statistics, and reported as `wind_down_excluded`.
3. **§4.5 (fee wait):** `FEE_WAIT_MINUTES = 15` stays provisional until G1 measures the CFEE delay and its linkage.
4. ~~§5 (escalation).~~ Decided 2026-11-03: 60 minutes, as an alert only.
5. **§7.5 (continuity design):** this revision replaces the reviewer's preferred monitor overlap with a session-boundary rule plus operator acceptance, because the account lease makes overlap impossible and two monitors would risk a duplicate exit. Does the reviewer accept that equivalence?
6. **§4.2 (option expiry across the gap):** a carried option that expires before the successor's first regular session would expire unmonitored. Should such an option be `unproven` at wind-down, which blocks the seal until the operator acts, or carried with an alert?
