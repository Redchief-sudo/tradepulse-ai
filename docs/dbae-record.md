# DBAE — Dynamic Balance-Aware Execution

**Spec:** `docs/superpowers/specs/2026-10-30-dbae-design.md` (rev. 4)
**Plan:** `docs/superpowers/plans/2026-10-30-dbae.md`, with reviewer conditions P1–P3
**Branch:** `dbae`, on `main` at `1ada81f4`. Not merged (DBAE-P3).
**Commits:**
- `63209a77`: task 1, retire the equity profile ladder (§12);
- `259cfdc3`: task 2, account, asset and portfolio-history capability evidence (§3.1, §5.1, §6);
- `4fc8e8e8`: task 3, bands, unlocks and scoped liquidation evidence (§4.1, §8.3);
- `cb6d2407`: task 4, the pure entry policy, shaping and exit feasibility (§5, §6, §8);
- `05dd7439`: task 5, funding reservations on the submitted quantity across symbols (§7);
- `15cd719e`, `26b2248b`: task 6, the calendar-bound session baseline (§4.2, §4.3, §13.1), and its review fix round;
- `b6c39035`, `284b3505`, `5a027aa8`: task 7, the gateway takes DBAE capability evidence; entry authorization with funding, shaping and identity (§3, §7, §8.1, §11); review fix round;
- `50343d11`, `b5214016`: task 8, scale-out feasibility, the residual alert and protective-exit rejection (§8.4, §8.5, §9), and its review fix round;
- `ce709ede`: task 9, the DBAE policy frozen into verification generations (§15);
- `48c7a19b`, `76988ab4`, `6ec23255`: task 10, the property test with the fix it found, and this record;
- `983eab94`, `f73c2836`, `41072dd8`, `b678d9af`, `0ab14d02`, `83632307`: the final review's fix wave (I1–I5, M5; rulings 22–27), and this record update;
- `9ddba3bc`, `cbe531c9`, `5dab5c3b`: the Rev.146 external audit's corrections (DBAE-146-01 to 146-03), and this record update.

DBAE lets TradePulse trade an account from $20. Every new buy is judged, inside the portfolio-risk lock, against fresh broker evidence:
- the account's state and permissions;
- a band set by the session baseline, which a gain during the session never raises;
- the asset's own tradability, fractionability, minimum and increment;
- the funding still committed to every pending buy.

The quantity is only ever rounded down. A buy is refused when its protective exit would not be a supported order. Protective exits never read any of this, and no DBAE reason can block one.

## What was built

| Task | Spec | Change |
|---|---|---|
| 1 | §12 | The equity profile ladder (`TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY`) is removed. Setting the variable fails settings parsing with the migration error. |
| 2 | §3.1, §5.1, §6 | `broker/capabilities.py`: `AccountEvidence`, `AssetTradingCapabilities`, `DailyEquityBar`, the `DbaeCapabilities` protocol, `AlpacaCapabilities`, `NoCapabilityEvidence`, `evidence_fresh`. `SimulatedCapabilities` for tests. Unknown fields are `None`, never a default. |
| 3 | §4.1, §8.3 | `dbae/bands.py` (bands, unlocks, `floor_to_increment`) and `dbae/evidence.py` (policy constants, the exact-scope `SUB_MINIMUM_LIQUIDATION_EVIDENCE` table, empty, and `dbae_policy_fingerprint`). |
| 4 | §5, §6, §8.0–§8.4 | `dbae/policy.py`: `evaluate_entry_policy`, `shape_quantity`, `exit_feasible`, `liquidation_threshold`, `scale_out_feasible`. Pure: no I/O, no clock reads. |
| 5 | §7 | `dbae/funding.py`: `max_funding`, `pending_buy_funding` (P derived from durable intents and SettlementEvents on every call), `funding_available`. The pending-remainder walk is shared with the risk engine (`pending_intent_remainders`). |
| 6 | §4.2, §4.3 | `dbae/baseline.py`: the write-once session baseline, captured from the daily bar mapped by New York date and cross-checked against `last_equity` within $0.01; `band_equity = min(current equity, baseline)`. |
| 7 | §3, §7, §8.1, §10, §11 | `execution/gateway.py`: steps 1–9 of §8.1 for every new buy. `risk_snapshot.dbae` records the four quantities and the decision, and freezes `max_funding` with the inputs it was computed from: `reference_price`, `contract_multiplier`, `slippage_pct` and `fee_pct`, as Decimal strings (ruling 32); `dbae_entry_refused`, `dbae_band_changed` and `dbae_quantity_identity_mismatch` audit events. |
| 8 | §8.4, §8.5, §9 | `monitor/coordinator.py`: scale-outs only when both parts meet the asset minimum and increment; a once-per-session residual alert; `PROTECTIVE_EXIT_REJECTED` alerts on a definitive broker rejection, and the obligation is kept. |
| 9 | §15 | `verification/service.py`: generations freeze `dbae_policy_fingerprint()`. A generation frozen without it gives `generation_predates_dbae`; a changed DBAE policy gives `frozen_configuration_mismatch`. |
| 10 | §14 item 10 | `python_tests/test_dbae_properties.py` (hypothesis), the funding fix it found (below), `hypothesis` in the `dev` extra. |

## The property test (task 10)

Hypothesis runs derandomized with no example database, so CI sees the same examples on every run. The file runs in about 40 s. Properties 1 and 2 run against the pure policy (400 examples each); 3 and 4 drive the gateway on the simulated broker (50 and 20 examples). No broker or network call is made.

