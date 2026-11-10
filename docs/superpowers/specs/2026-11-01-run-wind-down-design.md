# Rev.147 — Verification run wind-down and generation handover

**Status:** design, rev. 8, for forensic approval. No production code changes and no orders until the spec is approved.
**Rev. 8 (2026-11-10):** answers the rev.156 review (REV156-01..04) and its two smaller points. The reviewer confirmed Q10 and Q11.
- **REV156-01, an amount match is not attribution.**
  - **Fee obligations:** each wind-down crypto sell fill gets one fee obligation, `fee_obligation:<fill_id>`.
  - **What closes one:** only a broker-identity link, which G1 shows Alpaca never gives for crypto, or the operator's `--resolve-fee`, validated by amount.
  - **What an amount match is:** evidence only. It is listed as candidates, and it never consumes an activity, closes an obligation, or makes fee accounting complete.
  - **The cash** of each fee activity is still booked once, as an unallocated account fee (§4.5, §4.5.1).
- **REV156-02, the deadline at dispatch.** The dispatch transaction reads the clock itself and compares it with the frozen `planned_end_at − lead`, independently of the watcher. Past the deadline, it refuses the BUY and records `WIND_DOWN_REQUESTED` in that same transaction (§8 step 1).
- **REV156-03, agreed calendar evidence.** Freeze fetches the broker calendar read-only and compares it with the Rev.144 calendar in memory. Every date from `planned_end_at` through `next_session_open` must be covered and `agreed`. The evidence is kept in the frozen manifest, not in the database, so the fresh-database rule is untouched (§4.5).
- **REV156-04, the durable pause before resume checks.** On any start that is not a wind-down, `run_paused` (cause `startup_recovery`) is recorded before the asynchronous resume checks. `run_resumed` is recorded only after every check passes, so a failed restart leaves the run paused, durably (§6.2).
- **Smaller points:**
  - the test planned-end fixture uses real exchange sessions;
  - G1's delays are described as observed `created_at` lag, not as a maximum delay before the API shows a fee.
**Rev. 7 (2026-11-10):** records G1, the read-only CFEE history read of the paper account (§9), and amends the fee wait to match it (§4.5, §7.6, §12 q. 3 and 11).
- **G1 measured:**
  - **Buys:** a crypto buy's fee is a native-coin CFEE ("Coin Pair Transaction Fee (Non USD)"), one per fill, 2–5 s after it.
  - **Sells:** a crypto sell's fee is a USD CFEE ("Coin Pair Transaction Fee (USD)"), one per fill, with `created_at` **4 h 16 min 42 s** after it. That is the observed timestamp lag, not a maximum delay before the API shows the fee (rev. 8). It is 0.25% of quantity × price, rounded up to the cent.
  - **No link:** neither carries a `fill_id` or an `order_id`, and the USD CFEE has no symbol.
  - **Sample:** 1 sell order with 2 fills, and 2 buy orders with 4 fills. The evidence is in `~/tradepulse-evidence/2026-11-10-rev147-g1/`.
- **Consequences:**
  - The provisional 15-minute wait would seal every wind-down before its crypto sell fees post.
  - §7.6's linked-fee rule can never match a crypto fee, so every late one would need an operator `--resolve-fee`.
  - **Rev. 7:** the wind-down waits up to `CRYPTO_SELL_FEE_WAIT_HOURS = 8` for each of its crypto sells' USD fees before sealing, so in the normal case the fee posts inside its own generation. Freeze refuses a `planned_end_at` that leaves too little time before `next_session_open`.
  - **The amount** corroborates the fee; it never attributes it.
- **Operator decisions on rev. 7 (2026-11-10):**
  - **Q11 decided:** an unmatched crypto USD fee needs the operator's `--resolve-fee`. The exact amount cannot establish ownership on its own, because separate fills can produce identical rounded fees. The computed amount *validates* an operator attribution, and never makes one.
  - **The 8-hour wait is a timeout, not proof of fee completeness.** When it expires, the run's fee accounting is explicitly `incomplete`: each missing fee is listed, and nothing describes the fees as complete.
  - **The 9-hour rule** is checked against the authoritative next session: the Rev.144 calendar, with its broker cross-check fresh, and with holidays, early closes and daylight-saving changes tested explicitly.
**Rev. 6 (2026-11-07):** answers the Rev.151 review of rev. 5 (REV151-01..03) and records the reviewer's decisions on open questions 7–9 (see `docs/rev147-rev6-review-response.md`).
- **REV151-01, an abandoned head:** after `abandoned`, the effective head is the latest `sealed` or `genesis` entry before it. Abandon now needs broker proof, not only an empty intent table: no broker order after the generation's freeze, and no uncited FILL since the effective head. It also needs proof that the database is the frozen one and intact, and that the generation never took over protection. A database that is damaged or lost has a broker-only path. An abandoned generation can never run, freeze or seal again (§7.7).
- **REV151-02, a delayed POST:** a not-found lookup by client order ID never ends an intent that may have been POSTed. Today the stranded sweep does exactly that after 120 s (`reconciliation/coordinator.py::_resolve_stranded`). This revision withdraws that rule, in the build and for every generation, not only during the wind-down:
  - a `SUBMITTED` or `SUBMISSION_UNKNOWN` intent stays unresolved, with its asset blocked, until the broker shows the order;
  - otherwise, after a finality horizon, an operator resolution backed by broker evidence ends it;
  - its client order ID stays watched afterwards.
  
  A deterministic delayed-POST test covers this (§8 step 7, §10).
- **REV151-03, an unverified mark:** a `mark_unverified` position gets no price-dependent decision (stop, target, trailing ratchet, scale-out) from any unaccepted price. Its time and expiry checks continue. Its price-dependent evaluation starts at its first accepted executable quote, and every exit still passes the gateway's quote check (§7.5.1).
- **Decisions:**
  - Q7: a per-machine registry is accepted, with a hard operational restriction (one machine per account);
  - Q8: the per-position assumption is accepted with the REV151-03 boundary;
  - Q9: automatic reversal credits are accepted, only with proven linkage to the original fee (§7.6, §12).
**Rev. 5 (2026-11-06):** answers the five design findings D01–D05 of the 2026-11-04 forensic audit (`TradePulse_Rev147_Forensic_Audit.pdf`). Its runtime findings F01–F06 were fixed separately in Rev.149, merged as main `b010090d`. Each finding was checked against the code at that commit before it was answered (see `docs/rev147-rev5-review-response.md`).
- **D01, predecessor discovery:** a generation registry per broker account and environment, outside every database, with a digest-chained head, crash-safe appends, a bootstrap rule, and refusal on a missing, forked, stale or unsealed head (§7.7).
- **D02, late-fee cash:** a signed, exactly-once predecessor-cash bridge entry in the successor. It is included in the cash check and excluded from the successor's equity and P&L, like a capital flow (§7.6.1). Reversals are bridged too.
- **D03, carried lots:** a versioned carried-lot opening contract. Each carried lot gets one adoption row with a new successor identity, and keeps its original price, date and provenance chain. Its quantity is re-based to what remains. Opening proof and exclusions are aggregated per asset, and the carried target survives re-projection (§7.2.2).
- **D04, entry race:** a serialized entry-dispatch barrier in the database. It is the RISK_APPROVED → SUBMITTED write, shared by the gateway and the wind-down transition, and every BUY dispatched before the transition is tracked to a terminal state (§8).
- **D05, mark evidence:** a mark evidence schema with separate observed and received times and a session label. A prior-session mark is accepted for an out-of-session start, and a position without an acceptable mark gets a per-position assumption, so it is never certified (§7.5.1).
**Rev. 4 (2026-11-04):** answers the rev. 3 assessment (with the Rev.148 archive). The reviewer gave two conditions for approval:
- **Monitoring continuity (open question 5), accepted with conditions:** strict successor-start verification before the successor takes on protection, an explicit exposure-interval record in every case, and a gap audit that only detects and requires intervention. It never claims to restore protection that was absent (§7.5).
- **Options expiring between generations (open question 6), decided:** an option that can expire before monitoring resumes is `unproven`, which blocks completion until the operator resolves it. An option whose expiry is unknown is treated the same way (§4.2).
- **Fee wait (open question 3):** unchanged. `FEE_WAIT_MINUTES` stays provisional until G1 (§4.5, §9).
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
| `carry_adoptable` | long equity or option, fully managed by this run (holding and lots match the broker quantity), with a valid protective state: `stop_loss` present and, if `current_stop` is set, `current_stop ≥ stop_loss`; for an option, valid expiry metadata and an expiry date after the next regular session's date (see the option rule below) | none; protective exits stay active until the end; recorded in the handover with the full protective state |
| `close_crypto` | long crypto, fully managed by this run, Rev.140 sellable = broker quantity | sell the sellable quantity through the gateway (§4.3) |
| `crypto_residual` | long crypto managed by this run, quantity > 0 but below the asset's `min_order_size`, or not a multiple of `min_trade_increment` after a sell, and its local lots' remaining quantity equals the broker quantity | none (unsellable); recorded as a residual |
| `unproven` | everything else, including: partially managed; quarantined; a short or a Rev.140 liability; unmanaged crypto; an adopted asset with an `ADOPTED_EXIT_REFUSED` outstanding; a managed equity or option with a missing or invalid protective state (`carried_stop_missing`, `carried_stop_invalid`); an option that can expire before monitoring resumes or whose expiry is unknown (`carried_option_expires_unmonitored`, `carried_option_expiry_unknown`); residual dust whose local lots disagree with the broker | none, ever; a critical alert; recorded; makes the wind-down `UNRESOLVED` and blocks the next run until resolved (§7.3) |

**Option rule (rev. 4, open question 6 decided).** Under §7.5, monitoring resumes at the earliest at `next_session_open`. In the planned case, that is the start of the successor's first regular session. An option the successor cannot monitor through its own expiry is not safely adoptable:
- **The test:** a long option is `unproven`, reason `carried_option_expires_unmonitored`, when its expiry date (`asset.metadata["expiry"]`) is on or before the NY date of `next_session_open`. That date comes from the Rev.144 calendar, the same value the handover records (§7.1).
  - **Why the expiry day itself counts:** even a planned `no_session_gap` start puts the successor's only chance to act on that same session. A late start or a refused startup would let the option expire unmonitored, and the existing near-expiry exit (`monitor/coordinator.py::_near_expiry`) would have no session to act in. The rule therefore does not depend on the successor starting on time.
