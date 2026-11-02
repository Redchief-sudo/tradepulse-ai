# Rev.147 — Verification run wind-down and generation handover

**Status:** design, rev. 1, for forensic review. No production code changes and no orders until the spec is approved.
**Builds on:** Rev.140 (sellable inventory, the sell gate), Rev.142 (adoption), Rev.144 (calendar), Rev.146 (DBAE, merged before this Rev is built).
**Operator decisions (2026-11-01):**
- a verification run ends at a planned time;
- before it ends, the system closes everything the next run cannot adopt;
- long equities and options carry over to the next run;
- a crash or power loss must recover safely.

**Reviewer decision (2026-11-01):** keep the original position and exit obligation. Reconcile, and retry only when it is independently safe. Block any next run that cannot account for it. Never force a duplicate sell, and never claim the account is flat without broker proof.

## 1. Goal

At the end of a verification run, TradePulse either closes every non-adoptable position safely, or records each remaining one as an explicit, reconciled obligation. Either way, the next run can neither adopt nor ignore it by mistake.

Success means three things:
- the run's last state is `WIND_DOWN_COMPLETE`, proven by broker positions, broker open orders and matching accounting;
- a sealed **handover** lists every position, order and fee obligation that remains;
- the next generation's freeze and startup refuse unless the broker state matches that handover exactly.

## 2. Scope

**In scope.**
- **Planned end:** a planned end time frozen into the generation.
- **Wind-down state:** a durable wind-down state machine, recovered after a crash or restart.
- **Stopping entries:** at wind-down start, entries stop and the run's open entry orders are cancelled.
- **Closing crypto:** every crypto position the run owns is sold through the existing gateway and the Rev.140 sell gate.
- **Handover:** a sealed handover file, and a next-run eligibility check against it.
- **Operator commands:** end a run early, view status, resolve an unresolved wind-down, and amend a handover between runs.

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
- It falls inside an XNYS session, or within the 15 minutes before an open: `wind_down_end_inside_session`. This is the same rule as `scripts/a0_verification_window.py`, which keeps the valuation snapshot from moving at the boundary. Ending outside equity hours also means no equity or option exit can be pending at the end.
- The calendar cannot verify it: `wind_down_end_calendar_unverified`.

### 3.2 Phases

| Time | Phase |
|---|---|
| before `planned_end_at − lead` | normal run |
| from `planned_end_at − lead` | wind-down: no new entries; the crypto close runs |
| after `planned_end_at` | the wind-down continues until it reaches a final state (§5). The run never seals in a non-final state |

### 3.3 Stop signals, crashes and the early end