| Property | Test |
|---|---|
| No approval when any field the class requires is `None`. A missing field that another class needs never blocks this one. Funding is `None` when its inputs are missing. | `test_no_approval_with_a_required_field_missing`: from fully verified evidence, up to four fields of the account or asset are `None`; the entry is approved exactly when none of them is required, the band unlocks the class, and the baseline is known. |
| Shaped ≤ risk-approved. | `test_shaped_never_exceeds_approved` (pure, over minimums and increments); `test_reserved_funding_never_exceeds_funding_available` also checks `shaped = submitted ≤ risk_approved` on every approval. |
| Total `max_funding` over approved intents ≤ the funding available at each approval. | `test_reserved_funding_never_exceeds_funding_available`: balances from $0 to $5,000, cash and buying power as a share of it, all four profiles, one to four buys on distinct symbols (fractionable, whole-share, crypto with random minimum and increment, option), orders held open so each approval adds to the pending set. At each approval: the recorded P equals what earlier approvals still reserve, `funding_available = min(cash, power) − P`, and `max_funding ≤ funding_available`. `test_a_buy_sized_within_fundable_notional_reserves_within_funding` checks the sizing bound on its own. |
| Protective exits never carry a DBAE reason. | `test_protective_exit_never_carries_a_dbae_reason`: balances from $0.01 to $5,000; DBAE evidence unknown, stale, blocked, closing-only, limited-margin or verified. The stop exit fills, with no DBAE reason, no DBAE read, no `dbae` block, and no DBAE event. |

### Counterexample found and fixed: a reservation above the funding available

**Defect (task 7, gateway).** The gateway passed `funding_available` to `evaluate_risk` as `available_cash`. The engine's cash cap holds the order **notional** to 99% of that figure. But `max_funding` (§7.2) adds the slippage allowance, and for crypto the fee, on top of the notional. Wherever the allowance plus fee exceeds the 1% buffer, a cash-bound buy reserved more than the funding available:
- aggressive equities (1.5%);
- crypto under balanced and micro (1% + 0.25%), and under aggressive;
- options under aggressive, balanced and micro (1.25–1.75%).

Two examples:
- $1,000 aggressive account with $50 of funding, a $10 stock with a 1% stop: 4.95 shares, `max_funding` $50.24 against $50.00 available;
- found by hypothesis unaided (100 examples, before the fix), a $102.51 aggressive account with $1.03 of funding and a 1-cent stock: $1.035 against $1.03.

This broke §7.5's promise that it "never approves an unfunded" buy. §7.5 assumed that `check_cash_sufficiency` "applies its existing 1% buffer and the new order's fees", but the gateway passes no fees, and the buffer does not cover the allowance.

**Fix (at the core).** `dbae/funding.py` gains `fundable_notional(available, asset_class, slippage_pct) = available / (1 + slippage_pct/100 + fee rate)`, the inverse of `max_funding` per dollar of notional. The gateway passes it to `evaluate_risk` (both the first run and the re-approval) as `available_cash`. The engine's 1% buffer then applies to a figure from which the allowance and fee have already been removed, so `max_funding ≤ 0.99 × funding_available`. `funding_available` itself, its record field and P are unchanged.

**Regression tests.**
- `test_dbae_gateway.py::test_cash_bound_buy_reserves_within_funding_available`: an aggressive equity case and a balanced crypto case, both failing before the fix.
- The two counterexamples are pinned as `@example`s on the property test.

**Effect.** A cash-bound buy is about 0.5–1.75% smaller than before. A buy that is not cash-bound is unchanged, and no balance-matrix case changed.

## Deviations, rulings and deferred findings

The SDD ledger is not kept, so this is the permanent copy.

### Spec interpretations (rulings)

1. **Fingerprint read at call time.** `dbae_policy_fingerprint()` reads `BAND_THRESHOLDS`, the constants and the evidence table at call time, never cached. Task 9's mismatch tests depend on it. The fingerprint also includes `REQUIRED_OPTIONS_LEVEL` and `ACCEPTED_ACCOUNT_MULTIPLIERS`, beyond §15's list. That is conservative: changing either one invalidates a frozen generation.
2. **Exit-feasibility stop.** `EntryProposal.stop_price` is the gateway's sizing stop when `0 < stop < price`. Otherwise it is `price × (1 − stop_loss_pct/100)`, the profile stop the position would get. §8.3 is never skipped, and this also closes task 4's "stop above price" finding.
3. **Stale evidence.** Stale asset evidence is treated as all fields unknown, so it is refused under the field's own reason (for example `DBAE_ASSET_NOT_TRADABLE`). Stale account evidence gives `DBAE_ACCOUNT_STATE_UNVERIFIED`. §3.3 names no asset reason.
4. **`DBAE_FRACTIONAL_TRADING_UNVERIFIED`** applies only in `MICRO` and `MICRO_PLUS`. In higher bands, a quantity without verified fractional trading is floored to whole shares (§6.4), not refused.
5. **`shape_quantity` on a non-positive quantity** raises `ValueError`. `evaluate_risk` never approves ≤ 0, so this is a programming error, not a refusal.
6. **Funding.**
   - Only buys reserve funding.
   - The submitted quantity is `risk_snapshot.dbae.submitted_quantity`, else the intent's `requested_quantity`; a mismatch reserves the larger.
   - A pending buy that cannot be priced (no `reference_price`, or an unusable `dbae.submitted_quantity`) is reported in `PendingFunding.unpriced`, and entries are refused with `DBAE_FUNDING_UNVERIFIED`. It is never skipped.
   - The projected fee applies to the unattributed remainder's order notional only.
