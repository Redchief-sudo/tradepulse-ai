# Rev.140 — Unsupported short inventory

**Spec:** `docs/superpowers/specs/2026-10-26-rev140-unsupported-short-inventory-design.md` (rev. 8, amended at `2fd74ee9`)
**Plan:** `docs/superpowers/plans/2026-10-26-rev140-unsupported-short-inventory.md` (rev. 4)
**Branch:** `rev140`, stacked on Rev.145 (`rev145-fill-race`). 14 tasks, one commit each, subjects ending `(Rev.140 task N)`.

TradePulse can no longer sell inventory it cannot prove it holds. If the broker oversells anyway, the excess is kept whole as a quarantined liability. That liability latches the integrity block and is never covered automatically. It is resolved only by an operator command backed by verified broker evidence.

## What changed, by spec section

| Spec | Change | Task / commit |
|---|---|---|
| §3.1 | `execution/sellable.py`: two bounds, sellable = max(0, min(B_l, B_b)). **Amendment A1:** B_l also subtracts external open orders, because the seeded lemma test found a counterexample without it. The amended proof is in the spec. | 3 `1f6760d6` |
| §3.1a | The durable fill cursor C and `unresolved_broker_executions` (K1); `record_pass`, `write_outcome_if_proven` | 4 `fcfc4b7c` |
| §3.1a | `tradepulse reconcile bootstrap-fill-cursor --reason`: inception, P1 per asset, cash to the cent. The completeness pass runs each cycle plus a daily full pass. `history_unproven` is computed per asset. | 5 `fa17d060` |
| §3.1b | Gateway sell authorization: a mandatory reservation; refusals before any write; three O1/P/O2/A attempts; S3 `PRIOR_SUBMISSION_UNRESOLVED`; the cover path removed | 6 `48cbb89e` |
| §3.2 | An oversell's excess becomes a `ShortExposureLiability`, never a short lot. A new final stage, `project_liability_hold`, latches and holds. I3 is in `pnl_evidence`. | 7 `08effb3b` |
| §3.4 | The monitor skips quarantined and unsupported-short assets, alerting once per record per day | 8 `35fe916d` |
| §3.3, §3.5 | Valuation and exposure at adverse marks; I2 including quarantined records; `UNEXPLAINED_SHORT_POSITION`; exact-ID matching only on quarantined assets | 9 `69e47c0f` |
| §3.6 | Signed fee allocation with the quarantined records last; recognition conserved; C1 | 10 `9abe5a2a` |
| §3.7, §3.8 | `tradepulse quarantine resolve --activity --reason`: one transaction, one cash entry `fill:cash:<activity>`. Covers are applied oldest first; any surplus becomes unmanaged long inventory. Reconciliation matches the execution and the accounting replay re-derives its cash. The `reset-integrity` gates follow, and release of a fully resolved hold. | 11 `451ea03b` |
| §3.9, §3.10 | `tradepulse quarantine adopt-legacy-lot <lot_id> --reason`; converted lots are excluded at every lot reader. Corporate actions fail closed. | 12 `1ec8532a` |
| §3.11 | Ten append-only tables, with `BEFORE UPDATE`/`DELETE` triggers verified at startup (`SCHEMA_TRIGGER_MISSING`) | 2 `4314124d` |
| §7 | Simulator faults: ACCEPT_OVERSELL, POSITIONS_LAG, STEP_BETWEEN, OVERFILL, WITHHOLD_ACTIVITY, HOLD_CALL | 1 `bf75f887` |
| test 21 | `scripts/rev140_replay_check.py`, run on every preserved database | 13 `3ea130f2` |
| test 22 | Decimal guard with FloatOperation trapped in every thread | 14 |

## Behaviour changes in the live runtime (spec §6)

- **Sellable is min(local, broker), less pending sells.**
  - With nothing pending, this is today's held quantity.
  - A protective exit above it is reduced and alerted (`SELLABLE_BELOW_REQUESTED`), not refused.
  - "Protective" means it has an `exit_reason`, or the requested quantity is within the broker position. A purpose-less sell above P is refused.