- **SIGINT or SIGTERM** (Ctrl+C, and the `tradepulse-run` service's own `KillSignal=SIGINT`) **does not start a wind-down.** It stops the process the same way a crash does, and everything is resumable. This matters because the service uses the same signal for restarts. If a signal triggered liquidation, every restart or deploy would liquidate the run.
  - This changes what was offered in chat on 2026-11-01 ("Ctrl+C runs the same wind-down"), and the operator should confirm it.
- **Ending early is explicit:** `tradepulse verification end-run --generation G --reason "<text>"`.
  - It writes a durable wind-down request with the time and the reason.
  - The running supervisor sees it within one watch cycle (≤ 30 s) and enters wind-down.
  - Before any orders, the same window rule as §3.1 is checked against the current time:
    - inside an XNYS session, the request is accepted and the crypto close runs;
    - the seal is held until the session ends, so the valuation boundary rule still holds;
    - the status shows `seal_deferred_until`.
- **After a crash or power loss,** the restarted runtime (`run --resume`) reloads the wind-down state before any lane starts (§6).

## 4. What the wind-down does

### 4.1 Census

Before any order, the wind-down takes a **census**. The pattern is the opening checkpoint's capture: activities, then account, positions and open orders, then activities again. A change in between invalidates the census, which is retried. The census records:
- every broker position: identity, quantity, average entry price, `received_at`;
- every broker open order: client order ID, broker order ID, side, quantity, status;
- every local open intent, holding and lot;
- the Rev.140 sellable decision for each crypto asset.

### 4.2 Classification

Each broker position falls into exactly one class:

| Class | Rule | Wind-down action |
|---|---|---|
| `carry_adoptable` | long equity or option, fully managed by this run (holding and lots match the broker quantity) | none; protective exits stay active until the end; recorded in the handover |
| `close_crypto` | long crypto, fully managed by this run, Rev.140 sellable = broker quantity | sell the sellable quantity through the gateway (§4.3) |
| `crypto_residual` | long crypto managed by this run, quantity > 0 but below the asset's `min_order_size`, or not a multiple of `min_trade_increment` after a sell | none (unsellable); recorded as a residual |
| `unproven` | any position whose quantity, ownership or pending obligations cannot be proven: partially managed; quarantined; a short or a Rev.140 liability; unmanaged crypto; an adopted asset with an `ADOPTED_EXIT_REFUSED` outstanding | none, ever; a critical alert; recorded; blocks the next run until resolved (§7.3) |

### 4.3 The crypto close

- **Request:** each `close_crypto` asset gets one SELL `ExecutionRequest`, with `exit_reason = "run_wind_down"`, quantity equal to Rev.140's sellable quantity, a market order and `gtc` (crypto).
- **Path:** the request goes through the existing gateway as a protective exit. It therefore passes the Rev.140 sell reservation, the existing client-order-ID idempotency and the account-qualified identities. DBAE is never called, because protective exits bypass it (Rev.146 §9).
- **One sell at a time per asset.** No new sell is built while that asset has a sell intent in `RISK_APPROVED`, `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED` or `SUBMISSION_UNKNOWN`.
- **After a partial fill,** the remainder is re-sized from a fresh census and sold only after the first order is terminal.
- **Accounting:** wind-down fills are ordinary strategy exits. They close the run's own lots, count in the run's realized P&L, and carry `exit_reason = "run_wind_down"`, so reports can separate them.

### 4.4 Open orders

- **At wind-down start,** the run's own open BUY orders are cancelled through `cancel_order`. These are orders with an intent in this database whose client order ID matches the account-qualified scheme.
- **A cancel is not proof.** The order counts as closed only when the broker reports a terminal state (`canceled`, `expired`, `rejected` or `filled`). A fill that arrives during the cancel is settled normally. A resulting crypto position is then classified again (§4.2), and a resulting equity position becomes `carry_adoptable`.
- **Protective SELL orders in flight are never cancelled.** They are reconciled to a terminal state.
- **An open order that isn't this run's** (no matching intent) is never cancelled. It gets a critical alert and is recorded as `foreign_open_order`, which blocks the next run (§7.3).

### 4.5 Fee obligations

Alpaca posts FEE and CFEE activities dated at midnight, after the fill (`broker/simulated.py`). A crypto sell's USD fee may therefore arrive after the run is otherwise done. For each wind-down fill, the wind-down waits for its fee activity until a fixed deadline: `FEE_WAIT_MINUTES = 15` after the fill, plus any activity already visible.
- **Fee recognized:** included in the run's accounting as usual.
- **Fee not yet posted:** recorded in the handover as `pending_fee` (fill ID, asset, expected kind), and it does not block `WIND_DOWN_COMPLETE`. A later activity that matches a listed `pending_fee` is attributed to this generation through the existing late-fee reopening path (`verification/soak.py::_late_fee_reopenings`). The next generation excludes it from its own cash check (§7.2).
- **The fee timing is unverified.** Whether Alpaca posts the CFEE minutes or hours after a crypto sell fill, and whether the sell fee is a separate activity at all, are recorded as gate G1 (§9). The design handles both cases.

## 5. Wind-down states

The wind-down state is durable (§6). It moves only forward through these states, except for the retry edge.

| State | Meaning | Entered when |
|---|---|---|
| `WIND_DOWN_REQUESTED` | entries stopped; census pending | `planned_end_at − lead` reached, or `end-run` received |
| `WIND_DOWN_EXECUTING` | cancels and crypto sells in progress; reconciling | the first census succeeds |
| `WIND_DOWN_BLOCKED` | a safe step is impossible right now. The obligation is kept, and retries continue on a schedule | the broker is unreachable; a definitive rejection; an asset not tradable (crypto maintenance); a `SUBMISSION_UNKNOWN` not yet reconciled; a census changed during capture |
| `WIND_DOWN_COMPLETE` | proven final | every `close_crypto` asset has broker quantity 0 or is a recorded `crypto_residual`; no open orders of this run; every wind-down fill attributed; fees recognized or listed as `pending_fee`; a final census agrees with local holdings exactly |
| `WIND_DOWN_UNRESOLVED` | operator action is needed; never automatic success | `unproven` or `foreign_open_order` items exist; or `BLOCKED` persisted past `planned_end_at + WIND_DOWN_ESCALATE_MINUTES` (60) |

**Rules.**
- `BLOCKED` → `EXECUTING` only after a fresh census proves the blocking condition has cleared:
  - the broker is reachable again;
  - the order that was `SUBMISSION_UNKNOWN` has been reconciled by its client order ID to a terminal state;
  - the asset is tradable again.
- **No timeout produces `COMPLETE`.** A timeout only escalates `BLOCKED` to `UNRESOLVED`.
- **In `UNRESOLVED`, the runtime keeps running** in wind-down mode. Monitoring, settlement, reconciliation and the protective exits of carried positions all continue. Retries continue every `WIND_DOWN_RETRY_SECONDS = 60` where the cause allows. A critical alert is sent at entry and then hourly.
  - The run seals only on `COMPLETE`, or after `tradepulse verification wind-down resolve --generation G --reason "<text>"`. That command records the operator's acceptance and seals with outcome `UNRESOLVED_ACCEPTED`, listing every open item in the handover.
- **Each failure has a defined behaviour:**

| Failure | Behaviour |
|---|---|
| definitive rejection | the intent is closed `REJECTED` with the broker reason; the position stays; `BLOCKED`; retried after a fresh census only if Rev.140 allows a new sell |
| timeout or unknown outcome | `SUBMISSION_UNKNOWN`; no second sell for that asset until reconciliation by client order ID proves a terminal state |
| broker unreachable | `BLOCKED`; nothing fabricated; reconnect and census on schedule |
| partial fill | the remainder follows §4.3 |
| crash mid-step | §6 |

## 6. Durability and crash recovery

### 6.1 Storage

The wind-down state lives in the generation's database, in a new append-only table `wind_down_events` with `BEFORE UPDATE`/`DELETE` triggers. It is checked at startup like Rev.140's tables (`SCHEMA_TRIGGER_MISSING`).
- **Columns:** event ID, generation, state, cause, census digest, request reason, and occurrence time.
- **The current state** is the latest event, derived on every read. There is no mutable state row.

### 6.2 After a crash or restart

Whether the cause is a crash, a power loss, a service restart or a signal, `run --resume` does these steps in order, before any scan lane starts:
1. Load the frozen `planned_end_at`, plus the latest `wind_down_events` row and any `end-run` request.
2. Run the existing startup reconciliation, including reconciliation of every `SUBMISSION_UNKNOWN` intent by its original client order ID.
3. Decide the phase:
   - If the current time is at or past `planned_end_at − lead`, or a wind-down state or request exists, enter wind-down at the recorded state. Scan lanes never start. Monitoring, settlement and reconciliation start.
   - Otherwise, the run continues normally.
4. In wind-down, take a fresh census before any new order. A sell is never generated because an earlier submission timed out. Only a reconciled terminal state permits the next sell.

The previous generation's accounting and its evidence boundary are never touched. A late fee is attributed through §4.5.

### 6.3 What recovery cannot cover

If the machine stays off, nothing runs. Crypto keeps trading, and no stop protects anything until the runtime returns. The same applies to carried equities and options in the gap between two runs. This is stated in the run plan. Broker-held backup stops (a future Rev) and cloud hosting are the mitigations.

## 7. Handover and next-run eligibility

### 7.1 The handover file

At seal, the generation writes `handover.json` into its directory. It is included in the sealed evidence digest and contains:
- `generation`, `outcome` (`COMPLETE` or `UNRESOLVED_ACCEPTED`), `sealed_at`, and the final census digest;
- `carried`: each `carry_adoptable` position, with its asset identity, quantity, average entry price, lot IDs and current stop (informational: the next run's adoption measures its stop from Alpaca's average entry price, per Rev.142);
- `residuals`: each `crypto_residual`, with its quantity and reason;
- `unproven`: each `unproven` position and each `foreign_open_order`, with its reason;
- `pending_fees`: each fill whose fee had not posted;
- `account_identity_digest`.

### 7.2 Freeze and startup of the next generation

- `tradepulse verification freeze` gains `--predecessor <generation>`. It is required when the most recently sealed generation in the store was frozen under Rev.147 (its manifest has a `wind_down` key). A generation sealed before Rev.147 has no handover and needs no predecessor. The named predecessor must be that most recently sealed generation, or freeze refuses with `predecessor_not_latest`.
- **Freeze refuses** in each of these cases:
  - the predecessor's handover is missing or its digest fails: `handover_missing` or `handover_digest_invalid`;
  - the predecessor's `unproven` list is not empty and not covered by an amendment (§7.3): `handover_unproven_items`;
  - the account identity differs: `handover_account_mismatch`.
- **When the opening checkpoint is captured,** the broker state must equal the handover plus amendments, exactly:
  - every broker position must be either a `carried` entry or a `residual` entry with the same asset and quantity (`handover_position_mismatch`);
  - there must be no open orders (`handover_open_orders_present`).

  Any mismatch refuses the freeze. Startup (`runtime_started`) repeats the same comparison against the bound checkpoint.
- **Carried positions** are then `opening_inventory` under Rev.142, so they are never classified `unexpected_external`. Their adoption rows record the predecessor generation and handover digest as provenance.
- **Residual crypto** is valued and reported, but neither managed nor adopted (TP145-02).
- **A `pending_fee` activity** that appears during the new generation is matched to the predecessor's list by fill ID and asset. It is excluded from the new generation's cash check and attributed to the predecessor. An unmatched fee activity is handled as today.

### 7.3 Operator amendment between runs

`tradepulse verification handover amend --predecessor G --reason "<text>" --position <asset>=<qty> | --resolve <item-id>`:
- It appends an operator-attributed amendment record (reason, time, OS user) next to the sealed handover: a new file `handover-amendments.jsonl`, append-only with a per-line digest chain. It never edits `handover.json`.
- Use 1: declare a deliberate change between runs. Example: the 1 inherited share bought for the next run's adoption test. The amended expectation is what §7.2 compares against.
- Use 2: record how an `unproven` item was resolved outside TradePulse. Each `--resolve` must cite the broker activity or order ID that proves it. The amend command verifies that ID against the broker's activity history (read-only) before appending.
- Every amendment is shown in the next generation's freeze output and copied into its manifest.

### 7.4 The first Rev.147 run

The account has been flat since 2026-10-29, and no Rev.147 handover exists. The first generation frozen under Rev.147 therefore has no predecessor. Freeze still requires an empty open-order list. Its opening positions are whatever the operator has deliberately arranged, such as the 1 inherited share, and Rev.142 adopts them as today.

## 8. Entry stop

In wind-down, `ExecutionGateway` refuses every BUY that is not a protective exit, with `RUN_WIND_DOWN_ACTIVE`. The check runs in the same fenced authorization as `RISK_APPROVED`, alongside Rev.146's freshness check, so a buy authorized just before wind-down began cannot be persisted after it. Scan lanes also idle in wind-down, but the gateway check is the guarantee.

## 9. Gates

- **G1 (read-only, before the build):** from the account's existing activity history, find whether past crypto sell fills have a USD CFEE activity and its delay. If there are no crypto sells in the history, record that, and keep §4.5's design unchanged.
- **G2 (the first real wind-down):** the first 24 h run under Rev.147 is the first live exercise. Its run plan lists the expected census, classification and final handover, and the run's acceptance criteria include `WIND_DOWN_COMPLETE`.

## 10. Tests

All on the simulator, with no broker calls.

- **Happy path:** crypto positions sold, entry orders cancelled, equities carried. The result is `COMPLETE`, and the handover lists the carried positions.
- **Partial fills:**
  - a partial fill of the wind-down sell, with the remainder sold after a terminal state;
  - a partial fill of an entry order during its cancel.
- **Rejection:** definitive rejection → `BLOCKED` → cleared → `COMPLETE`.
- **Unknown submission:** `SUBMISSION_UNKNOWN` on the sell. The test proves no second sell until reconciliation by client order ID, then completion.
- **Broker outage:** an outage through `planned_end_at` → `BLOCKED` → escalation to `UNRESOLVED` after 60 min. Then reconnect, completion, and no seal before completion.
- **Crypto maintenance:** an asset not tradable → `BLOCKED` → retried.
- **Crash at each state.** At each of `REQUESTED`, `EXECUTING`, `BLOCKED` and `UNRESOLVED`:
  - the restart resumes the state;
  - no scan lane starts;
  - no duplicate sell is sent;
  - the previous accounting is unchanged.
- **Signal:** SIGINT before the lead time stops the process without starting a wind-down. A restart continues the normal run.
- **Early end:**
  - `end-run` outside a session runs the wind-down;
  - `end-run` inside a session holds the seal until the session ends.
- **Late fee:**
  - inside the wait, it is recognized;
  - after the wait, it becomes `pending_fee`, is matched in the next generation, and is excluded from that generation's cash check.
- **Residuals and unproven positions:**
  - dust below `min_order_size` becomes `crypto_residual`;
  - unmanaged crypto, a quarantined liability and a short each become `unproven`, are never sold, and lead to `UNRESOLVED`.
- **Foreign open order:** never cancelled; leads to `UNRESOLVED`.
- **Next-run refusals:** each §7.2 refusal:
  - a predecessor that is missing or not the latest;
  - a digest that fails;
  - unproven items;
  - a position mismatch;
  - open orders present;
  - an account mismatch.
- **Amendments:**
  - one declaring 1 share permits freeze;
  - an amendment with an unverifiable `--resolve` ID is refused.
- **Entry stop race:** a BUY whose authorization straddles the wind-down start is refused inside the fenced transaction.
- **Frozen policy:** `generation_predates_wind_down`, and `frozen_configuration_mismatch` on a changed `planned_end_at`.
- **Property test:** over random sequences of fills, rejections, timeouts, outages and crashes, check that:
  - at most one open wind-down sell exists per asset;
  - `COMPLETE` implies broker crypto quantity is 0 or a recorded residual;
  - no state is ever reached from `UNRESOLVED` except through the operator command or a proven completion.

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
- The build happens on its own branch and worktree, after Rev.146 is merged. Nothing is merged without review and the operator's approval.

## 12. Open questions for review

1. **§3.3 (stop signals):** a stop signal never starts a wind-down, and an early end needs `end-run`. This differs from what was offered in chat, and the operator should confirm it.
2. **§4.3 (reporting):** should wind-down exits count in the strategy's edge statistics, or be excluded like adopted closes? This spec counts them, because they close the strategy's own positions at market. The `exit_reason` lets a report separate them.
3. **§4.5 (fee wait):** `FEE_WAIT_MINUTES = 15` is a guess until G1 measures the CFEE delay.
4. **§5 (escalation):** `WIND_DOWN_ESCALATE_MINUTES = 60` before an unresolved alert. A shorter value means earlier operator attention, with more false alarms during brief outages.