7. **Identity mismatch.** A critical alert fires once per intent, and new entries are refused with `DBAE_QUANTITY_IDENTITY_MISMATCH`.
8. **Baseline calendar verification** is `gate.status(US_EQUITY, session_date) == "verified"`. On a weekend that is Friday's status, so a Friday that was never verified refuses weekend crypto entries.
9. **Weekend crypto (task 6 note).** If Friday's baseline was not stored and Alpaca has rolled `last_equity` to Friday's close, weekend crypto entries are refused until Monday (fail closed). G1b should observe `last_equity` on a weekend.
10. **Failures refuse.** A portfolio-history read failure, a capability read failure or a baseline read failure refuses the entry (check `history_unavailable` and others); none raises into the gateway. Protective exits never call DBAE.
11. **Naive clock.** A naive `now` raises `ValueError` in `load_or_capture_baseline`; it does not refuse. The gateway always passes aware UTC.
12. **`last_equity`** is not on `AccountEvidence`. The gateway takes it from its existing `broker.get_account()` (`AlpacaAccount.last_equity`).
13. **Kill switch first.** A DBAE refusal never pre-empts the kill-switch latch. The daily-loss and max-drawdown detection, and `latch_risk_stop`, run whenever their condition holds, before a DBAE refusal is returned. §8.1's order governs entry authorization, not account-level safety latches.
14. **Scale-outs** need both the existing `min_lot_notional` test and `scale_out_feasible` (§8.4). Capabilities are read only when a scale-out is possible (gain ≥ `scale_out_r`).
15. **Residual alert** (§8.5): once per asset identity key per New York trading session date.
16. **`PROTECTIVE_EXIT_REJECTED`**: subject is the intent id, severity critical, and the message carries the broker reason. It is raised only for a protective exit with a definitive rejection. A failed alert write never aborts the monitor cycle.
17. **Activation check placement.** `generation_predates_dbae` and the DBAE mismatch are checked in `VerificationRun.check`, before the thresholds comparison. Bind and resume both go through `start() → acquire() → check()`.
18. **Test hooks.** Task 7's stale-worker test subclasses `SimulatedCapabilities` in the test file for its stall hook; production `SimulatedCapabilities` has no hooks.
19. **Hypothesis.** Task 4's ruling used seeded random generation because hypothesis was not a dependency. Task 10 adds `hypothesis>=6.100` to the `dev` extra (installed in the worktree's own `.venv`, never the main checkout's). Task 4's seeded test remains.
20. **Review zip.** The controller builds it after the final whole-branch review, not task 10, so the zip matches the reviewed head.
21. **Funding sizing** (task 10, above): `evaluate_risk` sizes against `fundable_notional(funding_available)`, not `funding_available` itself. This deviates from §7.5's wording ("It is passed to `evaluate_risk` as `available_cash`") in order to keep its guarantee.

### Final review fix wave (2026-11-01)

22. **I1, no type-ignore comments.** The branch's 18 in `test_dbae_gateway.py`, and the two pre-existing ones on the lines it touched in `test_runtime_lease.py`, are gone. The gateway rig uses typed seams: `HookedBroker` (a `SimulatedBroker` subclass with typed place/quote/account/calendar hooks and a recording `place_on_book`), `RecordingAlerter`, `RecordingIntents` (a `RecordRepository` subclass), and a `restamp` flag on `FreshCapabilities` in place of swapping `rig.caps`. `ExecutionGateway.FILL_TIMEOUT_SECONDS` and `POLL_INTERVAL_SECONDS` are annotated `float`, which they always were in use. `test_runtime_lease.py` calls the constructor through a `Callable[..., ExecutionGateway]` on purpose, because it tests the runtime guard behind the static one. `mypy python_tests/test_dbae_gateway.py python_tests/test_runtime_lease.py` passes.
23. **I2, store failures refuse.** On the entry path, a `DatabaseError` never raises:
    - the baseline due-check and the daily checks share the baseline step's catch, so the entry is refused with `DBAE_SESSION_BASELINE_UNVERIFIED` (check `baseline_store_unavailable`);
    - a failed `pending_buy_funding` read is refused with `DBAE_FUNDING_UNVERIFIED`;
    - the `dbae_band_changed` audit write is best-effort and logged, because it is not a decision input;
    - the identity-mismatch alert is best-effort and logged, and the entry is still refused with `DBAE_QUANTITY_IDENTITY_MISMATCH`.

    No new reason strings. Tests: `test_baseline_store_failure_refuses_entry`, `test_pending_funding_store_failure_refuses_entry`, `test_band_audit_store_failure_does_not_block_entry` and `test_identity_mismatch_alert_store_failure_still_refuses`.
24. **I3, whole-only scale-out.** `whole_units_only(caps, asset_class)` holds for an equity or option unless `caps.fractionable is True`. `None` counts as unknown, so it is whole-only. For such an asset:
    - `_scale_out_quantity` floors the part to whole units;
    - `scale_out_feasible` requires both parts to be whole;
    - a 3-share whole-only position sells 1 share, never 1.5;
    - a split that cannot be whole is skipped.

    Ruling 14 still holds: both `min_lot_notional` and `scale_out_feasible` must pass. Test: `test_dbae_exits.py::test_whole_only_equity_scale_out_is_whole_or_skipped`.

    Behaviour change: an equity whose capability read fails is now whole-only, where before it fell back to the $1 rule. A fractional holding then is not split (see the task 8 note under "Known behaviour"). Two pre-DBAE expectations were updated to match:
    - `test_exit_ladder.py::test_fractional_half_floors_to_order_precision` now passes fractionable caps and asserts that unknown caps give no fractional split;
    - `test_dbae_policy.py::test_scale_out_feasible` treats `None` as whole-only.