- **Each sell makes three more broker reads:** `get_open_orders` twice and `get_activities` once.
- **Sells require the execution reservation.** The monitor already holds it, so today's path is unchanged.
- **The new commands refuse to run without their preconditions:**
  - `run`, `scan` and `monitor` refuse to start until `reconcile bootstrap-fill-cursor` has been run once (`FILL_CURSOR_NOT_BOOTSTRAPPED`).
  - `quarantine resolve` and `adopt-legacy-lot` take the account lease themselves, and refuse while a runtime holds it.
- **`reset-integrity` refuses** while any of these holds:
  - an unresolved broker execution is open, at any age;
  - a quarantined record is open;
  - I5, C1 or I1 fails.
- **No automatic short cover.** A BUY against a short is refused, and a negative or quarantined asset is never traded.
- **An oversell becomes a liability plus a latch.** None occurs in any preserved database.
- **Fill matching:** heuristic matching is disabled on assets with quarantined or unresolved inventory.

## Deviations from the spec and plan (for the reviewer)

1. **A1 (Task 3):** the local bound subtracts external open orders. The seeded lemma test found the counterexample, and the spec includes the amended proof.
2. **Unstable reads (Task 3):** an order present in only one of the two open-order reads is `SELLABLE_SNAPSHOT_UNSTABLE`.
3. **Unresolved-execution signatures (Task 4):** `write_outcome_if_proven` takes the re-read activity, because its raw digest is part of the evidence. `record_pass` classifies by exact ID inside its own transaction.
4. **Task 5:**
   - **Read ranges:** each pass reads since the latest proven P1 baseline plus an overlap window from C − Δ (default 7 days). The daily full pass, after 16:05 New York, re-reads from inception.
   - **Spec test 4n cannot occur.** The since-baseline read lists a fill as soon as Alpaca does.
   - **An unproven read** marks every asset unproven ("*"). It does not latch.
5. **Protective sells (Task 6):** P < 0 is `UNSUPPORTED_SHORT_POSITION`, following the spec rather than the plan's P ≤ 0. P = 0 is `INSUFFICIENT_SELLABLE_QUANTITY`. Every read after C also filters `id > C` itself.
6. **Task 7:**
   - **R7 fixtures:** these were not built from `c10bb2fa`. A payload-level legacy-resume test with three cases replaces them (see Omissions).
   - **Legacy settlements:** completed ones count `liability_hold_projected` as done.
   - **New hold type:** `IntegrityHoldType.SHORT_EXPOSURE`.
7. **Task 10:**
   - **Coin fees:** coin-fee replay refuses `ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY` on any asset with a liability. This is stricter than per-lot.
   - **Evidence builder:** it keeps the old allocator, so oversold runs fail closed.
   - **C1 on cover pools:** `check_c1` treats a non-zero U on a cover pool as a violation. Cover fees are recognised as dated adjustments.
8. **Task 11:**
   - **Spec test 12's numbers don't add up:** a cover of 4 cannot resolve "2, then 1 of the 3". The test uses 4 → (2, 2), then 1 → the rest.
   - **Hold release:** a fully resolved SHORT_EXPOSURE hold no longer counts as active in valuation or the unlock proof. `reset-integrity` deletes it in the same transaction as the session transition, and the latch blocks trading until then.
   - **`replay_accounting`** verifies before it repairs. A replay that changes nothing now runs on an order with an active hold; before, it raised.
   - **Unresolved outcome:** the resolve command writes it in its own transaction.
   - **Oversized sale:** a sale larger than the unmanaged inventory is refused (`SALE_EXCEEDS_UNMANAGED_INVENTORY`) rather than opening a new short.
