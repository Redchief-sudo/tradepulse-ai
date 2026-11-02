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
- task 10: the property test with the fix it found, and this record.

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
| 7 | §3, §7, §8.1, §10, §11 | `execution/gateway.py`: steps 1–9 of §8.1 for every new buy. `risk_snapshot.dbae` records the four quantities and the decision; `dbae_entry_refused`, `dbae_band_changed` and `dbae_quantity_identity_mismatch` audit events. |
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

### Known behaviour to note

- **Crypto scale-out under a transient capability failure (task 8).** A transient crypto capability-read failure past `single_unit_target_r` gives a full exit instead of a partial one. The position cannot be proven splittable, so it exits whole, consistent with ruling 14.
- **Inverted exit config (task 8).** With `single_unit_target_r < scale_out_r` and the gain between them, unknown capabilities make a splittable position exit whole at `single_unit_target_r`. No profile sets this today (defaults 1 and 1.5). A `RiskLimits` validation `single_unit_target_r ≥ scale_out_r` would close it.

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
  - `test_dbae_gateway.py` has a `# type: ignore` on `rig.caps`; the final review must fix it.
  - `_baseline_capture_due` reads `dbae_session_baselines` outside the `DatabaseError` catch, so a store failure raises instead of refusing; the final review must fix it.
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
  - The property tests suppress hypothesis's `function_scoped_fixture` health check. The calendar fixture and the monitor reset are identical for every example, and each example builds its own database, broker and clock.

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

### G3: passed at the task-10 commit

Run in `~/tradepulse-ai-dbae` with its own `.venv` (Python 3.12.3, tool versions pinned to the main venv's):

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 2082 passed, 0 failed, 0 errors (about 12 min) |
| `.venv/bin/mypy` | `Success: no issues found in 133 source files` |
| `.venv/bin/ruff check` | 1750 findings, equal to the HEAD baseline before task 10 (`ce709ede`): no new findings |
| `.venv/bin/python -m pytest python_tests/test_dbae_properties.py -v` | 5 passed, about 40 s |

## Before live

- **The alpaca-live liquidation scope is unverified,** so sub-$1 entries are refused on live. G2 verifies paper only, and paper may differ from live.
- **`account_multiplier == 1` is refused** (`DBAE_ACCOUNT_TYPE_UNVERIFIED`). Cash accounts and limited-margin live $20 accounts cannot trade until that account type is verified.
- **IBKR needs its own `DbaeCapabilities`.** `NoCapabilityEvidence` refuses every IBKR entry until A2 adds them.
- **A regulatory check (adviser and broker rules) is needed** before TradePulse is offered to other people for their own money.

## Not done (DBAE-P3)

These wait for the final review and the operator's decisions:
- merging;
- G1b;
- G2;
- freezing a fresh verification generation.