25. **I4, residual alert guard.** The residual value, including `contract_multiplier_of`, is computed inside the existing `try`. It runs ahead of the stop check, so it can no longer abort the exit loop. Test: `test_dbae_exits.py::test_residual_value_failure_does_not_block_stop_check`.
26. **I5, one evidence-age constant.** `DBAE_EVIDENCE_MAX_AGE_SECONDS = 30` lives in `broker/capabilities.py` (the import direction is dbae → broker). `evidence_fresh()` and `dbae_policy_fingerprint()` both read it at call time, and `tradepulse.dbae` re-exports it. The hard-coded `timedelta(seconds=30)` is gone. Test: `test_dbae_capabilities.py::test_evidence_age_limit_is_the_frozen_constant`.
27. **M5, no vacuous pinned examples.** `_check_funding` returns every result. `test_pinned_funding_cases_keep_their_outcome` asserts each pinned `@example`'s statuses and reasons:
    - The $50 case is approved.
    - The unaided counterexample ($1.03 of funding, 1-cent stock) is, after the funding fix, refused with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED` (its stop value is below $1), not approved. Before this wave its `@example` passed without checking anything.
    - A $1.05 twin is pinned as the tightest approval. Before the fix it would have reserved $1.0551 against $1.05.

    The clock-calendar `MONITOR` is reset per example, and the shared-fixture comment is corrected: `MONITOR` is observed by the session decision, and the autouse `held_lease` is shared too.

### Rev.146 audit corrections (2026-11-01)

An external audit of the branch at `5d8b7e51` raised four findings. The controller made rulings on them, and the reviewer amended two of those rulings (marked below).

28. **DBAE-146-01, P from the frozen authorization** (`9ddba3bc`; closes M1).
    - **Defect.** `pending_buy_funding` priced every pending buy at the *current* profile's slippage. Lowering the slippage would have shrunk an existing, unproven commitment.
    - **Fix** (`dbae/funding.py`). A buy now reserves the larger of two figures (controller ruling):
      - its frozen `dbae.max_funding × unattributed / dbae.submitted_quantity`;
      - `max_funding` on the unattributed quantity at the current profile.

      So a profile change can only raise P.
    - **Broker evidence still reduces P** (reviewer amendment). An attributed fill reduces P, and each SettlementEvent is counted once. A confirmed terminal state drops the buy out of P. `SUBMISSION_UNKNOWN` keeps the full frozen commitment. The fee term stays on the unattributed remainder only, and a mismatch still reserves on the larger quantity.
    - **Fail closed.** A buy with an unattributed remainder is `unpriced` (`DBAE_FUNDING_UNVERIFIED`) when any of these holds:
      - it has no `dbae` block (pre-DBAE: there are no frozen parameters);
      - `max_funding` does not reconcile exactly with the block's own frozen inputs (ruling 32).

      The fixed bounds this round first used (`notional + fee ≤ max_funding ≤ 2 × notional`, `MAX_FROZEN_FUNDING_RATE`) were replaced by that reconciliation in the re-review fix round.
    - **Why no production buy lacks a block.** The gateway's RISK_APPROVED write is the only production writer of a pending buy, and every buy there passes DBAE. So only pre-DBAE rows, and test fixtures, lack a block. Two fixtures were given the block they would have had: one in `test_execution_gateway.py` and the two mismatch fixtures in `test_dbae_gateway.py`. `test_pre_dbae_intent_reserved_on_field` became `test_pre_dbae_intent_is_unpriced`.
    - **Tests.**
      - `test_dbae_funding.py`: `test_profile_change_never_reduces_pending_funding` (RISK_APPROVED, ACCEPTED, PARTIALLY_FILLED, SUBMISSION_UNKNOWN × lowered, raised); `test_definitive_fill_reduces_to_frozen_remainder` (amendment a, b); `test_terminal_after_partial_fill_reserves_nothing` (c); `test_submission_unknown_keeps_full_frozen_commitment` (d); `test_invalid_frozen_max_funding_is_unpriced`; the reconciliation tests of ruling 32 (which replaced `test_frozen_max_funding_bounds_are_inclusive` and `test_crypto_frozen_below_notional_plus_fee_is_unpriced`); `test_pre_dbae_intent_is_unpriced`; `test_fully_attributed_pre_dbae_intent_is_not_flagged`.
      - `test_dbae_gateway.py`: `test_profile_change_across_restart_never_reduces_pending` (accepted, partial, ambiguous; restart; lowered and raised); `test_pending_pre_dbae_buy_refuses_new_entries`.
29. **DBAE-146-02, evidence age at the authorization boundary** (`cbe531c9`; closes M2's commit-time point).
    - **Defect.** Entry authorization read the clock once, at its start. Calendar, baseline, risk and database work followed, so evidence could pass 30 s before RISK_APPROVED committed.
    - **Fix.** The controller's ruling, a check immediately before the update, was **reversed by the reviewer**: the check is part of the fenced write's own transaction.
      - `RecordRepository.update` gains a generic `precondition` (`Callable[[], Sequence[str]]`). It runs inside the write's BEGIN IMMEDIATE transaction, after `_check_lease` and before the write. Any reason it returns raises `RepositoryPreconditionError`, which rolls back with nothing written. Persistence has no DBAE imports.
      - The gateway's precondition, `_evidence_still_fresh`, reads the gateway clock inside the transaction. Stale account evidence gives `DBAE_ACCOUNT_STATE_UNVERIFIED`.
      - Stale asset evidence is all fields unknown (ruling 3), so the policy is re-run at that instant and its field reasons are returned, at least `DBAE_ASSET_NOT_TRADABLE`.
      - The intent is closed unsubmitted (`REJECTED`) and `dbae_entry_refused` is recorded. Freshness is never extended, and there are no new reason strings. The lease fence runs first, so a lapsed lease is still `PORTFOLIO_RISK_LOCK_LOST`. Protective exits never take this path.
    - **Tests.**
      - `test_persistence_lock.py`: `test_failed_precondition_writes_nothing`, `test_passing_precondition_writes`, `test_lease_fence_runs_before_the_precondition`, and `test_precondition_runs_inside_the_write_transaction` (a second writer cannot BEGIN IMMEDIATE while the precondition runs).
      - `test_dbae_gateway.py`: `test_account_evidence_expiring_before_approval_is_refused` and `test_asset_evidence_expiring_before_approval_is_refused`. Each runs at four points: the baseline step, risk evaluation, the funding read, and before the write's transaction. Then `test_evidence_fresh_at_the_boundary_is_approved` (the control, 29 s), `test_lapsed_lease_with_expired_evidence_cannot_commit`, and `test_expired_evidence_and_two_symbols_competing_for_the_same_cash` (a $20 account, with the lease lapsed or held: one approval, reserved ≤ funding available, the loser refused with its reason).
30. **DBAE-146-03, the crypto weekend baseline** (`5dab5c3b`). This finding only documents and tests current behaviour; see "Crypto weekend baseline" above. No case contradicts the spec, and no code changed.
31. **DBAE-146-04: a $20 conservative account and G2.** No code change. `SUB_MINIMUM_LIQUIDATION_EVIDENCE` stays empty. A $20 conservative account sizes a $1.00 fractional entry whose stop value is $0.95, so §8.3 refuses it with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED` (§8.6; `test_matrix_expectations_match_the_spec_20_dollar_cases`). It cannot complete an entry and protective-exit lifecycle until G2 adds the paper, equity, fractional evidence row. This is intended: DBAE never approves an entry whose protective exit is not a proven-supported order.

### Rev.146 re-review fixes (2026-11-01)