9. **Task 12:**
   - **"Evidence database"** means bound to a generation (`generation_id`) or under `data/soaks/`. It does not mean `ever_evidence`, which any financial insert sets, so it would refuse every real database.
   - **No fee re-allocation at conversion.** Cash fees attach only to closure attributions, and a convertible lot has none (precondition 3).
   - **Adoption** writes the same SHORT_EXPOSURE hold an oversell gets. It deletes the holding row when no unconverted open lot remains.
   - **I8 relatch:** it also covers any open quarantined record (`QUARANTINED_INVENTORY_OPEN`), at startup and on every pass.
   - **Corporate actions** (types from https://docs.alpaca.markets/docs/account-activities, accessed 2026-10-29). Only executions, fees and account cash flows are harmless. Splits, mergers, symbol and name changes, spin-offs, reorgs, security transfers, option events, distributions naming the instrument (DIV, DIVNRA, CGD) and unknown types all block resolution.
10. **The per-Rev review stop in plan Task 14 is superseded.** The operator decision of 2026-10-28 is one combined review zip for Rev.145, 140, 141, 144, 143 and 142.

## Existing tests changed in Task 6, and why

All of these tests sold without a reservation, or without the lots their scenario implies.

- **`test_execution_gateway`:**
  - `_setup` seeds the cursor, mocks the two new reads, and wraps sells in the reservation.
  - Five tests seed lots.
  - The crypto re-read test gets one more positions response.
  - "Not covered … stays blocked" now refuses with `INSUFFICIENT_SELLABLE_QUANTITY`, still with no order.
  - **"Understated local holding" now sells the 3 the lots prove, not the broker's 10.** This is the intended change.
- **`test_monitor_coordinator`:** cursor and read mocks; six tests seed lots; the short-cover test now asserts the refusal.
- **`test_runtime_on_simulator`:** a funded simulator with the cursor bootstrapped. The fee-cursor test ignores the fill-cursor row.
- **`test_restart_survival`:** cursor mocks and seeding.
- **Later tasks:** `test_cli`, `test_run_protection` and `test_runtime_lease` were updated for the fill-cursor startup gate. The `reset-integrity` test now asserts the exact refusal event.

## Replay of preserved databases (spec test 21)

`scripts/rev140_replay_check.py --copy-from <db>` was run on 2026-10-29. Each database was copied read-only with SQLite's online backup API into a 0700 scratch directory.

| Database | Issues | Rows inserted | Ledgers identical | Source unchanged |
|---|---|---|---|---|
| soak 20260925T043540Z | none | 0 | yes | yes |
| soak 20260928T142358Z | none | 0 | yes | yes |
| soak 20260929T145151Z | none | 0 | yes | yes |
| soak 20261001T160811Z | none | 0 | yes | yes |
| soak 20261007T124709Z | none | 0 | yes | yes |
| soak 20261013T133032Z | none | 0 | yes | yes |
| A0 check 20261026T203002Z | none | 0 | yes | yes |
| service database | none | 0 | yes | yes |

- **No legacy short lot** and no `UNSUPPORTED_SHORT_*` finding appears in any of them.
- **Where the reports are:** `~/tradepulse-evidence/2026-10-29-rev140-replay/`, outside git.
- **Manifest SHA-256 prefix:** `1881310f0dfe9326`. The manifest lists each report's SHA-256 and each source's SHA-256.
- **Fixtures:** the legacy fixtures (`python_tests/fixtures/rev140/`) are authentic `c10bb2fa` settlement output, built by `make_legacy_short_fixture.py`. They replay identically twice.

## Verification (2026-10-29, at the Task 14 commit)

- **pytest:** 1561 passed, 0 failed, 0 skipped. `main` has 1359; Rev.145 adds 3.
- **mypy (project config):** clean. mypy over the whole package finds 117 errors, the same count as `main`.
- **Ruff:** no new finding in the Rev.140 modules (`tradepulse/quarantine/`, `execution/sellable.py`, `reconciliation/unresolved.py`, `bootstrap.py`, `completeness.py`, `fee_replay.py`, `cash_fees.py`, the scripts and the new tests).
  - Edited legacy files gain only FURB157 (`Decimal("0")`), the style the surrounding code already uses.

## Omissions and follow-ups

- **Spec test 20 (R7) checkpoint fixtures** from `c10bb2fa` at each stage are not built. A payload-level resume test covers the same contract.
  - The fixture generator from Task 12 can now produce them, if the reviewer wants them before merge.
- **The corporate-action adjustment procedure** (spec §3.10, requirements 1–7) is out of scope by design.
- **Adopting unmanaged long inventory into strategy management** is not in Rev.140. Rev.142 specifies adopting unmanaged positions.
- **Pre-existing bug, found here:** `infer_alpaca_asset_class` keys OCC option symbols as equities in production activity parsing. Rev.140 code uses the new `broker_symbol_asset_*` helpers; the fix in `parse_activity_row` is a follow-up.
- **Operational:** before the next run, run `tradepulse reconcile bootstrap-fill-cursor --reason "..."` once on the run database. Otherwise `run` refuses to start.