- **Unknown expiry:** an option whose expiry metadata is missing or malformed (the monitor's existing `_option_expiry_metadata_invalid`) is `unproven`, reason `carried_option_expiry_unknown`. Unknown is not treated as safe, which matches the monitor's own stance.
- **No sale is attempted.** The wind-down runs outside a session (§3.1), so it cannot sell the option, and Rev.147 adds no options-selling path. It never exercises or closes the option either.
- **Effect:** like every `unproven` item, the option makes the wind-down `UNRESOLVED`. That outcome is not `COMPLETE`, and it blocks the next run until the operator resolves the option outside TradePulse and records the resolution with a broker-verified `--resolve` (§7.3). The resolution is a closing trade, an exercise or a broker-reported expiry.
- **Normally unreachable:** during the run, the near-expiry exit closes options within `min_days_to_expiry` while sessions remain. The rule is the backstop for an option that the run could not close, for example after an outage or a failed exit.
- **The calendar must be verified.** If the Rev.144 calendar cannot verify `next_session_open`, every carried option is `unproven` with `carried_option_expires_unmonitored`, because a safe date cannot be proven.

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

**The wait (rev. 7, measured by G1).** For each wind-down fill, the wind-down waits for its fee activity before sealing. Any activity already visible is used.
- **A crypto buy's native-coin fee** posts within seconds, through the existing asset-fee path. It is waited for up to `FEE_WAIT_MINUTES = 15`.
- **A crypto sell's USD fee:** in the one measured case, its `created_at` was 4 h 16 min 42 s after the fill. That is an **observed timestamp lag**, not a maximum delay before the API shows the fee (rev. 8). The wind-down waits up to `CRYPTO_SELL_FEE_WAIT_HOURS = 8` after its last crypto sell fill.
  - A fee that posts inside the wait posts inside its own generation. The existing membership rules class it `in_generation`, and the existing exactly-once ledger books its cash once as an `unallocated_account_fee`. Booking the cash does **not** prove which fill the fee belongs to (§4.5.1).
  - **Provisional,** because the sample is one sell order. Each run's record states the measured delays, and the constant is revised when they say so.
- **What the waits are:** they only decide how long the run waits before sealing with a fee still pending. They are timeouts, not settlement boundaries, and they never declare fee accounting complete (operator decision, 2026-11-10).
  - **An expired wait leaves an explicit unresolved state.** The handover and the run record carry `fee_accounting: "incomplete"`, listing each `pending_fee` with the reason `fee_wait_expired`. The run report says that fee accounting is incomplete. It never describes fee accounting as complete while a pending fee is open.
  - **`fee_accounting` becomes `"complete"`** only when every pending fee is attributed (§7.6) or resolved (§7.3). That happens in the predecessor's `post_seal_adjustments`, and never by time.
  - The inventory outcome (`COMPLETE` or `UNRESOLVED_ACCEPTED`) and `fee_accounting` are reported separately, so a proven inventory never implies proven fees.
- **Fitting the wait into the gap (freeze check):** `planned_end_at` + 30 min + `CRYPTO_SELL_FEE_WAIT_HOURS` + `SUCCESSOR_START_MARGIN_MINUTES` (30) must be no later than `next_session_open`. That is 9 hours before the next open. Otherwise freeze refuses with `wind_down_end_fee_window_insufficient`.
  - **The authoritative next session.** `next_session_open` is the first XNYS session open strictly after `planned_end_at`, from the Rev.144 calendar, with all times in UTC. A holiday is skipped (it has no session), and daylight-saving changes are absorbed by the UTC session times. An early close moves the session's close, and with it the "inside a session" test, but not the next open.
  - **The broker must agree (rev. 8, REV156-03).** A fresh check alone is not enough: the age test (`CalendarGate.fresh`) says nothing about completion, agreement or coverage, and a fresh database holds no calendar evidence, because `_require_fresh` refuses populated tables. Freeze therefore takes its own evidence:
    - **The read:** read-only, with the broker it already uses for the opening checkpoint, freeze calls `broker.get_calendar(ny_date(now), ny_date(next_session_open))`.
    - **The comparison:** it runs the existing pure comparison (`calendar/checks.py::compare`) in memory.
    - **What must hold:** every date from `ny_date(planned_end_at)` through `ny_date(next_session_open)` is in the comparison's coverage with result `agreed`, including holidays, where both sides have no session. A missing date, a `contested` date, or a failed fetch refuses with `wind_down_end_calendar_unverified`.
    - **Where it is kept:** the request range, the response's SHA-256 and the per-date rows are frozen in the manifest as `policy.wind_down.calendar_evidence`. Nothing is written to the database, so the fresh-database rule is untouched.
    - **Mismatch check:** `Verification.check` compares only the policy constants and `planned_end_at` for `frozen_configuration_mismatch`, never this evidence.
  - **Examples:**
    - a weekday end at 16:30 ET leaves 17 hours, and passes;
    - Monday 2026-11-16 at 00:31 ET is refused;
    - an end on the Wednesday before Thanksgiving (2026-11-25, 16:30 ET) skips the holiday, so the next open is Friday's early-close session at 09:30 ET on 2026-11-27, and it passes;
    - an end on Friday 2026-11-27 at 13:30 ET, after that early close at 13:00 ET, is outside the session, and passes;
    - after the 2027-03-14 daylight-saving change, Monday 2027-03-15 at 00:30 EDT is exactly 9 hours before that day's 09:30 EDT open (13:30 UTC, an hour earlier in UTC than the week before), and passes. 00:31 EDT is refused.
- **Amounts are evidence, not attribution (rev. 8, REV156-01):** see §4.5.1. No amount computation closes an obligation or consumes an activity.
- **Fee recognized in time:** recorded in the run's accounting as usual, through the existing exactly-once generation cash ledger. Its cash entry ID is `broker:fee:<activity_id>` (`reconciliation/generation_fees.py`).
### 4.5.1 Fee obligations and what closes them (rev. 8, REV156-01)

**An obligation per fill.** Each wind-down fill whose fee is not proven by broker identity gets one durable obligation, `fee_obligation:<fill_id>`. In practice that is every crypto sell fill, because G1 shows no crypto CFEE carries a link. It records:
- the fill, the asset, the fill's quantity and price;
- the frozen `crypto_fee_rate`, and the expected amount −ceil_cent(quantity × price × rate);
- the state: `open` or `closed`.

**What closes an obligation.** Only one of these:
1. **A broker-identity link:** an activity whose `fill_id`, or `order_id` with an exact split under the existing `closing_fee_allocation` rule, names the fill. G1 shows this never happens for crypto today.
2. **An operator `--resolve-fee <obligation_id> --activity <id>`** (§7.3), validated by amount. The activity's `net_amount` must equal the expected amount (`fee_attribution_amount_mismatch` otherwise). The activity must not already close another obligation (`fee_activity_already_attributed`); its ID is the attribution key, so it is unique across every generation's attributions.

**What does not close an obligation:**
- **An amount match:** a fee activity whose amount equals the expected amount is recorded as a **candidate** (`fee_amount_candidates`, a list of activity IDs) on every obligation it matches. A candidate is evidence for the operator. It never consumes the activity, never closes the obligation, and never makes `fee_accounting` complete.
- **The cash booking:** the cash of every fee activity in the generation is booked exactly once, as an `unallocated_account_fee` (`broker:fee:<activity_id>`). That keeps the cash invariant exact, but proves nothing about which fill the fee belongs to.
- **Time:** an expired wait closes nothing (operator decision, rev. 7).

**What the candidates look like:**
- **Two equal expected fees, one activity:** both obligations stay `open`, and both list the same single activity as a candidate. Resolving one with it succeeds. Resolving the other with the same activity refuses with `fee_activity_already_attributed`.
- **An unrelated or predecessor fee of the same amount:** it is listed as a candidate, and it closes nothing. If it is already attributed elsewhere, it is listed with `already_attributed`.
- **Repeated reconciliation:** the obligations and candidates are recomputed from immutable rows on every pass, never accumulated. One activity never satisfies two obligations, because only an attribution record closes one, and attributions are unique per activity.

**At the seal:**
- `fee_accounting` is `"complete"` only when every obligation is `closed`. Otherwise it is `"incomplete"`.
- The handover lists each open obligation:
  - as `unattributed_fee` when one or more candidates were booked inside the generation. The cash is already in this generation, so the later resolution is attribution only, with no cash entry;
  - as `pending_fee` (reason `fee_wait_expired`) when no candidate posted. Its cash may arrive in the successor, where §7.6 and §7.6.1 apply after `--resolve-fee`.
- The inventory outcome is reported separately from `fee_accounting`.

**Fee not yet posted:** when a wait expires, the fee is recorded in the handover as a `pending_fee` (§7.1). It does not block `WIND_DOWN_COMPLETE`. It stays a durable predecessor obligation until it is attributed (§7.6) or resolved by the operator (§7.3). No timeout closes it.
  - **A crypto USD fee can only be attributed by the operator** (Q11, decided 2026-11-10). G1 shows it carries no link, so §7.6's linked-fee rule never matches it, and `--resolve-fee` is the only way to attribute it. The run plan says this. The 8-hour wait exists so that the path is the exception.
  - **The computed amount validates the attribution.** For a crypto sell's pending fee, `--resolve-fee` also requires the activity's `net_amount` to equal −ceil_cent(fill quantity × fill price × the frozen `crypto_fee_rate`). Otherwise it refuses with `fee_attribution_amount_mismatch`. The amount never selects the fill: the operator names the pending fee and the activity, and the command only checks them.

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
7. **Fills accounted:** every wind-down fill is settled and attributed. Each fill's fee obligation is closed (§4.5.1), or listed as `unattributed_fee` or `pending_fee` with `fee_accounting: "incomplete"`. An open fee obligation never blocks the inventory outcome, and it is never reported as complete.

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
   - **Otherwise,** the run is `PAUSED` (or crashed). **Rev. 8 (REV156-04):** before any asynchronous check, the startup durably records the entry block. If the run state is `RUNNING`, meaning no event or a latest `run_resumed` (the crash case), it records `run_paused` with cause `startup_recovery`, through the transition write of §8 step 2, which also rejects any stale `RISK_APPROVED` BUY. A `WINDING_DOWN` state is preserved and never overwritten. The run returns to `RUNNING` only after all of these pass, in order. Any failure leaves the durable `PAUSED` state and refuses with the existing reason:
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
  - `lots`: for each open lot, `lot_id`, `originating_fill_id`, `opened_quantity`, `remaining_quantity`, `acquisition_price`, `opened_at`, `mfe_price` and `mae_price`, and (rev. 5, D03) its full history: `closures` (fill ID to quantity), `asset_fee_quantities`, `realized_pnl`, and `provenance_chain` (§7.2.2). The history is evidence only. The successor never re-applies it;
  - `protective_state_digest`: SHA-256 over the canonical encoding of `holding` and `lots`, history included;
  - `stop_provenance`:
    - the source generation;
    - the holding and lot record IDs;
    - the stop's origin: `entry` for a stop from this run's own entry, or `inherited:<generation>` for a stop carried in from an earlier handover;
  - `adoption_policy_version: "rev147-handover-v1"`.
- **`residuals`:** each `crypto_residual`, with its quantity, its lot IDs and the reason.
- **`unresolved`:** each `unproven` position and each `foreign_open_order`, with its class, quantity and reason. It is empty unless the outcome is `UNRESOLVED_ACCEPTED`.
- **`fee_accounting`** (rev. 8): `complete` or `incomplete` (§4.5.1).
- **`unattributed_fees`** (rev. 8): each open obligation whose cash was booked inside the generation. Each lists `fee_obligation_id`, the fill, the expected amount and `fee_amount_candidates`.
- **`pending_fees`:** for each fill whose fee had not posted:
  - `pending_fee_id`;
  - the local fill ID, the broker fill (activity) ID and the broker order ID;
  - the asset, the fill time and the expected kind (USD CFEE for a crypto sell, FEE for a regulatory equity fee);
  - `origin_generation`. This is this generation, or the earlier one for a still-open fee inherited through a chain (§7.6).
- **`watched_client_order_ids`** (rev. 6, REV151-02): each client order ID resolved by `resolve-dispatch` and still inside its 30-day watch, with its intent, `dispatched_at`, its resolution digest and its origin generation (§8 step 7). The successor keeps watching it. Rev.154 stores the watch in `resolved_dispatch_watches`. Carrying it into the handover is still Rev.147 work.
- **`protection`** (§7.5):
  - `monitoring_released_at`: when the monitor loop stopped, which is before the seal;
  - `next_session_open`: from the Rev.144 calendar;
  - `carried_count`.

### 7.2 Freeze and startup of the next generation

- `tradepulse verification freeze` gains `--predecessor <generation>`. **Rev. 5 (D01):** the predecessor is found through the account's generation registry (§7.7), never through the new database's store, which is always empty. `--predecessor` is required when the registry head is a sealed generation frozen under Rev.147 (its manifest has a `wind_down` key); omitting it refuses with `predecessor_required`. The named predecessor must be the registry head, or freeze refuses with `predecessor_not_latest`. The registry rules of §7.7 run before every check below.
- **Freeze refuses** in each of these cases:
  - the predecessor's handover is missing or its digest fails: `handover_missing` or `handover_digest_invalid`;
  - the predecessor's `unresolved` list is not empty and not covered by an amendment (§7.3): `handover_unproven_items`;
  - the account identity differs: `handover_account_mismatch`;
  - a carried entry's protective state fails validation (§7.2.1): `handover_stop_invalid` or `handover_stop_unverified`.
- **When the opening checkpoint is captured,** the broker state must equal the handover plus amendments, exactly:
  - every broker position must be either a `carried` entry or a `residual` entry with the same asset and quantity (`handover_position_mismatch`);
  - there must be no open orders (`handover_open_orders_present`).

  Any mismatch refuses the freeze. Startup (`runtime_started`) repeats the same comparison against the bound checkpoint, and then applies §7.5.
- **Carried positions** are then `opening_inventory` under Rev.142, so they are never classified `unexpected_external`. They are adopted through §7.2.1 and the carried-lot contract of §7.2.2, not through Rev.142's broker-average reconstruction.
  - Their lots have `origin = "adopted"`, so their closes stay out of the successor's strategy edge, exactly as Rev.142 does.
  - Their adoption rows, one per carried lot (§7.2.2), record the predecessor generation, the handover digest and the `protective_state_digest` as provenance.
- **Residual crypto** is valued and reported, but neither managed nor adopted (TP145-02).
- **Pending fees** follow §7.6.

### 7.2.1 Restoring the protective state (REV147-R2-01)

For a carried position, the successor's adoption takes the protective state from the handover. It does not recompute a stop.
- **Holding:** the adopted holding takes `stop_loss`, `current_stop`, `target_price`, `scaled_out` and `average_price` exactly from the handover `holding`.
- **Lots:** each adopted lot takes `acquisition_price`, `opened_at`, `mfe_price` and `mae_price` from the handover lot. Its identity, quantities and opening proof follow §7.2.2.
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
  6. (rev. 5, D03) Each lot's history conserves quantity, so `opened_quantity = remaining_quantity + Σ closures + Σ asset_fee_quantities`. Also, Σ(`remaining_quantity` × `acquisition_price`) / Σ `remaining_quantity` over the lots equals `holding.average_price` exactly, in Decimal. This is the value the successor's projection will recompute (§7.2.2). A failure gives `handover_stop_unverified`.
- **Cost basis.** `broker_avg_entry_price` may differ from `average_price` (for example, Alpaca's averaging after a partial exit). This difference is recorded in the adoption row and does not refuse adoption, because the protective state comes from TradePulse's own fills, which the predecessor's sealed evidence proves.
- **No amendment can set or change a stop.** An operator who doubts a carried stop closes the position outside TradePulse, then amends the expected quantity with broker proof (§7.3).
- **Successor startup:** a carried position whose mark is at or below its inherited `effective_stop` exits on the first regular-session monitor cycle. This is the existing behaviour, and it gets no special case.

### 7.2.2 The carried-lot opening contract (rev. 5, D03)

**Problem.** A literal copy of a predecessor lot conflicts with the current model in four places:
- **Conservation:** `PositionLot` requires `opened_quantity = remaining_quantity + Σ closures + Σ asset_fee_quantities`. If `opened_quantity = 10` and `remaining_quantity = 6` are copied without the closure map, conservation fails. Copying the closure map instead would point at fills that the successor database does not have.
- **Origin:** `PositionLot.origin` is derived from the `adopted:` / `adoption:` prefixes. A lot that kept its original fill IDs would be `origin = "fill"`, and its opening proof (`settlement/accounting.py::_opening_evidence`) would look for an original fill that the successor does not have (`ACCOUNTING_OPENING_FILL_MISSING`).
- **Opening proof:** Rev.142's `_AdoptedOpening` requires `adoption.broker_avg_entry_price = lot.acquisition_price`, `adoption.adopted_quantity = lot.opened_quantity` and `adoption.adopted_at = lot.opened_at`. A carried lot keeps its original date and price, so it fails all three.
- **Aggregation:** `adoption/accounting.py::adopted_opening_exclusions` assumes one adopted lot per asset and overwrites `exclusions[key]`, so a second carried lot for the same asset replaces the first.

A fifth conflict was found while checking the code. On any successor settlement for the asset, `settlement/engine.py::_project_holding` rebuilds the holding. It recomputes `average_price` from the open lots and takes the targets from `_entry_protective_levels`, which returns `target_price = None` for an adopted lot. The carried target would be lost on the first successor fill in that asset.

**The contract.** Each carried lot becomes one versioned opening record in the successor.
- **One adoption row per lot,** not per asset, with `classification = "opening_inventory"`, `source_id` = the successor's opening checkpoint ID, and `adoption_policy_version = "rev147-handover-v1"`. Rev.142 rows are unchanged and keep their own rules.
- **Successor identity:** a new `adoption_id` per lot. The lot is `lot_id = "adopted:<adoption_id>"` and `originating_fill_id = "adoption:<adoption_id>"`, the existing prefixes, so `origin = "adopted"` without any model change, and its closes stay out of the successor's edge.
- **Successor quantities:** `opened_quantity` = the predecessor lot's `remaining_quantity`, and `remaining_quantity` is the same value. `closures` and `asset_fee_quantities` start empty and `realized_pnl` starts at 0, so conservation holds in the successor from its first row.
- **Kept from the predecessor:** `acquisition_price` (the original), `opened_at` (the original, so time and stall exits keep counting from the real entry), `mfe_price` and `mae_price`.
- **Immutable original provenance:** the adoption row's `carried` block holds:
  - `provenance_chain`: the root generation, root `lot_id` and root `originating_fill_id`, then one hop per handover (the generation, the lot ID in that generation, the handover digest);
  - `history`: the predecessor lot's `opened_quantity`, `closures`, `asset_fee_quantities` and `realized_pnl`. This is evidence only. Those closures, fees and results were settled in the generation that recorded them and are never counted again;
  - `original_acquisition_price`, `original_opened_at` and `carried_quantity`;
  - `adopted_at`: the successor's adoption time, which is separate from `original_opened_at`.
- **Versioned opening proof:** `_AdoptedOpening` branches on `adoption_policy_version`. For `rev147-handover-v1` it checks `lot.acquisition_price = carried.original_acquisition_price`, `lot.opened_quantity = carried.carried_quantity` and `lot.opened_at = carried.original_opened_at`. It never compares the broker average or the adoption time with the lot. A mismatch is the existing `ACCOUNTING_ADOPTION_LOT_MISMATCH`. The independent projection in `verification/evidence.py` applies the same branch.
- **Successor basis:** the successor's P&L on carried inventory stays excluded through the existing exclusion X = R × mark + N − V0, where V0 is the opening checkpoint's market value. So the successor never needs a predecessor fill. Attribution rows keep `broker_basis_pnl` (broker average) and `managed_pnl` (adoption mark), and add `original_basis_pnl` from the original acquisition price, for reporting only.
- **Per-asset aggregation:** `adopted_opening_exclusions` sums over every adopted lot of an asset and subtracts V0(k) once: X(k) = Σ_lots (R × mark + N) − V0(k). The corruption guard becomes Σ `carried_quantity` (or the Rev.142 `adopted_quantity`) per asset = the checkpoint quantity (`ADOPTION_CHECKPOINT_MISMATCH`). The independent assessment aggregates the same way.
- **Re-projection keeps the protective state:**
  - `average_price`: `_project_holding` recomputes it from the lots' original prices and remaining quantities. §7.2.1 check 6 proves at freeze that this equals the handover's `average_price`.
  - `stop_loss` and `target_price`: for a `rev147-handover-v1` adoption, `_entry_protective_levels` returns the adoption row's `stop_loss` and `target_price`. The rule is still `first_entry`, so the oldest carried lot governs.
  - `current_stop` and `scaled_out`: carried forward by the existing `mutate`, as today.
- **A second inheritance** (a lot carried again) appends one hop to the chain and keeps the root unchanged. Its stop provenance stays `inherited:<generation>` (§7.1).
- **Native asset fees:** carried lots are equities and options, which normally have none. If a predecessor lot has `asset_fee_quantities`, they are kept in `history`. Its `remaining_quantity` is already net of them, and the successor lot starts with none.

### 7.3 Operator amendment between runs

`tradepulse verification handover amend --predecessor G --reason "<text>"`, with one of these:
- `--position <asset>=<qty>`;
- `--resolve <item-id>`;
- `--resolve-fee <pending_fee_id> --activity <id>`;
- `--accept-gap`;
- `--ack-gap-breach <asset>` (rev. 4; this is written to the **successor's** amendment chain, because the hold belongs to the successor; see §7.5).

How it works:
- It appends an operator-attributed amendment record (reason, time, OS user) next to the sealed handover: a new file `handover-amendments.jsonl`, append-only with a per-line digest chain. It never edits `handover.json`.
- **Use 1:** declare a deliberate change between runs. Example: the 1 inherited share bought for the next run's adoption test. The amended expectation is what §7.2 compares against. A newly declared position is not a carried one: it has no inherited protective state and goes through ordinary Rev.142 adoption. **Rev. 5:** the declaration cites the broker order or activity IDs of the fills that made the change, verified read-only, because §7.7's stale-head check requires every fill after the seal to be cited.
- **Use 2:** record how an `unresolved` item was resolved outside TradePulse. Each `--resolve` must cite the broker activity or order ID that proves it. The amend command verifies that ID against the broker's activity history (read-only) before appending.
- **Use 3:** `--resolve-fee` attributes an unlinked fee to a pending fee (§7.6).
- **Use 4:** `--accept-gap` accepts a protection gap (§7.5). It accepts the gap only. It does not waive successor-start verification.
- **Use 5:** `--ack-gap-breach` ends the gap-breach hold for one position (§7.5).
- Every amendment is shown in the next generation's freeze output and copied into its manifest.

### 7.4 The first Rev.147 run

The account has been flat since 2026-10-29, and no Rev.147 handover exists. The first generation frozen under Rev.147 therefore has no predecessor, and the account has no registry yet. Its freeze bootstraps the registry with `--registry-bootstrap --reason "<text>"` (§7.7). Freeze still requires an empty open-order list. Its opening positions are whatever the operator has deliberately arranged, such as the 1 inherited share, and Rev.142 adopts them as today, with a stop rebuilt from the broker's average entry.

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
2. **The successor** becomes responsible at `monitoring_assumed_at`. It may record that time only after **successor-start verification** passes (rev. 4, below). The successor records it in its `runtime_started` evidence.

**Successor-start verification (rev. 4, the reviewer's first condition).** All of the following must pass, in this order, inside the successor's startup and before `monitoring_assumed_at` is recorded. Any failure refuses the startup with `handover_assumption_unverified`, names the failed check, and raises a critical alert. No `monitoring_assumed_at` is written, the runtime does not start, the continuity class is not computed, and the exposure stays open (below). No amendment can waive these checks.
1. **Checkpoint comparison:** the §7.2 startup comparison against the bound opening checkpoint passes.
2. **Fresh census:** a new census at assumption time, using the §4.1 capture pattern (activities, then account, positions and open orders, then activities again), equals the handover plus amendments. The positions and quantities must match, there must be no open orders, and no new position-changing activity on a carried asset since the checkpoint. That means a fill or a quantity-changing corporate action, such as a split, a spinoff or an option exercise or assignment. Cash-only activities, such as a dividend (Rev.143) or a fee (§7.6), do not fail this check, and the existing membership rules classify them. This covers the time between freeze and startup, when the account could have changed.
3. **Restored state:** for each carried position, the successor's own adopted holding and lot rows are re-read from its database after adoption. A `protective_state_digest` recomputed from those rows must equal the handover's. This proves that what the monitor will use is what the predecessor sealed, not just what the handover file says.
4. **Mark evidence (rewritten in rev. 5, D05):** each carried position gets a mark evidence record under §7.5.1. The check fails only on what §7.5.1 calls a failure: no mark source at all, or a timestamp ahead of the clock beyond the skew allowance. A position whose mark is not acceptable is not certified. It gets a per-position assumption (§7.5.1). The startup never assumes a mark, and it never treats a receipt time as a market time.
5. **One full evaluation:** the monitor completes one evaluation of every carried position with the restored state: effective stop, ladder, time and stall exits, and the near-expiry rule. Every exit the evaluation decides on is submitted through the gateway before `monitoring_assumed_at` is written, and its intent state is recorded. Outside a session, equity and option exits are not decided, as today, so the evaluation records `out_of_session` for those positions. It does not treat them as protected-and-checked.

A failed check is never retried silently into a pass. The operator restarts the successor after fixing the cause, and the whole sequence runs again from step 1.

**Exposure record (rev. 4, the reviewer's second condition).** The unmonitored interval is recorded explicitly in **every** case, including `no_session_gap` and a refused startup.
- **The interval:** `[monitoring_released_at, monitoring_assumed_at)`. Until the successor writes `monitoring_assumed_at`, the record has `assumed_at = null` and `status = "open"`. An open exposure is never closed by time.
- **Its contents** (`protection_exposure`):
  - `released_at`, `assumed_at`, the duration, and `next_session_open`;
  - `sessions_unmonitored`: each XNYS regular session, or part of one, that falls inside the interval, from the Rev.144 calendar. It is empty for `no_session_gap`;
  - for each carried position: the asset, the quantity, the `effective_stop` at release, and for options, the expiry;
  - the residual crypto, listed as unmanaged in both generations (TP145-02), because crypto trades through the interval;
  - the continuity class, and the gap-audit results (item 5);
  - the successor-start verification result for each check.
- **Where it is written:**
  - in the successor's `runtime_started` evidence, inside its sealed digest at its own seal;
  - in the predecessor's `handover-amendments.jsonl`, as a **system-written** `protection_exposure` record in the same digest chain as §7.6. The sealed `handover.json` is never edited.
  - A refused startup appends a `protection_exposure` record with `status = "open"` and the refusal reason, so a failed takeover is recorded on the predecessor's side too.
- **Reporting:** the predecessor's reports show the exposure in `post_seal_adjustments`. The successor's run report shows it in a `protection_exposure` section. Neither report describes the interval as protected.
3. **Continuity class**, recorded in the successor's start record and reported in both generations' evidence:
   - `no_session_gap`: `monitoring_assumed_at` < `next_session_open`. Protection is equivalent to a continuous run. The interval is still recorded.
   - `session_gap`: otherwise. The record lists each regular session, or part of one, during which no monitor ran.
   - `no_carried_positions`: `carried_count = 0`. There is nothing to protect.
4. **Operator acceptance.** If the successor's startup would produce `session_gap`, startup refuses with `handover_protection_gap_unaccepted`, unless an `--accept-gap` amendment exists. That amendment is appended after the predecessor's `next_session_open` has passed, and it states the operator's reason. A `session_gap` start then records the accepted interval and the amendment ID.
5. **Gap audit.** On a `session_gap` start, for each carried position, the successor compares the regular-session low of each missed session (from the existing bar provider) against the inherited `effective_stop`.
   - **What it cannot do (rev. 4):** the audit detects only. It cannot recreate protection that was absent, undo a loss that happened while no monitor ran, or show what a continuous run would have filled. Its results are reported as findings about the interval, never as protection.
   - **Breach:** if any low was at or below the stop, it raises a critical alert, `CARRIED_STOP_BREACHED_UNMONITORED`, and reports it in the run evidence and in the exposure record. The exit itself is the normal one (§7.2.1).
   - **Unverified:** if bars are unavailable or incomplete, the audit records `gap_audit_unverified` for that position. It does not guess, and an unverified result is handled like a breach (below).
   - **Required intervention (rev. 4):** a breach or an unverified audit puts the successor in a **gap-breach hold**:
     - protective exits run normally, so a position at or below its stop still exits on the first regular-session cycle;
     - `ExecutionGateway` refuses every non-protective BUY with `GAP_BREACH_UNACKNOWLEDGED`, in the same fenced authorization as `RUN_WIND_DOWN_ACTIVE` (§8);
     - the hold ends only through an operator `--ack-gap-breach <asset>` amendment for each held position (§7.3). The amendment states what the operator reviewed and, if the position was closed outside TradePulse, cites the broker activity, which is verified read-only;
     - time never ends the hold, and the generation's evidence records the hold's start, its end and the amendment IDs.
6. **Run plans** for successive generations schedule the successor's start before `next_session_open`, so the planned result is `no_session_gap`. A plan whose successor start falls after `next_session_open` is an accepted-gap plan, and it must say so.

**Equivalence, as accepted (rev. 4).** `no_session_gap` is equivalent to a continuous run for equity and option exits only, because neither monitor would have acted between the seal and the open. It holds only when successor-start verification passed before the open. It is not equivalent for crypto residuals, which are unmanaged, or for any event outside the regular session, such as an after-hours gap that opens through a stop. The exposure record names those limits, and the run report repeats them.

**Crypto residuals** are unmanaged in both generations (TP145-02), so they are outside this rule. They are already listed as residuals.

### 7.5.1 Mark evidence (rev. 5, D05)

**Problem.** Rev. 4 required "a fresh mark from the same source the monitor uses." The monitor uses `positions.current_price`. `AlpacaPosition` has `received_at`, the time the HTTP response arrived, but no time at which the price itself was observed. A positions response received a second ago can carry the previous session's price. The 30 s / 60 s freshness bounds (`execution/quotes.py::quote_age_problem`) belong to executable quotes, which are a separate source with their own `observed_at`. Before the open, those bounds cannot be met at all for an illiquid option. Read literally, rev. 4 either certifies a stale price or makes the planned pre-open takeover impossible.

**The mark evidence record.** One per carried position, written into the successor-start evidence and the `protection_exposure` record:
- `price` and `source`: the quote midpoint (`market_data_quote`) or the latest trade (`market_data_trade`), both from the existing market data provider;
- `observed_at`: the market data's own timestamp (`AuthoritativeQuote.observed_at`, or the trade's timestamp);
- `received_at`: when the response arrived;
- `session_label`: the Rev.144 calendar session that contains `observed_at` (`regular:<date>`, `extended:<date>` or `closed`);
- `evaluated_at` and the `acceptance` result below;
- `broker_position_price`: `positions.current_price`, recorded separately with its `received_at` only. It is never labelled as observed, and it never satisfies the check.

**Acceptance.** It depends on when the successor starts.
- **Before the open (the planned case):** `prior_session_mark` when `observed_at` falls at or after the open of the latest completed regular session and no later than `evaluated_at` plus the existing clock-skew allowance (`QUOTE_CLOCK_SKEW_SECONDS`). This proves only that the price is from the session that just ended. The evaluation outcome for that position stays `out_of_session` (§7.5, check 5), and no exit is decided from it.
- **During a session:** `fresh_executable`, under the existing `quote_age_problem` bound for the asset class. This is the same evidence the gateway requires before an in-session exit.
- **Failure:** a timestamp ahead of the clock beyond the skew allowance, or no mark source for the position at all (the provider cannot price the asset), fails check 4 and refuses the startup, as in rev. 4.
- **Not acceptable, but not a failure:** for example, an illiquid option with no quote or trade observed in the latest completed session. The position is `mark_unverified`, and gets a per-position assumption:
  - its `assumed_at` stays null in the exposure record, so the record does not claim it was checked;
  - non-protective BUYs in that asset are refused with `CARRIED_MARK_UNVERIFIED`, in the fenced authorization (§8);
  - **what the monitor does with it (rev. 6, REV151-03):**
    - **No price-dependent decision from an unaccepted price.** The monitor makes no stop, target, trailing-stop ratchet or scale-out decision for the position from `broker_position_price`, a `prior_session_mark` or any other unaccepted price. Its carried stop and target stay exactly as restored. A stale price can neither ratchet them nor trigger them. It does not fold into the lots' `mfe_price` and `mae_price` either (`_fold_lot_extrema`), because the stall exit and the ladder read those values later.
    - **Time and expiry checks continue,** because they do not read the current price:
      - the `max_hold_days` time stop and the option near-expiry rule;
      - the stall exit, judged on the carried MFE only.
      
      An exit they decide is still submitted through the gateway.
    - **Every exit still passes the gateway's quote check.** The gateway's existing executable-quote check (`quote_age_problem`) still governs submission. With no fresh quote, the gateway refuses the exit as today, and the existing protective-exit alerting applies.
    - **Price-dependent evaluation starts at the first accepted quote:** the first in-session cycle where an executable quote for the asset passes the existing bound. That cycle evaluates the full protective state from that quote, and the position's `assumed_at` is written, which closes its exposure. Every later cycle uses the monitor's normal price path.
    - **Everything else is unchanged.** The monitor keeps its normal cycle for every other carried position, and one `mark_unverified` position never delays them;
  - if the first regular session passes without such a quote, a critical `CARRIED_MARK_UNVERIFIED` alert is raised, and the exposure stays open. Time never closes it.

Rev. 4 refused the whole startup instead. That would leave every carried position unmonitored because of one illiquid option, which is worse protection, not better.

**Per-position assumption.** `monitoring_assumed_at` stays the generation-level time at which checks 1–5 passed. Each carried position also gets its own `assumed_at`. That is the same time for a position whose mark is accepted, and later for a `mark_unverified` one. A position assumed after `next_session_open` lists the part of the first session before its `assumed_at` in its own `sessions_unmonitored`. The generation's continuity class is unchanged, and the report shows the per-position exception.

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

  The `pending_fee` stays open until then. **G1 (2026-11-10):** Alpaca's crypto CFEEs carry no `fill_id` or `order_id`, and the USD CFEE has no symbol. So a crypto fee is always unlinked here. The rev. 7 wait (§4.5) makes this path the exception.

**Where it plugs in:** one rule in the existing membership classifier (`reconciliation/membership.py::_classify`), and no new fee pipeline.
- **The new class.** A FEE or CFEE activity that is absent from the successor's opening population and matches an open `pending_fee` (linked, or named by a `--resolve-fee` amendment) is classified **`predecessor_obligation`**. The rule is checked before the `unresolved_generation_membership` fallbacks.
- **Not the successor's P&L.** `predecessor_obligation` is not in `ELIGIBLE_MEMBERSHIPS`, so the activity is never a successor fee and is never posted as P&L in two generations. **Rev. 5 (D02):** that exclusion alone does not balance the successor's physical cash. §7.6.1 adds the signed bridge entry that does. The rev. 4 wording, "excluded from the successor's cash check", is withdrawn.
- **A fee posted before the successor's freeze.** It is in the successor's opening population and is already `pre_generation` (excluded). Freeze matches it to the pending fee by the same rule and appends the `fee_attribution` record.

**Duplicates and conflicts:**
- **Key:** the activity ID is the attribution key.
- **The same ID with an identical raw digest** is an idempotent no-op. It is neither refused nor posted again, which mirrors the exactly-once `broker:fee:<activity_id>` rule.
- **The same ID with a different raw digest** is refused with `fee_attribution_conflict` and raises a critical alert. Neither version is posted.
- **Several fees for one fill:** one `pending_fee` may be satisfied by more than one linked activity, for example a split fee. Each activity is attributed once by its own ID. The pending fee is marked `observed` at the first one and stays matchable for the rest.

**Corrections and reversals:**
- Some linked activities reference a predecessor fill but are not a USD debit, such as a credit or a reversal. The existing code refuses these with `GENERATION_FEE_NOT_A_DEBIT`.
- Such an activity is recorded as a `fee_correction` attribution against the predecessor, citing the original `pending_fee_id` and, if any, the fee activity it reverses.
- It is never netted against or written over the original record.
- **Rev. 5 (D02):** it gets a signed **credit** bridge entry in the successor (§7.6.1), because the broker's cash already includes it. Rev. 4 posted no cash entry, which left observed cash unexplained in the same way as an unbridged fee. It is still not successor P&L.
- **Rev. 6 (Q9 decided), provenance conditions for the automatic credit.** A credit is bridged automatically only when all of these hold:
  - its raw activity digest verifies;
  - it carries the same broker `fill_id` or `order_id` link as an original fee that is already attributed to the predecessor by its own `fee_attribution` record;
  - its amount does not exceed the attributed fees for that link not yet reversed, so a credit can never reverse more than was charged;
  - its activity ID has not been bridged or attributed anywhere before.

  A credit that fails any condition is never bridged automatically. That covers an unlinked credit, one whose link matches no attributed fee, one that would over-reverse, and one that matches more than one origin generation. It is classified `unresolved_generation_membership`, with the reason `predecessor_cash_reversal_unlinked`, which keeps the successor's cash status unresolved until the operator attributes it with `--resolve-fee`, under the same read-only broker checks.
- It raises an alert for operator review. The alert is mandatory even when the bridge succeeds.

**Chains:**
- A `pending_fee` still open when the successor seals is copied into the successor's handover `pending_fees`, keeping its `origin_generation`.
- An attribution always targets the origin generation's amendments file.
- A pending fee is closed only by attribution or by a verified `--resolve-fee`. Time never closes it.

### 7.6.1 The predecessor-cash bridge (rev. 5, D02)

**Problem.** The successor's cash invariant is opening cash + Σ cash-ledger movements = observed broker cash (`valuation.py::_generation_invariants`). A predecessor fee that posts after the successor's opening still changes that account's cash. For example, the successor opens with $1,000, makes no trades, and a $1 predecessor fee posts. Observed cash is $999. If the fee is only excluded, expected cash stays $1,000 and the cash check latches a mismatch.

**The bridge.** Each activity classified `predecessor_obligation` gets exactly one successor record and one cash-ledger entry.
- **The record:** `generation_predecessor_cash:<activity_id>`, holding:
  - the activity ID and the SHA-256 of the raw activity;
  - the broker's signed USD amount: negative for a fee debit, positive for a reversal credit;
  - the `pending_fee_id` and `origin_generation`;
  - the entry digest of the matching `fee_attribution` or `fee_correction` record in the origin generation's amendment chain (§7.6);
  - the kind: `fee_debit` or `fee_correction_credit`.
- **The cash entry:** `broker:predecessor_cash:<activity_id>`, with the same signed amount, in USD, posted at the activity's time.
- **Exactly once:** the activity ID is the key for both. The same ID with an identical raw digest is a no-op. The same ID with a different digest is `fee_attribution_conflict`, as in §7.6.
- **Write order:** the predecessor's amendment record is appended first. The bridge is then written, citing that record's entry digest. After a crash between the two, the next reconciliation pass finds the amendment, which is idempotent, and writes the bridge. A bridge is never written without its amendment.

**Where it counts.**
- **Cash: included.** `expected_cash` already sums every cash-ledger entry, so the bridge entry is in it: $1,000 + (−$1) = $999, `matched`.
- **Equity and P&L: excluded,** in the same way as capital flows. The successor's `total_equity` subtracts the bridge total, and the report shows it as `excluded_predecessor_cash`. It never enters the successor's strategy P&L, its fee totals or its edge.
- **Validation:** `_generation_invariants` validates each bridge record against its ledger entry and its raw activity, as `validate_generation_fee` does for fees. A `predecessor_obligation` activity with no bridge fails with `predecessor_cash_bridge_missing`. A bridge whose amendment record is absent from the origin generation's chain fails with `predecessor_cash_bridge_unlinked`. Either one leaves the cash status not `matched`.
- **The independent assessment** (`verification/evidence.py`) applies the same bridge term, so valuation and the evidence check agree.

**Cases.**
- **Debit after the successor's opening:** one bridge for the negative amount.
- **Split fee:** two activities, two bridges, each exactly once.
- **Reversal or credit:** one positive bridge, a `fee_correction` record in the predecessor, and an alert, but only under the rev. 6 provenance conditions (§7.6). Otherwise it is held as `predecessor_cash_reversal_unlinked`, with no bridge.
- **Before the successor's opening** (the activity is in the opening population): no bridge. It is already in the opening cash, so only the predecessor's `fee_attribution` is written (§7.6).
- **Unlinked fee before `--resolve-fee`:** it stays `unresolved_generation_membership`, as today, which holds the successor's invariants unresolved. Once it is resolved, it is classified `predecessor_obligation` and bridged.

**The predecessor side.** The sealed figures never change. `post_seal_adjustments` lists each attributed amount, and the predecessor's adjusted result is the sealed result + Σ adjustments. The fee is in the predecessor's adjusted result once, and in the successor's P&L never. Both generations' cash bridges reconcile.

### 7.7 The generation registry (rev. 5, D01)

**Problem.** Each generation must use a new database (`verification/service.py::_require_fresh`), and its store sits next to that database (`store_for`: `<database>.paper-verification`). A new database's store is always empty, so it cannot show that an earlier generation exists. For example, `g1.db` and `g2.db` have different stores. A literal "most recently sealed generation in the store" lookup would treat every new database as a first generation, and skip the unresolved-item, inherited-stop and protection-gap checks.

**The registry.** There is one registry per broker account and environment, outside every database and store.
- **Location:** `TRADEPULSE_REGISTRY_DIR`, else `$XDG_STATE_HOME/tradepulse/generations`, else `~/.local/state/tradepulse/generations`. It is created with mode 0700 and refused on a network filesystem. This is the account lease's resolver rule (`runtime_lease.py::lease_directory`), used for the same reason.
- **Key:** one directory per `<venue>-<environment>-<account_number>`. `environment` is `paper` or `live`, from the configured broker base URL. The directory's first entry records the `account_identity_digest`. A configured account whose digest differs refuses with `registry_account_mismatch`, so two accounts never share a chain.
- **Entries:** one file per entry, `NNNNNNNN.json`, created atomically: written to a temporary file, fsynced, then linked into place with no-replace semantics, then the directory is fsynced. A crash leaves either no entry or a complete one, never a torn one. Each entry holds `seq`, `kind`, `generation`, `parent_entry_digest`, `entry_digest` (the SHA-256 of the canonical entry without that field), the time and the OS user. By kind:
  - `genesis`: the bootstrap (below);
  - `frozen`: the database identity (`verification_identity.database_id`), the database and store paths at freeze (as hints only), the manifest digest and the predecessor generation;
  - `sealed`: the outcome, `sealed_at`, the handover digest and the sealed evidence digest;
  - `abandoned`: an operator record for a frozen generation that never sent an order (below).
- **Head and validity:** the head is the highest `seq`. The chain is valid only if every `seq` from 0 is present, each `parent_entry_digest` equals the previous `entry_digest`, and each digest recomputes. Otherwise freeze refuses with `registry_chain_invalid`. Two entries naming the same parent, or two `frozen` generations open at once, refuse with `registry_forked`. Appends hold an exclusive `flock` on the registry directory's lock file, and the no-replace link refuses a second entry with the same `seq`, so a fork can only come from copying or tampering.

**Ordering with the database and the seal.**
- **Freeze:** freeze appends `frozen` after every other freeze check and before the database bind (`_publish_freeze`'s `UPDATE verification_identity`). If it crashes in between, the `frozen` entry has no bound database. The next freeze for the account sees an unsealed head and refuses with `registry_head_unsealed`. Re-running the same freeze (same generation and database ID, still unbound) completes the bind, idempotently. Otherwise the operator abandons the generation.
- **Seal:** the seal appends `sealed` after `handover.json` and the evidence digest are written, and before the seal is reported complete. After a crash in between, the generation's next start re-runs the seal step, which appends the entry. Identical content is a no-op.
- **Abandon (rewritten in rev. 6, REV151-01):** `tradepulse verification registry abandon --generation G --reason "<text>"` appends `abandoned` for the head `frozen` generation G. Any other unsealed generation must be wound down and sealed. An empty intent table is not proof on its own, because the database itself may be damaged, restored from a backup, or a different file. So abandon needs both local and broker evidence, all read-only, in this order:
  1. **The database is G's, and intact.** G's database, opened read-only, must:
     - pass `PRAGMA integrity_check`;
     - carry the `verification_identity.database_id` of G's `frozen` entry;
     - have the frozen manifest digest.

     Otherwise abandon refuses with `registry_abandon_database_unverified`.
  2. **No intent.** G's database has no trade intent at all. Otherwise abandon refuses with `registry_abandon_has_intents`.
  3. **No protection taken over.** G never wrote `monitoring_assumed_at` or a successor-start evidence record (§7.5). A generation that took over carried positions holds protective state that would be lost, such as a ratcheted stop, so it must be wound down and sealed instead. Otherwise abandon refuses with `registry_abandon_protection_assumed`.
  4. **No broker order since the freeze.** Abandon takes a complete census of the account's orders of every status, created at or after G's `frozen` time less the clock-skew allowance. It uses a paged listing of orders of every status, under the Rev.149 F02 completeness rules (`after` inclusive, `until` exclusive; refused when completeness cannot be proven). The census must be empty. One account is operated from one machine (§11), so any order in that window is G's or out-of-band, and either one refuses with `registry_abandon_broker_activity`. A census whose completeness cannot be proven refuses with `registry_abandon_broker_unproven`.
  5. **No uncited fill since the effective head.** Every FILL activity after the effective head's `sealed_at` (or its `genesis` time) must already be cited by an amendment of that head, which is the stale-head rule below. Otherwise abandon refuses with `registry_abandon_broker_activity`.

  The `abandoned` entry records:
  - the census windows, and the SHA-256 digests of the order census and the activity census;
  - G's `database_id` and the result of each check;
  - the effective head it falls back to.

  **A damaged or lost database:** `--database-lost` replaces checks 1–3 with broker evidence only. It requires checks 4 and 5, plus an empty open-order list. It is recorded as `database_lost: true`, with a critical alert. It is sound only because one account is operated from one machine (§11): an intent that never reached the broker left nothing to account for, and one that did is in the order census.

**Freeze rules.** These run first, before §7.2.
- **No registry for the account:** refuse with `registry_missing`, unless `--registry-bootstrap` is given (below).
- **Head unsealed** (`frozen` with no `sealed` or `abandoned`): refuse with `registry_head_unsealed`, naming that generation.
- **Head sealed under Rev.147:** `--predecessor` is required (`predecessor_required`) and must name the head (`predecessor_not_latest`). §7.2 then applies.
- **Head is `genesis`:** there is no predecessor, and opening positions follow §7.4.
- **Head is `abandoned` (rev. 6, REV151-01):**
  - **The effective head:** the latest `sealed` or `genesis` entry before the abandoned run of entries. Several abandons in a row are skipped together. The abandoned entries stay in the chain as evidence and are never deleted.
  - **The predecessor:** the effective head's generation, so the freeze applies the rule for that head, either "head sealed" or "head is `genesis`". `--predecessor` must name the effective head (`predecessor_not_latest`). The new `frozen` entry's predecessor is the effective head's generation, never the abandoned one.
  - **The broker re-check:** every abandoned entry's census digests are re-verified. The stale-head corroboration below is anchored at the effective head's `sealed_at` (or `genesis` time), not at the abandoned entry's time. So the window from the last seal to this freeze, which includes each abandoned generation's whole life, is checked again from the broker. Any fill in it must be cited, and none can come from an abandoned generation.
  - **The protection gap:** the successor's protection exposure (§7.5) starts at the effective head's `monitoring_released_at`. An abandoned generation never assumed protection (abandon check 3), so it closes no part of that interval.
- **An abandoned generation never runs again:** a freeze, a seal, a successor start or a `run` for a generation with an `abandoned` entry refuses with `registry_generation_abandoned`. The runtime reads the registry at startup, and its generation must be the head `frozen` entry.
- **Locating the predecessor's artifacts:** by digest, not by path. The store and database are found at the paths in the `frozen` entry, or at `--predecessor-store <dir>` and `--predecessor-database <file>` if they were moved. Either way, the manifest digest, `database_id`, handover digest and sealed evidence digest must equal the registry's (`predecessor_artifacts_mismatch`). Missing artifacts refuse with `predecessor_artifacts_missing`. A renamed database is accepted, because its identity is the `database_id` inside it, not its name.

**A stale or rolled-back head (broker corroboration).** A registry restored from an old backup, or with its newest entries deleted, would show an older head. The broker's own history corroborates the head. At freeze, every FILL activity on the account after the head's `sealed_at` (or the `genesis` time) must be cited by an amendment of the head generation (§7.3: a `--position` declaration or a `--resolve`, each verified against the broker). An uncited fill refuses with `registry_head_stale`. It means someone traded the account after the last seal the registry knows about: a lost generation, or an out-of-band trade.
- A rolled-back generation that never filled leaves no position or fee. Freeze already requires an empty open-order list, so there is nothing such a generation could have left that this check misses.

**Bootstrap.** `--registry-bootstrap --reason "<text>"` is accepted only when no registry directory exists for the key; otherwise it refuses with `registry_exists`. It writes `genesis` with the operator, the reason and the census digest at bootstrap. Opening positions are adopted under ordinary Rev.142 rules, with stops rebuilt from the broker average. No inherited protective state is claimed.

**Limits (stated, not solved).**
- **One machine (rev. 6: a hard operational restriction, Q7 decided):** like the account lease, the registry is local to one machine. Two machines trading one account are not detected by either. Broker-fill corroboration finds only fills after the fact. It is not a distributed lock, and it cannot see another machine's unfilled orders. So one broker account must never be operated by TradePulse from more than one machine, and the run plan and the runbook state that as a precondition (§11). Manual trading of the account outside TradePulse breaks the same assumption (the A0 run failed that way). The registry detects it only through an uncited fill. A shared registry belongs with cloud hosting, which is out of scope (§2).
- **A deleted registry:** if it is deleted and bootstrapped again, the chain restarts. The only consequence is a downgrade. Carried positions lose inherited-stop restoration and are adopted with rebuilt stops. A predecessor's late fee falls to `unresolved_generation_membership`, which holds the successor's invariants unresolved, as today. Nothing is certified that was not proven.

## 8. Entry stop and the dispatch barrier

**Authorization (unchanged).** In wind-down, `ExecutionGateway` refuses every BUY that is not a protective exit, with `RUN_WIND_DOWN_ACTIVE`. The check runs in the same fenced authorization as `RISK_APPROVED`, alongside Rev.146's freshness check. Scan lanes also idle in wind-down. In a successor, the gap-breach hold (§7.5) uses the same check and refuses with `GAP_BREACH_UNACKNOWLEDGED`. So does a `mark_unverified` asset (§7.5.1), with `CARRIED_MARK_UNVERIFIED`.

**Problem (rev. 5, D04).** Authorization is not dispatch. A BUY can already be `RISK_APPROVED` when the wind-down starts, while it still awaits the market clock, the entry window, the lease check, the quote recheck and the `SUBMITTED` write before its POST (`execution/gateway.py`, from the `RISK_APPROVED` write to `place_order`). A census taken in that interval sees no broker order. When the call resumes, the order reaches the broker after the wind-down started. A boolean read followed by more awaits is not a guarantee.

**The dispatch barrier.** It lives in the database, so a second or restarted process sees it, and it uses SQLite's serialized writes.
1. **The dispatch write.** For a non-protective BUY, the `RISK_APPROVED` → `SUBMITTED` write becomes one `BEGIN IMMEDIATE` transaction that:
   - re-reads the run state from `wind_down_events`, and any successor hold (gap-breach or `mark_unverified` for the asset);
   - **(rev. 8, REV156-02) checks the deadline itself.** It reads the clock *inside* the transaction, never a time captured before the database was awaited, and compares it with the frozen `planned_end_at − lead`. This does not depend on the watcher having run. At or past the deadline, the BUY is refused with `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH`, and `WIND_DOWN_REQUESTED` (cause `deadline_at_dispatch`) is recorded in the same transaction if it is absent. That is the §8 step 2 transition, so every other `RISK_APPROVED` BUY is rejected with it. The fenced `RISK_APPROVED` write applies the same deadline test;
   - compares and sets the intent from its `RISK_APPROVED` payload, comparing the full payload, as the stranded sweep does;
   - writes `SUBMITTED` only when the run state is `RUNNING` and no hold applies. Otherwise, in the same transaction, it writes the intent `REJECTED` with `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH`, `RUN_PAUSED_AT_DISPATCH`, `GAP_BREACH_UNACKNOWLEDGED` or `CARRIED_MARK_UNVERIFIED`, and `place_order` is never called.

   Since Rev.149 R149-03, the `SUBMITTED` write is the last await before `place_order`. The barrier keeps it that way, so nothing can be awaited between the barrier and the POST.
2. **The transition write.** The event that stops entries (`end_run_requested`, `WIND_DOWN_REQUESTED`, or `run_paused`) is written in a `BEGIN IMMEDIATE` transaction. In that same transaction, every non-protective BUY intent still in `RISK_APPROVED` is moved to `REJECTED` with `RUN_WIND_DOWN_ACTIVE` or `RUN_PAUSED`, by compare-and-set.
3. **Serialization.** Both writes take SQLite's write lock, so for each BUY exactly one of these is true:
   - its `SUBMITTED` commit came before the transition, so it is a dispatched order;
   - the transition came first, so it is `REJECTED` and never sent.
4. **The dispatched population.** A BUY committed `SUBMITTED` before the transition may still reach the broker after it. At wind-down start, the census therefore reads every local BUY intent in `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED` or `SUBMISSION_UNKNOWN`. Each one enters the observed-order register (§4.1) by client order ID, even if no broker order is visible yet. The wind-down cannot be `COMPLETE` until each is durably classified:
   - accepted, then cancelled and terminal under §4.4;
   - rejected by the broker;
   - or resolved by client order ID through the existing `SUBMISSION_UNKNOWN` recovery or the stranded-intent sweep, when the broker shows the order, or by an operator `resolve-dispatch` (step 7). A not-found lookup is never a classification.

   If the census sees no broker order for a `SUBMITTED` intent, that proves nothing, because the POST may still be in flight.
5. **Protective exits are never barred.** That covers every SELL, wind-down crypto sells included.
6. **Pause and crash.**
   - On a pause, `run_paused` closes the `RISK_APPROVED` BUYs (step 2), and a `SUBMITTED` BUY still completes to its true state (§3.3).
   - After a crash between the `SUBMITTED` commit and the POST, the intent is `SUBMITTED` with no broker order. The stranded-intent sweep (`reconciliation/coordinator.py::_recover_stranded_intents`) resolves it by client order ID, under step 7, and the wind-down waits for that through the register.
7. **A not-found lookup is not a rejection (rev. 6, REV151-02).**

   **The defect.** A not-found result from the client-order-ID lookup (HTTP 404) proves only that the broker does not show the order now. A POST delayed in transit, for example across a crash, can still reach the broker afterwards. Today `_resolve_stranded` treats a 404, once 120 s have passed since `created_at`, as definitive: a `SUBMITTED` intent becomes `REJECTED` (`STRANDED_BEFORE_SUBMISSION`), and a `SUBMISSION_UNKNOWN` one becomes `REJECTED` (`BROKER_NEVER_RECEIVED`). A late order would then have no live intent. It would be found only when it filled, as an unattributed fill, after the wind-down had already counted it as never sent. This applies to every generation, not only to the wind-down, so the rule below replaces the sweep's rule everywhere.

   **The rule.** What the sweep may conclude depends on how far the intent got:
   - **`RISK_APPROVED`** (never POSTed, because the POST happens only after the `SUBMITTED` commit, step 1): the sweep may close it as `REJECTED` (`STRANDED_BEFORE_SUBMISSION`) without a broker lookup. This is unchanged in effect.
   - **`SUBMITTED` or `SUBMISSION_UNKNOWN`** (may have been POSTed):
     - **Order found:** it is adopted by client order ID, exactly as today.
     - **404:** the intent becomes, or stays, `SUBMISSION_UNKNOWN`. Each absence is recorded as drift, with one record and one critical alert per UTC day (Rev.154). It is never made terminal. Its asset stays blocked, as for every `SUBMISSION_UNKNOWN` intent, and the sweep repeats the lookup every pass.
     - **No time limit:** no timeout, grace period or horizon ever makes the intent terminal on its own.
   - **The time base:** the `SUBMITTED` write sets the intent's `dispatched_at`, the local dispatch time. `submitted_at` stays the broker's acceptance time (Rev.154 R154-03). The sweep's grace period is measured from `dispatched_at`, not from `created_at`.

   **Operator resolution** is the only way to end an intent that stays absent. `tradepulse verification resolve-dispatch --intent <id> --reason "<text>"` is accepted only when all of these hold:
   - **The horizon has passed:** at least `DISPATCH_FINALITY_MINUTES` since `dispatched_at`. This is a precondition for the operator's action, never an automatic resolution. It is provisional, like `FEE_WAIT_MINUTES`.
   - **The lookup is still absent:** it runs again at the command and still returns 404.
   - **No order and no fill in the window:** a complete, completeness-proven census of orders of every status, created from `dispatched_at` less the clock-skew allowance (5 minutes in Rev.154) up to now, contains no order matching the intent's client order ID or its asset, side and quantity. Also, no FILL activity on the asset in that window is uncited.

   On success, the intent becomes `REJECTED` with `SUBMISSION_ABSENT_OPERATOR_RESOLVED`. It cites the census digests, the operator and the reason.

   **Watched client order IDs.** After an operator resolution, the client order ID stays watched. It enters `watched_client_order_ids`:
   - the reconciliation coordinator looks each one up every pass;
   - the list is carried in the handover (§7.1), so a successor keeps watching it;
   - an ID leaves the list only after 30 days with no appearance.

   If a watched ID ever appears at the broker:
   - a critical alert and an integrity hold are raised (`RESOLVED_DISPATCH_APPEARED`), and the order is never adopted silently;
   - the operator then decides, and the existing `ORDER_IDENTITY_MISMATCH` path is the model.

   **In the wind-down,** an intent in this state keeps the wind-down out of `COMPLETE`, through the register (step 4). It is `BLOCKED` while the order may still arrive, with the existing escalation alert. Only the broker showing the order, or an operator resolution, moves it. It is never cleared to let the wind-down complete.

   **In the build:** this is a change to the existing sweep in `reconciliation/coordinator.py`, in its own commit with its own tests, separate from the wind-down. The stranded-intent blocker text (`blockers.py`) is updated to match.

   **Implemented ahead of Rev.147 in Rev.154** (`docs/rev154-record.md`): the sweep rule, `resolve-dispatch`, the watch, and `--acknowledge-appearance`, which closes an appearance once the late order is terminal and its fills are accounted. Rev.147 still carries the watch across generations and ties it into the wind-down register.

## 9. Gates

- **G1 (read-only, before the build): done 2026-11-10.** The read covered 227 activities on 3 pages, the last one terminal, with the operator's go-ahead. Results:
  - **Crypto sells:** one order with 2 fills. Each fill has one USD CFEE, with `created_at` 4 h 16 min 42 s later (observed timestamp lag, not a maximum API delay), at 0.25% of quantity × price rounded up to the cent ($3.54 and $5.77).
  - **Crypto buys:** two orders with 4 fills. Each fill has one native SOL CFEE, posted 2.3 s and 4.5 s later, at 0.25% of quantity.
  - **No link:** no CFEE carries a `fill_id` or an `order_id`.
  - **Applied** in rev. 7 (§4.5, §7.6). The evidence is in `~/tradepulse-evidence/2026-11-10-rev147-g1/` (manifest sha256 `3e76666d…`).
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
- **Successor-start verification (rev. 4):**
  - **each check:** for each of the five checks, a failure refuses with `handover_assumption_unverified` naming that check, writes no `monitoring_assumed_at`, does not start the runtime, and appends an open `protection_exposure` record to the predecessor's chain;
  - **account drift:** a position change or a new open order between freeze and startup fails check 2, even though the checkpoint comparison (check 1) passed;
  - **restored rows:** a successor holding row altered after adoption fails check 3, even when `handover.json` is intact;
  - **stale mark (amended in rev. 5):** a carried position with no mark source, or a mark ahead of the clock beyond the skew allowance, fails check 4. A stale mark gives `mark_unverified` under §7.5.1, not a pass;
  - **evaluation exit:** an exit decided in the first evaluation is submitted, and its intent is recorded, before `monitoring_assumed_at`;
  - **no waiver:** `--accept-gap` does not let a failed check pass;
  - **restart:** after a fix, a restart reruns all five checks from the first one.
- **Exposure record (rev. 4):**
  - written in every class, `no_session_gap` included, in both the successor's evidence and the predecessor's chain;
  - `sessions_unmonitored` is empty for `no_session_gap` and lists the exact sessions, including a partial one, for `session_gap`;
  - an open exposure stays open with no successor, and time does not close it;
  - the predecessor's sealed files are byte-identical after the exposure record is appended.
- **Gap-breach hold (rev. 4):**
  - a breach puts the successor in the hold: a non-protective BUY is refused with `GAP_BREACH_UNACKNOWLEDGED` in the fenced transaction, and the breached position's stop exit still fires on the first in-session cycle;
  - `gap_audit_unverified` leads to the same hold;
  - `--ack-gap-breach` for each held position ends the hold. An acknowledgement citing an unverifiable broker activity is refused;
  - the hold never ends on time alone.
- **Option expiry (rev. 4):**
  - an option expiring before the NY date of `next_session_open` becomes `unproven` with `carried_option_expires_unmonitored`, and the wind-down becomes `UNRESOLVED`, never `COMPLETE`;
  - an option expiring **on** that date is `unproven` too;
  - an option expiring after it, with a valid stop, is `carry_adoptable`;
  - an option with missing or malformed expiry metadata becomes `unproven` with `carried_option_expiry_unknown`;
  - when the calendar cannot verify `next_session_open`, every carried option becomes `unproven`;
  - no option sell, exercise or close is ever sent by the wind-down;
  - after the operator's broker-verified `--resolve`, the next run's freeze is permitted.
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
- **Dispatch barrier (rev. 5, D04).** These are deterministic interleavings on the simulator, using test hooks that pause the gateway at a named await.
  - **Every await:** pause after `RISK_APPROVED` and at each await before `place_order` (clock, entry window, lease, positions recheck, quote recheck, the `SUBMITTED` write). At each one, request the wind-down and resume. The BUY is either `REJECTED` with `RUN_WIND_DOWN_ACTIVE` / `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH` and the simulator received no order, or it committed `SUBMITTED` before the transition and is in the observed-order register.
  - **Census in the gap:** a census taken while a `SUBMITTED` BUY's POST is held sees no broker order. The wind-down still cannot complete until that intent is classified and, if accepted, cancelled to a terminal state.
  - **The same, with a pause:** the same pauses with `run_paused` instead give `RUN_PAUSED` / `RUN_PAUSED_AT_DISPATCH`, and a `SUBMITTED` BUY ends in its true state.
  - **Cancel during acceptance:** the broker accepts the BUY while the wind-down's cancel is in flight. The fill or cancel is settled, and the result is classified under §4.2.
  - **Crash:** the process dies after the `SUBMITTED` commit and before the POST. On restart the intent is `SUBMITTED` with no broker order. The stranded sweep resolves it by client order ID, and the wind-down waits for that.
  - **Unknown submission:** the POST times out (`SUBMISSION_UNKNOWN`). No second BUY is sent, and the wind-down waits for reconciliation by client order ID.
  - **Delayed POST after the recovery lookup (rev. 6, REV151-02).** The simulator holds the POST in transit and the process crashes:
    1. On restart, the stranded sweep runs past its grace period, and the client-order-ID lookup returns 404.
    2. Expected: the intent is `SUBMISSION_UNKNOWN` (never `REJECTED`), its asset is blocked, and the wind-down is `BLOCKED`, not `COMPLETE`.
    3. The simulator then releases the POST, and the broker accepts it.
    4. The next pass adopts the order by client order ID, the wind-down cancels it or settles its fill under §4.4, and only then can it complete.

    Variants:
    - **Long hold:** the same, with the POST held across many passes and a restart. The intent never becomes terminal on its own.
    - **Never released:** with the POST never released, the intent stays `SUBMISSION_UNKNOWN` indefinitely. `resolve-dispatch` is refused before `DISPATCH_FINALITY_MINUTES` and when the order census is incomplete. It is accepted after the horizon, and the result is `SUBMISSION_ABSENT_OPERATOR_RESOLVED`, with the client order ID watched.
    - **Appears after resolution:** if the POST is released after an operator resolution, the watched ID appears. That gives `RESOLVED_DISPATCH_APPEARED`, a critical alert and an integrity hold, and the order is not adopted.
    - **In the successor:** the watch is carried in the handover, and an ID that appears in the successor gives the same result.
    - **Before the POST:** a `RISK_APPROVED` intent that never reached the `SUBMITTED` commit is closed as `STRANDED_BEFORE_SUBMISSION` without a broker lookup.
    - **Time base:** the grace period counts from `dispatched_at`. An intent created long before its dispatch is not swept early.
  - **Protective exits:** a SELL at each of the same pauses is never barred.
- **Generation registry (rev. 5, D01):**
  - **two fresh databases:** two fresh databases for one account. Freezing the second without `--predecessor` refuses with `predecessor_required`, and with a wrong one, `predecessor_not_latest`;
  - **renamed database:** the predecessor's database is renamed and found through `--predecessor-database` by its `database_id`. A different database with the same name refuses with `predecessor_artifacts_mismatch`;
  - **moved artifacts:** a moved store refuses with `predecessor_artifacts_missing` until `--predecessor-store` names it, and is then verified by digest;
  - **stale head:** the registry's newest entries are deleted, leaving an older head. A broker FILL after that head's seal refuses with `registry_head_stale`;
  - **fork:** a copied entry with a duplicate parent refuses with `registry_forked`, and a broken digest refuses with `registry_chain_invalid`;
  - **unresolved and unsealed predecessors:** an unresolved predecessor still refuses with `handover_unproven_items`. An unsealed head refuses with `registry_head_unsealed`. `abandon` is refused for a generation with intents (`registry_abandon_has_intents`);
  - **abandoned head (rev. 6, REV151-01):**
    - G1 is sealed, G2 is frozen and then abandoned, and G3 is frozen. G3's predecessor must be G1 (`--predecessor G2` refuses with `predecessor_not_latest`), and G3 inherits G1's carried state and protection interval;
    - two abandons in a row fall back to the same sealed head;
    - after a `genesis`, an abandon falls back to the genesis rules;
    - the stale-head check from G1's `sealed_at` finds an uncited fill during G2's life, and refuses with `registry_head_stale`;
  - **abandon refusals (rev. 6):**
    - a database that is not G2's (another `database_id`), fails `integrity_check`, or was restored from before a dispatched intent (no intent, but a broker order after the freeze): `registry_abandon_database_unverified` or `registry_abandon_broker_activity`;
    - a broker order created after G2's freeze, with an empty intent table: `registry_abandon_broker_activity`;
    - an order census whose completeness cannot be proven: `registry_abandon_broker_unproven`;
    - a G2 that wrote `monitoring_assumed_at`: `registry_abandon_protection_assumed`;
    - `--database-lost` with a clean broker census is accepted and alerted, and with any order in the window it is refused;
    - running, freezing or sealing an abandoned G2 refuses with `registry_generation_abandoned`;
  - **two accounts:** two accounts get two independent chains, and an account whose digest differs refuses with `registry_account_mismatch`;
  - **bootstrap:** it succeeds only when no registry exists (`registry_exists`). A missing registry without the flag refuses with `registry_missing`;
  - **crash safety:** a crash at every step of an append (after the temporary write, after the link, before the directory fsync) leaves either no entry or a complete one, and the chain stays valid. A crash between `frozen` and the database bind is completed by re-running the same freeze.
- **Predecessor-cash bridge (rev. 5, D02), each case run end to end through `valuation.py` and `verification/evidence.py`:**
  - **$1,000 / $999:** the successor opens with $1,000, makes no trades, and a $1 linked predecessor fee posts. Expected cash is $999, `matched`. The successor's equity and P&L are unchanged, with `excluded_predecessor_cash = −1`. The predecessor's `post_seal_adjustments` shows −$1, and its sealed bytes are identical;
  - **split fee:** two bridges, each exactly once;
  - **reversal:** a +$1 credit bridge, `fee_correction`, an alert, and cash `matched`;
  - **reversal without provenance (rev. 6, Q9).** Each of these gets no bridge, is recorded as `predecessor_cash_reversal_unlinked`, raises an alert, and leaves cash not `matched` until `--resolve-fee`:
    - a credit with no link;
    - a credit whose link matches no attributed fee;
    - a credit larger than the unreversed attributed fees for its link;
    - a credit whose link matches fees in two origin generations;
    - a credit whose raw digest does not verify;
  - **before the opening:** no bridge, and the opening cash already includes the fee;
  - **missing bridge:** a classified `predecessor_obligation` with no bridge gives `predecessor_cash_bridge_missing`, and a bridge without its amendment gives `predecessor_cash_bridge_unlinked`;
  - **crash between the amendment and the bridge:** the next pass writes the bridge once;
  - **no double P&L:** across both generations, the fee appears once in P&L (the predecessor's adjusted result) and in both cash bridges.
- **Carried-lot contract (rev. 5, D03).** Each scenario is closed in the successor and must reconcile quantity, basis, P&L, account equity and generation equity, without any predecessor fill in the successor's database:
  - **partially closed lot:** opened 10, closed 4. The successor lot opens at 6 with an empty closure map, and the history keeps 10 and the closure;
  - **native asset fees:** a lot with `asset_fee_quantities` keeps them in its history, and the successor lot starts with none;
  - **several lots:** lots with different dates and prices for one asset. The exclusion is summed over the lots with V0 subtracted once, the projected `average_price` equals the handover's, and the oldest lot governs the stop and target;
  - **second inheritance:** the chain gains one hop and the root is unchanged;
  - **target survives:** a successor add-on or partial sell re-projects the holding, and the carried `target_price`, `current_stop` and `scaled_out` are unchanged;
  - **versioned proof:** a `rev147-handover-v1` row with a tampered `original_opened_at`, `original_acquisition_price` or `carried_quantity` gives `ACCOUNTING_ADOPTION_LOT_MISMATCH`, and Rev.142 rows still use their own rule;
  - **average mismatch:** a handover whose `average_price` differs from its lots' weighted average refuses the freeze with `handover_stop_unverified`.
- **Mark evidence (rev. 5, D05):**
  - **old mark, new response:** a newly received positions response carrying a prior-session price is never accepted. `broker_position_price` is recorded with its `received_at` only;
  - **prior-session mark:** a quote observed in the latest completed regular session, at a pre-open start, gives `prior_session_mark`, and the position's outcome is `out_of_session`;
  - **older than that session:** a quote observed before the latest completed session's open is `mark_unverified`;
  - **illiquid option before the open:** no quote in the latest session gives `mark_unverified`. The startup still proceeds for the other positions, the option's `assumed_at` is null, and a BUY in it is refused with `CARRIED_MARK_UNVERIFIED`. At the first in-session fresh quote, its `assumed_at` is written. With none during the session, an alert is raised and the exposure stays open;
  - **no decision from a stale price (rev. 6, REV151-03):**
    - a `mark_unverified` position whose `broker_position_price` is below its stop is not exited on that price, and its stop is not ratcheted by a stale price above the trailing trigger;
    - its time or near-expiry exit is still decided and submitted, and the gateway refuses it without a fresh quote;
    - at the first accepted quote, the full evaluation runs from that quote and `assumed_at` is written;
    - the other carried positions are evaluated every cycle throughout;
  - **at the open:** a start exactly at `next_session_open` uses `fresh_executable`;
  - **future timestamp:** a quote ahead of the clock beyond the skew allowance fails check 4, and the startup is refused;
  - **planned window:** a successor started in the planned pre-open window with carried equities and an illiquid option records `no_session_gap` for the generation, with the option's per-position exception, and nothing claims more than was checked.
- **Fee waits (rev. 7, G1).** On the simulator with the measured posting (`cfee_usd_delay` = 4 h 17 min):
  - **USD fee inside the wait (rev. 8):** a wind-down crypto sell's USD CFEE posts at about 4 h 17 min, within the 8-hour wait.
    - Its cash is booked once, `in_generation`, as an `unallocated_account_fee`, and the cash invariant is `matched`.
    - The fill's obligation stays `open`, with the activity as its candidate, and the handover lists it under `unattributed_fees` with `fee_accounting: "incomplete"`.
    - After `--resolve-fee` it is `closed`, and `fee_accounting` is `"complete"` in `post_seal_adjustments`, with no new cash entry.
  - **Two equal expected fees, one activity (REV156-01):** two fills whose expected fees both round to $3.54, with one $3.54 CFEE posted. Both obligations stay `open`, and both list the activity. Resolving the first succeeds. Resolving the second with the same activity refuses with `fee_activity_already_attributed`.
  - **Unrelated or predecessor fee of equal amount:** a $3.54 fee from another fill, or attributed to a predecessor, is a candidate at most. It never closes the obligation, and it is shown `already_attributed` where that applies.
  - **Repeated reconciliation:** ten passes over the same rows give identical obligations and candidates. No activity closes more than one obligation, and the cash ledger has one entry per activity.
  - **USD fee after the wait:** with the USD CFEE delayed past 8 h, the seal proceeds with a `pending_fee`. In the successor the fee is unlinked, and it is held until `--resolve-fee`.
  - **Amount mismatch:** an obligation with no candidate inside the wait is a `pending_fee` (`fee_wait_expired`). `--resolve-fee` naming an activity one cent off refuses with `fee_attribution_amount_mismatch`. Cash and P&L come from the activities only.
  - **Native-coin buy fee:** it is waited for within 15 minutes through the existing asset-fee path.
  - **Freeze:** a `planned_end_at` that leaves less than 30 min + 8 h + 30 min before `next_session_open` refuses with `wind_down_end_fee_window_insufficient`. A weekday 16:30 ET end passes.
  - **Authoritative next session (rev. 7 decision):** the examples in §4.5 are each a test: Thanksgiving 2026 (Wednesday 16:30 ET passes, with the next open on Friday 2026-11-27, an early-close day; Friday 13:30 ET, after the 13:00 close, is outside the session), the daylight-saving change (Monday 2027-03-15 00:30 EDT passes and 00:31 EDT is refused), Monday 2026-11-16 00:30 versus 00:31 ET, and refusals with `wind_down_end_calendar_unverified`:
    - a fetch that fails;
    - a broker response that is `contested` for any date in the range;
    - a broker response that agrees, but whose coverage ends before `next_session_open`'s date (a fresh check for other dates).
  - **Timeout, not completeness:** a wait that expires gives `fee_accounting: "incomplete"`, with each `pending_fee` marked `fee_wait_expired`, and the report says so. A later `--resolve-fee` for each one gives `"complete"` in `post_seal_adjustments`.
  - **Amount validates an attribution:** `--resolve-fee` naming a crypto USD fee whose amount is one cent off the computed fee refuses with `fee_attribution_amount_mismatch`. Two fills with identical computed fees are never resolved automatically.
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
  - Rev.147 adds only the run state, the census, the observed-order register, the classification, the per-asset sequencing rule, the handover, the protective-state restore, the continuity record and the next-run checks. Rev. 5 adds the generation registry, the predecessor-cash bridge (a new ledger entry kind in the existing ledger), the versioned carried-lot branch of the existing adoption proof, the dispatch-barrier transaction around the existing `SUBMITTED` write, and the mark evidence record.
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
- New strings in rev. 4:
  - refusals: `handover_assumption_unverified`, `GAP_BREACH_UNACKNOWLEDGED`;
  - classification reasons: `carried_option_expires_unmonitored`, `carried_option_expiry_unknown`;
  - records and sections: `protection_exposure` (both the system-written amendment record and the report section);
  - amendment: `--ack-gap-breach`;
  - evaluation outcome: `out_of_session`.
- New strings in rev. 5:
  - registry refusals (D01): `registry_missing`, `registry_exists`, `registry_account_mismatch`, `registry_chain_invalid`, `registry_forked`, `registry_head_unsealed`, `registry_head_stale`, `registry_abandon_has_intents`, `predecessor_required`, `predecessor_artifacts_missing`, `predecessor_artifacts_mismatch`;
  - registry entry kinds: `genesis`, `frozen`, `sealed`, `abandoned`; commands and flags: `registry abandon`, `--registry-bootstrap`, `--predecessor-store`, `--predecessor-database`;
  - cash bridge (D02): record `generation_predecessor_cash:<activity_id>`, cash entry `broker:predecessor_cash:<activity_id>`, kinds `fee_debit` and `fee_correction_credit`, report field `excluded_predecessor_cash`, refusals `predecessor_cash_bridge_missing` and `predecessor_cash_bridge_unlinked`;
  - carried lots (D03): the adoption row's `carried` block (`provenance_chain`, `history`, `original_acquisition_price`, `original_opened_at`, `carried_quantity`), and the attribution field `original_basis_pnl`;
  - dispatch barrier (D04): `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH`, `RUN_PAUSED_AT_DISPATCH`;
  - mark evidence (D05): acceptance results `prior_session_mark`, `fresh_executable` and `mark_unverified`; the refusal and alert `CARRIED_MARK_UNVERIFIED`; the field `broker_position_price`.
- New strings in rev. 6:
  - abandon (REV151-01): `registry_abandon_database_unverified`, `registry_abandon_protection_assumed`, `registry_abandon_broker_activity`, `registry_abandon_broker_unproven`, `registry_generation_abandoned`; the flag `--database-lost` and the entry field `database_lost`;
  - dispatch outcome (REV151-02):
    - the command `resolve-dispatch`;
    - the outcome `SUBMISSION_ABSENT_OPERATOR_RESOLVED`;
    - the alert and hold `RESOLVED_DISPATCH_APPEARED`;
    - the handover field `watched_client_order_ids`, and the intent field `dispatched_at` (Rev.154; it replaces the proposed `last_absent_at`);
    - the provisional constant `DISPATCH_FINALITY_MINUTES`;
    - withdrawn: `BROKER_NEVER_RECEIVED`, so a 404 no longer ends a `SUBMISSION_UNKNOWN` intent;
  - reversal provenance (Q9): `predecessor_cash_reversal_unlinked`.
- New strings in rev. 8:
  - the obligation ID `fee_obligation:<fill_id>`, with states `open` and `closed`;
  - the field `fee_amount_candidates`, the handover section `unattributed_fees`, and the candidate flag `already_attributed`;
  - the refusal `fee_activity_already_attributed`;
  - the manifest field `policy.wind_down.calendar_evidence`;
  - the event causes `deadline_at_dispatch` and `startup_recovery`.
- Withdrawn in rev. 8: `fee_corroborated` and `fee_amount_unmatched`. Amount evidence is now `fee_amount_candidates`.
- New strings in rev. 7:
  - the constants `CRYPTO_SELL_FEE_WAIT_HOURS` and `SUCCESSOR_START_MARGIN_MINUTES`;
  - the freeze refusal `wind_down_end_fee_window_insufficient`;
  - the evidence results `fee_corroborated` and `fee_amount_unmatched`;
  - the manifest field `crypto_fee_rate`;
  - the handover and record field `fee_accounting` (`complete` or `incomplete`), and the pending-fee reason `fee_wait_expired`;
  - the amendment refusal `fee_attribution_amount_mismatch`.
- **Operational restriction (rev. 6, Q7):** one broker account is operated by TradePulse from exactly one machine, and nobody trades it by hand during a run or between generations. The run plan and `docs/operations-runbook.md` state this as a precondition of every run. The registry, the lease and broker corroboration all assume it, and none of them enforces it.
- The build happens on its own branch and worktree, after Rev.146 is merged. Nothing is merged without review and the operator's approval.

## 12. Open questions for review

1. ~~§3.3 (stop signals).~~ Decided in rev. 2: Ctrl+C pauses; `end-run` and the scheduled end start the wind-down.
2. ~~§4.3 (reporting).~~ Decided 2026-11-03: included in financial accounting, excluded from edge statistics, and reported as `wind_down_excluded`.
3. ~~§4.5 (fee wait).~~ Measured by G1 (2026-11-10). Rev. 7 waits up to `CRYPTO_SELL_FEE_WAIT_HOURS = 8` for a crypto sell's USD fee, and `FEE_WAIT_MINUTES = 15` for the rest. The values are provisional because the sample is one sell order.
4. ~~§5 (escalation).~~ Decided 2026-11-03: 60 minutes, as an alert only.
5. ~~§7.5 (continuity design).~~ Accepted with conditions (2026-11-04, the Rev.148 assessment): strict successor-start verification, an explicit exposure record in every case, and a gap audit that only detects and requires intervention. Applied in rev. 4 (§7.5). The original question was: this revision replaces the reviewer's preferred monitor overlap with a session-boundary rule plus operator acceptance, because the account lease makes overlap impossible and two monitors would risk a duplicate exit. Does the reviewer accept that equivalence?
6. ~~§4.2 (option expiry across the gap).~~ Decided 2026-11-04 (reviewer recommendation): `unproven`, which blocks completion until the operator resolves it. The expiry day itself counts, and an unknown expiry counts too (§4.2, the option rule). The original question was: a carried option that expires before the successor's first regular session would expire unmonitored. Should such an option be `unproven` at wind-down, which blocks the seal until the operator acts, or carried with an alert?
7. ~~§7.7 (registry scope).~~ Accepted conditionally (2026-11-07, the Rev.151 review): a per-machine registry is acceptable for the current single-machine deployment, as a hard operational restriction. Broker-fill corroboration is not distributed locking and cannot see another machine's unfilled orders. Applied in rev. 6 (§7.7 limits, §11).
8. ~~§7.5.1 (`mark_unverified`).~~ Accepted with a clarification (2026-11-07): stale marks never drive a price-dependent decision. Applied in rev. 6 (§7.5.1, REV151-03).
9. ~~§7.6 (reversal credits).~~ Accepted with provenance conditions (2026-11-07). A credit is bridged automatically only when it is linked to an attributed original fee, its raw digest verifies, and it is recorded once. An unlinked or ambiguous credit stays unresolved. The alert is mandatory either way. Applied in rev. 6 (§7.6).
10. **§8 step 7 (`DISPATCH_FINALITY_MINUTES`, rev. 6).** Confirmed by the reviewer (rev.156 review, 2026-11-10): 15 minutes is acceptable provisionally as the operator-resolution minimum, with Rev.154's evidence checks and watch intact. It never proves rejection. The original question was: the horizon after which an operator may resolve a dispatch the broker still does not show. It gates the operator's action and never resolves anything on its own. The proposed value is 15 minutes, provisional. Does the reviewer want a longer horizon, or a second lookup through the order census before the command is allowed?
11. ~~§4.5 and §7.6 (crypto USD fees).~~ Decided by the operator (2026-11-10): manual `--resolve-fee` whenever attribution is not proven by broker identity. The exact amount cannot establish ownership, because separate fills can produce identical rounded fees. The computed amount only validates an operator attribution (`fee_attribution_amount_mismatch`). The reviewer confirmed it (rev.156 review): the exact amount validates an attribution, and does not establish ownership.