An independent re-review of the correction round (`5d8b7e51..39a73781`) found one Important and one Minor. The reviewer's binding position ruled both fix-now.

32. **I1, the frozen record reconciles exactly** (`54a24f48`; replaces the 2× bound of ruling 28).
    - **Defect.** `MAX_FROZEN_FUNDING_RATE = 2` was a fixed window, not evidence. A corrupt `max_funding` within `[1 + fee, 2] ×` notional was accepted, and a legitimate record under a custom profile with 100% or more slippage (`RiskLimits` sets no upper limit) was rejected.
    - **Frozen inputs.** At approval the gateway's `risk_snapshot.dbae` block also records, as Decimal strings, the exact values its `max_funding` is computed from: `reference_price` (the proposal price, which is the intent's `reference_price`), `contract_multiplier` (`contract_multiplier_of(asset)`), `slippage_pct` (`slippage_limit_for(asset_class)`) and `fee_pct` (the buy-side fee rate, `buy_fee_pct`).
    - **One formula.** `funding_for(quantity, reference_price, contract_multiplier, slippage_pct, fee_pct)` in `dbae/funding.py` is the §7.2 expression. `max_funding()` and the gateway price a new buy with it; `pending_buy_funding` recomputes the frozen record with it.
    - **Validation** (`_frozen_max_funding`). The frozen `max_funding` is used only when:
      - `max_funding`, `reference_price` and `contract_multiplier` are each present, finite and positive, and `slippage_pct` and `fee_pct` are each present, finite and zero or positive;
      - the frozen `reference_price` equals the intent's `reference_price`, and the frozen `contract_multiplier` equals `contract_multiplier_of(intent.asset)`;
      - `funding_for(dbae.submitted_quantity, …frozen inputs)` equals `max_funding` exactly. The tolerance is zero: the same Decimal expression on the same inputs, which `str(Decimal)` round-trips losslessly, gives the same Decimal.

      Anything else is `unpriced` (`DBAE_FUNDING_UNVERIFIED`). No fixed ratio bounds the record. `MAX_FROZEN_FUNDING_RATE` is deleted.
    - **Unchanged.** The reservation per unattributed unit is still the larger of the frozen funding and the current profile's; verified fills and confirmed terminal states reduce it, attribution is exact-once, `SUBMISSION_UNKNOWN` keeps its full frozen commitment, and a buy with no `dbae` block, or a block without the new fields, is unpriced. The branch is unmerged, so no production record lacks the new fields. The `test_execution_gateway.py` fixture was given the fields it would have had.
    - **A zero `slippage_pct` is valid** (controller ruling, follow-up commit "a zero frozen slippage allowance is valid"): a zero allowance is a valid configuration, and the exact recomputation still catches corruption. Negative or non-finite values stay unpriced.
    - **Tests.**
      - `test_dbae_funding.py`: `test_exactly_reconciled_record_is_priced`, `test_block_without_frozen_rates_is_unpriced`, `test_missing_frozen_field_is_unpriced`, `test_non_finite_frozen_field_is_unpriced`, `test_non_positive_frozen_field_is_unpriced` (each × the five fields), `test_recompute_mismatch_is_unpriced` (`max_funding`, `slippage_pct`, `fee_pct` ± 0.01), `test_recompute_mismatch_by_one_millionth_is_unpriced`, `test_reference_price_mismatch_is_unpriced` and `test_contract_multiplier_mismatch_is_unpriced` (each self-consistent, so only the cross-check refuses it), `test_option_with_options_slippage_reconciles`, `test_crypto_with_fee_reconciles`, `test_legitimately_large_ratio_is_priced` (an option with multiplier 100, and a custom 150% slippage the old bound rejected), `test_zero_frozen_slippage_reconciles` (equity, crypto: 0 priced, −0.01 unpriced).
      - `test_dbae_gateway.py`: `test_approval_freezes_the_funding_inputs` (equity, crypto, option).
33. **M1, an unexpected precondition error closes the intent** (`31c1d32a`).
    - **Defect.** If the in-transaction precondition raised anything but its refusal (for example the policy re-run on stale asset evidence, or the clock), the transaction rolled back but the exception escaped `execute_intent`, leaving the intent in its pre-approval status.
    - **Fix.** `_evidence_still_fresh` catches an error while judging, logs it with the intent id (`logger.exception`), and returns `DBAE_ACCOUNT_STATE_UNVERIFIED`: freshness at commit is unverified. The write raises `RepositoryPreconditionError` and rolls back, so nothing is written on the approval path, and the gateway closes the intent `REJECTED` with `dbae_entry_refused`, as for stale evidence. No new reason string.
    - **Test.** `test_dbae_gateway.py`: `test_unexpected_precondition_error_closes_the_intent_unsubmitted` (no `RISK_APPROVED`, no `dbae` block, P = 0, no orders, `REJECTED` with the reason, the refusal event, and the logged error).

### Rev.146 remediation review follow-ups (2026-11-03)

The forensic review of the remediation zip (`d35643e9`) gave a conditional merge approval (9.0/10). It found no new critical defect, and it set an order: add the M7 guard, close the minor items where practical, re-run G3 in the worktree, then review the merge separately. Rulings 34 to 40 are that round, all in `63aa22a7`.

34. **M7, a static guard on the lease fence.** New `python_tests/test_gateway_lease_fence.py`. An AST scan of `gateway.py` finds every `trade_intents.update` or `create` that writes an intent built with `status=TradeIntentStatus.RISK_APPROVED`, and fails if any lacks `lease=`, or passes `lease=None`. Two mutation tests prove the guard fires: one drops the argument and one passes `None`. A fourth test fails if any other module builds a `RISK_APPROVED` intent, because the guard would not see that path.
35. **M10, ordered exit rungs.** `RiskLimits.__post_init__` refuses `single_unit_target_r` or `final_target_r` below `scale_out_r` when both are enabled; 0 still disables a rung. Every shipped profile passes (defaults 1, 1.5 and 3). This closes the "inverted exit config" behaviour of task 8 below. Tests are in `test_risk_profiles.py`.
36. **M9, dead code and a stale docstring.**
    - `PendingIntentRemainder` gains `requested_qty: Decimal`, set where `pending_intent_remainders` has already raised on a missing quantity, so `pending_buy_funding` no longer needs its dead `requested_quantity is None` branch. This is fixed at the type, not by narrowing.
    - `ExecutionPolicy.as_snapshot` was used only by its test, so both are deleted.
    - The `_core_account_reasons` docstring now says §5.2's order.
37. **M8, the windowed alert is public.** `tradepulse.alerts.alert_once_per_window(audit_events, alerts, …, entity_type=…)` replaces the gateway's and monitor's imports of the private `reconciliation.coordinator._alert_once`. Reconciliation's `_alert_once` now delegates to it with `entity_type="reconciliation"`. The gateway writes `"execution"` and the monitor writes `"monitor"`, which also closes task 8's `entity_type` note.
38. **M4, one rejection alert per asset per hour.**
    - `PROTECTIVE_EXIT_REJECTED` is keyed by the asset and the UTC hour, not by the intent. Each monitor retry is a new intent, so a broker that kept rejecting used to alert on every cycle.
    - Every rejection stays on its own `REJECTED` intent row, and the alert's details carry the first intent of the hour.
    - Test: `test_repeated_rejections_alert_once_per_asset_per_hour` covers three rejected cycles in one hour (one alert), then a fourth in the next hour (a second alert), with four `REJECTED` intents and the position still held.
39. **M3, the latest band change is one key read.**
    - `dbae_band_changed` ids are now `dbae_band_changed:<12-digit sequence>`, and the intent id moved into `details` along with the `sequence`.
    - The new `RecordRepository.last_by_id_prefix` reads the greatest id in the prefix as a primary-key range. So the latest event is found without decoding every event, and no `created_at` tie or UUID order decides it.
    - Two writers racing for one sequence collide on `create_once`.
    - The branch is unmerged, so no database holds the old ids.
    - Tests: `test_repositories.py::test_last_by_id_prefix_reads_the_greatest_id_in_the_prefix` (neighbouring prefixes excluded), plus sequence assertions in `test_band_changed_event_once_per_change`.
40. **The freshness-check fault has its own reason.**
    - The reviewer noted that the commit-time precondition turns any exception into `DBAE_ACCOUNT_STATE_UNVERIFIED`. That is safe, but monitoring could not tell a fault from stale evidence.
    - The precondition now returns `DBAE_FRESHNESS_CHECK_ERROR` beside `DBAE_ACCOUNT_STATE_UNVERIFIED`, and the log line starts with the same token.
    - The refusal is unchanged. The new string is listed in the DBAE spec's reason table, and `test_unexpected_precondition_error_closes_the_intent_unsubmitted` asserts both reasons.

### Known behaviour to note

- **Crypto scale-out under a transient capability failure (task 8).** A transient crypto capability-read failure past `single_unit_target_r` gives a full exit instead of a partial one. The position cannot be proven splittable, so it exits whole, consistent with ruling 14.
- **Inverted exit config (task 8).** Closed by ruling 35. With `single_unit_target_r < scale_out_r` and the gain between them, unknown capabilities make a splittable position exit whole at `single_unit_target_r`. No profile sets this today (defaults 1 and 1.5). A `RiskLimits` validation `single_unit_target_r ≥ scale_out_r` would close it.

### Deferred minor findings (from task reviews)

- **Task 1.** A full-suite pass count was lost to `tail`. The reviewer's note on the trailer model name is not a defect.
- **Task 2.**
  - `capabilities.py` imports the concrete `AlpacaClient` for an annotation, and `test_broker_port`'s allow-list was widened; a structural protocol would avoid that.
  - It uses `AlpacaClient._request` and `_trading_base`, which are private.
  - `evidence_fresh` raises `TypeError` on naive datetimes.
  - The active/tradable parsing is duplicated.
- **Task 3.**
  - `floor_to_increment` truncates a negative quantity toward zero; `shape_quantity` never passes one.
  - A quantity over 28 digits raises `InvalidOperation` (untested), and an increment of `1E+1` yields `0E+1` exponent form.
  - Code was written before the first RED run (process).
- **Task 4.**
  - The `_core_account_reasons` docstring says §10 order, but the order is §5.2's.
  - A NaN band equity raises `InvalidOperation` instead of refusing (it still fails closed).
  - `ExecutionPolicy` does not record the asset class; the gateway passes the same asset through every step.
- **Task 5.**
  - The `unpriced` docstring is stale: a bad `dbae` block is flagged even when fully attributed.
  - The AST multiplier test misses `getattr`, `asdict` and `vars` string access.
  - `test_contract_vs_account_multiplier` is near-tautological; the AST test is the real guard.
  - A dead `continue` reads as a silent skip.
  - There is an inline import in `test_fee_correction_counted_once`.
- **Task 6.** The stored-row path writes an audit `create_once` on every call, and a `DatabaseError` there raises.
- **Task 7.**
  - `test_dbae_gateway.py` carried type-ignore comments: 18 lines, not only `rig.caps`. Fixed (ruling 22).
  - `_baseline_capture_due` reads `dbae_session_baselines` outside the `DatabaseError` catch. Fixed with the wider store exposure (ruling 23).
  - A `latch_risk_stop` write failure on the DBAE-refusal path propagates, the same exposure as the existing rejection branch.
  - `NO_RECENT_SESSION` from `session_date_for` escapes `load_or_capture_baseline` (pre-existing).
- **Task 8.** `_alert_once` writes `entity_type="reconciliation"` for monitor and gateway events. The restart test reads private gateway attributes.
- **Task 9.**
  - The `begin` and `runtime_started` parameters in `test_dbae_activation.py` are vacuous: they refuse on the missing lease.
  - The fixture reads `kwargs["policy"]`, which is fragile if `freeze_source` is called positionally.
  - The start/acquire reason is asserted only through `invalid.json`.
  - The mismatch tests do not assert invalidation, matching the existing context check.
- **Task 10.**
  - `.venv/bin/pytest` cannot import `python_tests.*` helper modules (two test files import `python_tests.opening_fixtures`), so the suite is run as `.venv/bin/python -m pytest`, as the ledger did with the main venv.
  - The property tests suppress hypothesis's `function_scoped_fixture` health check. The calendar fixture and the autouse `held_lease` are identical for every example. `MONITOR` is reset per example (ruling 27), and each example builds its own database, broker and clock.
  - Refused examples were skipped silently. Fixed (ruling 27).

### Known follow-ups (final review Minors, not fixed in the fix wave)

- **M1.** Closed by DBAE-146-01 (ruling 28).
- **M2.** Evidence age and lock budget:
  - `/account` figures are stamped after the `/account/configurations` call;
  - freshness is judged at the start of `_authorize_entry`, not at commit (closed by DBAE-146-02, ruling 29);
  - the 30 s portfolio-risk lock now carries extra broker reads and possibly `run_daily_checks`.

  It fails safe.
- **M3.** Closed by ruling 39. `_record_band` scans every `dbae_band_changed` event on each buy, and a `created_at` tie falls back to UUID order.
- **M4.** Closed by ruling 38. A stop exit that the broker keeps rejecting sends a critical alert every monitor cycle (once per intent). Consider a per-asset, per-hour window.
- **M6.** Install `hypothesis` in the main checkout's venv after merge (see "After merge").
- **M7.** Closed by ruling 34. No static (AST) guard that the `RISK_APPROVED` update in `gateway.py` passes `lease=`.
- **M8.** Closed by ruling 37. The gateway and monitor import the private `reconciliation.coordinator._alert_once`. Promote it to `tradepulse.alerts`.
- **M9.** Closed by ruling 36.
  - The `_core_account_reasons` docstring cites §10 order, where the order is §5.2's.
  - `ExecutionPolicy.as_snapshot` is used only by tests.
  - `pending_buy_funding` has a dead `requested_quantity is None` branch.
- **M10.** Closed by ruling 35. Add a `RiskLimits` validation `single_unit_target_r >= scale_out_r`.
- **Test typing.** `mypy python_tests/test_dbae_properties.py` reports two `arg-type` errors in `_knock_out` (`replace(**dict[str, None])`). Test files are outside mypy's configured scope.

## Crypto weekend baseline (DBAE-146-03)

Crypto trades every day, but its band uses the equity session's baseline (§4.3). The session date is the New York calendar date when that date is a US equity session, and otherwise the most recent session before it. The baseline for a session is the close of the trading date before it.

Example: Thursday closes at $90 (MICRO_PLUS, crypto locked). Friday gains and closes at $120 (FRACTIONAL, crypto unlocked).

| Crypto entry at (New York time) | Session date | Previous trading date | Baseline | Crypto |
|---|---|---|---|---|
| Friday 17:00, after the close | Friday | Thursday | $90 | locked |
| Saturday | Friday | Thursday | $90 | locked |
| Sunday | Friday | Thursday | $90 | locked |
| Monday 06:00, before the open | Monday | Friday | $120 | unlocked |
| Monday 10:00, after the open | Monday | Friday | $120 | unlocked |
| Holiday Monday (Memorial Day 2026-05-25) | Friday | Thursday | $90 | locked |
| Tuesday after that holiday, 06:00 | Tuesday | Friday | $120 | unlocked |

In plain terms: **a Friday gain unlocks a band for crypto from 00:00 New York time on the next equity trading day** (Monday, or Tuesday after a Monday holiday), not during the weekend and not only at Monday's open. A loss still lowers the band at once, because band equity is the lower of current equity and the baseline (§4.2).

Two conditions apply to that unlock:
- **`last_equity` must have rolled.** The new baseline is verified only when `last_equity` equals Friday's close within $0.01. If Alpaca still reports Thursday's close early on Monday, the entry is refused (`DBAE_SESSION_BASELINE_UNVERIFIED`, check `last_equity_disagrees`), and capture is retried at the next buy. There is no fallback to Friday's baseline.
- **Weekend first capture** (ruling 9). If Friday's baseline was not stored on Friday and Alpaca has already rolled `last_equity` to Friday's close, weekend crypto entries are refused until Monday.

This matches the spec: "Upgrades therefore happen only at an equity trading-day boundary", and "A crypto entry on Saturday uses Friday's session baseline". No case contradicts it, and no code was changed. Tests in `python_tests/test_dbae_baseline.py`:
- `test_crypto_weekend_baseline_table`;
- `test_saturday_first_capture_needs_last_equity_at_thursday_close`;
- `test_monday_premarket_before_last_equity_rolls_is_refused`;
- `test_crypto_holiday_monday_baseline`.

## Gates

### G1: done (read only, 2026-10-30)

Evidence: `~/tradepulse-evidence/2026-10-30-dbae-g1/` (manifest SHA-256 prefix `8c687d283405b346`, no identifiers).

- **Bar dates.** 1D portfolio-history bars are stamped 00:00 UTC on the day after each trading date. They are mapped by New York date: no weekend bars, and no bar for the current session during it.
- **Agreement.** `last_equity` matched the previous close (the bar for trading date 10-29) exactly.
- **Limitation.** The account was flat, so current equity also equalled `last_equity`. This cannot tell "previous close" from "current equity", so **G1b must repeat on a day the account holds positions.**

### G1b: not done

Read only, on the first session day after the build with a non-flat account. Ideally `last_equity` is also observed on a weekend (ruling 9). If `last_equity` disagrees with the mapped bar during a session, the spec is revised before activation.

### G2: not done

Paper sub-$1 liquidation and funding behaviour:
1. buy about $1.10 of one fractionable stock;
2. sell about $0.50 of it by quantity;
3. sell the remainder.

The account snapshot is recorded before each submission, after acceptance and after each fill. **The operator approves each exact order beforehand**, and never during a verification run. The outcome adds the single paper, equity, fractional row to `SUB_MINIMUM_LIQUIDATION_EVIDENCE`. Until then, §8.3 refuses every entry whose stop value is below $1.

### G3: passed at the final-review fix head (`83632307`, 2026-11-01)

Run in `~/tradepulse-ai-dbae` with its own `.venv` (Python 3.12.3, tool versions pinned to the main venv's). The record commit that follows changes only this file.

| Check | Summary line |
|---|---|
| `.venv/bin/python -m pytest -o addopts="" -q` | `2092 passed, 10 deselected in 644.49s (0:10:44)` (the 10 deselected are `integration`-marked) |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | `Found 1750 errors.`: equal to the baseline, so no new findings |
| `.venv/bin/python -m pytest python_tests/test_dbae_properties.py -o addopts="" -q` | `8 passed in 73.89s (0:01:13)` |
| `git diff 1ada81f4..HEAD \| grep "type: ignore"` | only two `-` lines, the pre-existing comments removed from `test_runtime_lease.py` (ruling 22); no added lines |

The task-10 G3 run (2082 passed, counted from the dots with no summary line) is superseded. The 10 new tests are 4 for I2, 1 each for I3, I4 and I5, and 3 parametrised cases for M5.

### G3: passed at the Rev.146 correction head (`5dab5c3b`, 2026-11-01)

The same worktree and `.venv`. Dependency pins: `exchange_calendars` 4.13.2 is installed and pinned `==4.13.2`. `respx` 0.23.1 is installed, and the pin is `>=0.21`. There is no lock file, so nothing was reinstalled. The record commit that follows changes only this file.

| Check | Summary line |
|---|---|
| `.venv/bin/python -m pytest -o addopts="" -q -p no:cacheprovider` | `2147 passed, 10 deselected in 748.29s (0:12:28)` (55 more than the last G3, all new in this round) |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | `Found 1750 errors.`: equal to the last G3 and to the baseline, so no new findings (`1ada81f4` itself has 1763) |
| `.venv/bin/ruff check $(git diff --name-only 1ada81f4..HEAD -- '*.py')` | `Found 537 errors.` across the 50 files the branch changed. That is 15 fewer than those files had at `1ada81f4`. The branch introduced only 2, both in task 1's `test_dbae_ladder_retired.py:29-30` (`PLR0402`); the Rev.146 round introduced none. |

### G3: passed at the Rev.146 re-review fix head (`31c1d32a`, 2026-11-01)

The same worktree and `.venv`; no dependency changed. The record commit that follows changes only this file.

| Check | Summary line |
|---|---|
| `.venv/bin/python -m pytest -o addopts="" -q -p no:cacheprovider` | `2209 passed, 10 deselected in 787.17s (0:13:07)` (62 more than the last G3: the I1 reconciliation tests, the gateway freeze test and the M1 regression, less the two removed bound tests) |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | `Found 1750 errors.`: equal to the baseline |
| `.venv/bin/ruff check $(git diff --name-only 1ada81f4..HEAD -- '*.py')` | `Found 537 errors.` across the same 50 files: equal to the last G3, so this round introduced none |

### G3: passed at the final Rev.146 code head (`e934cb77`, 2026-11-01)

This is the run for the review package: the same worktree and `.venv`, with no dependency changed. It is run with the project's default selection. Integration items are deselected; the Alpaca paper conformance kit is never collected.

| Check | Summary line |
|---|---|
| `.venv/bin/python -m pytest -p no:cacheprovider` | `2211 passed, 10 deselected in 765.40s (0:12:45)`: 2 more than the last G3, the zero-slippage tests in `e934cb77` |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | `Found 1750 errors.`: equal to the baseline |
| `.venv/bin/ruff check $(git diff --name-only 1ada81f4..HEAD -- '*.py')` | `Found 537 errors.`: equal to the last G3 |

**A run with a non-default selection was discarded.** An earlier full run at this head used `-o addopts="" -m "not slow"`. That expression selects integration items, and its result was `1 failed, 2210 passed, 1 skipped, 9 errors`. All 9 errors were the conformance fixture refusing with `ALPACA_API_KEY/ALPACA_API_SECRET are not configured`, because the worktree has no `.env`. No broker was contacted and no order guard was reached (`TRADEPULSE_CONFORMANCE_ORDERS` was unset, and the market was closed). Never pass `-m` to the full suite.

**The Rev.146 independent re-reviews:**
- Every audit requirement was addressed. An exact recomputation then replaced the 2× bound (I1, `54a24f48`).
- The M1 intent-close fix is `31c1d32a`.
- A zero slippage allowance is accepted as valid (`e934cb77`).
- The scoped re-review found no new breakage. It fuzzed the recomputation on 20,000 Decimal round-trip cases with 0 mismatches. Verdict: ready to merge.

### G3: passed at the remediation-review follow-up head (`63aa22a7`, 2026-11-03)

This is the same worktree and `.venv`, with no dependency changed, and the project's default selection. No `-m` was passed.

| Check | Summary line |
|---|---|
| `.venv/bin/python -m pytest` | `2222 passed, 10 deselected in 755.30s (0:12:35)`. That is 11 more than the last G3: 4 for the lease fence, 6 for the exit rungs, 1 repository test and 1 rejection-window test, minus the deleted `as_snapshot` test |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | `Found 1750 errors.`: equal to the baseline. A finding-by-finding diff against `4ab90ac4` is empty after seven `FURB157` findings in the new tests were fixed |

## Before live

- **The alpaca-live liquidation scope is unverified,** so sub-$1 entries are refused on live. G2 verifies paper only, and paper may differ from live.
- **`account_multiplier == 1` is refused** (`DBAE_ACCOUNT_TYPE_UNVERIFIED`). Cash accounts and limited-margin live $20 accounts cannot trade until that account type is verified.
- **IBKR needs its own `DbaeCapabilities`.** `NoCapabilityEvidence` refuses every IBKR entry until A2 adds them.
- **A regulatory check (adviser and broker rules) is needed** before TradePulse is offered to other people for their own money.

## After merge

- **Install `hypothesis` in the main checkout's venv** (`pip install -e '.[dev]'`, or `pip install 'hypothesis>=6.100'`). It is now a dev dependency, and without it `python_tests/test_dbae_properties.py` fails at import.
- Build the review zip, run the token scan and record the sha256 at the post-fix head (ruling 20). Then G1b, G2 (if the operator authorises it), and the fresh generation freeze (§15).

## Not done (DBAE-P3)

These wait for the final review and the operator's decisions:
- merging;
- G1b;
- G2;
- freezing a fresh verification generation.
