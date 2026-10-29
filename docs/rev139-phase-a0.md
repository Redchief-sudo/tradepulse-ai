# Rev.139: Phase A0 verification

Phase A0 put every broker call behind `BrokerPort`, added the simulated broker, the account lease and the conformance kit (Rev.130 to Rev.138). This revision adds the run's tooling, and this document records the one live verification run on the Alpaca paper account.

**Status: run pending.** Phase A starts in parallel (operator decision, 2026-10-24); a defect the run finds is fixed on main and carried into Phase A.

## What changed in the live runtime path

Most of Phase A0 is tests, the simulator and tooling. These are the changes a live Alpaca run exercises:

| Change | Rev | What the run shows |
|---|---|---|
| The account lease taken by `run`, `scan` and `monitor`, and verified before every order | 136 | `account_lease_acquired` at each start, no `ACCOUNT_LEASE_NOT_HELD`, orders still placed |
| `AlpacaClient`'s activity paging moved, unchanged, into `paginate_activities` | 134 | Reconciliation's activity population and fee capture still conserve (criteria 3 and 7) |
| Venue names canonicalized in asset keys; Alpaca keys byte-identical | 131 | No position or holding drift (criterion 3) |
| `AsyncSQLiteDatabase.initialize` closes its connection | 136 | No database file changing under a reader |
| Type annotations moved to `BrokerPort` | 132 | Nothing at runtime |

## The run

- **Window:** Monday 2026-10-26 09:00 ET (13:00 UTC) to Tuesday 2026-10-27 16:30 ET (20:30 UTC), 31.5 hours.
  - It covers two full sessions and Monday evening's fee batch.
  - The operator asked for "Monday open to Tuesday close". Both ends were moved outside the session, because the Rev.127 soak ended at the open and a mid-move snapshot failed it.
- **Window check:** `scripts/a0_verification_window.py --start 2026-10-26T09:00-04:00 --hours 31.5` passes. The original "open to close" window is refused.
- **The restart exercise:** the soak runner times the 31.5 hours from the runtime's guarded startup, then reconciles and runs a 30-minute restart session. The process therefore ends around 17:00 ET Tuesday, still outside the session.
  - The restart is safe for the lease: the runner waits for the old process to exit, and the kernel releases the lease when it does.
- **Inherited positions:** the account's open positions are not closed first. The soak's opening checkpoint records them as the deliberate inherited state.
- **Before starting:**
  - the laptop is on wall power and awake;
  - no other TradePulse process or unit is running;
  - nothing has traded on the account that day (the preflight refuses otherwise);
  - the operator gives the go-ahead.
- **Command:**

  ```
  D=data/soaks/$(date -u +%Y%m%dT%H%M%SZ)-a0-check && mkdir -p "$D" && chmod 700 "$D"
  .venv/bin/python scripts/run_accounting_soak.py --run-number 1 --hours 31.5 \
    --database "$D/soak-accounting-1.db" --report "$D/soak-accounting-1.json" --port 8766
  ```

- **After it ends:**

  ```
  .venv/bin/python scripts/a0_order_census.py --database "$D/soak-accounting-1.db" \
    --start 2026-10-26T13:00:00+00:00 --end 2026-10-27T20:30:00+00:00
  ```

  The census was checked read-only against the Oct 13 soak (7 submitted, 7 accepted, 0 duplicate, 0 unauthorized).

  **Completeness (audit TP139-01):** Alpaca's order history has no continuation token, only `after` (inclusive) and `until` (exclusive), on `submitted_at`, as measured on 2026-10-26, and a 500-row limit. Rev.145 corrected the census's bound check to match.
  - After a full page, the census re-reads from one microsecond before that page's last timestamp, so orders sharing a timestamp across a page edge are never skipped.
  - If 500 or more orders share one timestamp, or a page brings nothing new, the census fails ("not provably complete") rather than passing.

  An order placed by hand during the run counts as unauthorized, and reconciliation flags it as out-of-band. Leave the account alone for the whole window.

## Acceptance criteria

| # | Criterion | Outcome |
|---|---|---|
| 1 | Logs: zero `ERROR`/`CRITICAL` records; `WARNING` counted by event; `alpaca_rate_limited_retrying` ≤ 5; any warning event new since Rev.129 explained here | **not passed.** Zero `ERROR`/`CRITICAL`; zero `alpaca_rate_limited_retrying`. Warnings: `equity_reconciliation_failed` 275 and `generation_reconciliation_incomplete` 266. Both appear in the 10-07 and 10-13 runs (market-hours observation timing), and 189 of each came after the 09:30 manual sells. The run has no final report, so the criterion can't be closed. |
| 2 | Zero `trading_supervisor_lane_failed`; `all_lanes_continuous` | **not passed.** Zero `trading_supervisor_lane_failed`. `all_lanes_continuous` can't be computed: no shutdown evidence after the hard reboot. |
| 3 | Zero `reconciliation_issues`; the only `drift_detected` allowed is an `asset_fee` supersession (`new_accounting_evidence`) | **not passed.** Before Wed 09:30 ET: only 8 `asset_fee` drifts, all `new_accounting_evidence` (allowed). After the manual sells: 752 fill, 378 position and 6 fee drifts, and 189 unresolved equity results, all caused by those sells. |
| 4 | One completed settlement per fill; `missing_attributions == duplicate_attributions == 0` | **passed on the evidence.** 14 fills, 14 completed settlements; missing and duplicate attributions both passed (no round trips). |
| 5 | Order census: `duplicate_orders == unauthorized_orders == unresolved_submissions == 0` and `accepted_orders + definitive_rejections == submitted_requests` | **not passed.** Census (Rev.145 script): `submitted_requests` 4, `accepted_orders` 4, `definitive_rejections` 0, `duplicate_orders` 0, `unresolved_submissions` 0, **`unauthorized_orders` 2**. These are the manual AAPL and UNH sells placed from Alpaca's app at 04:01 ET Wed by a person with account access. |
| 6 | Zero integrity holds or incidents; never `financial_integrity_blocked` | **not passed.** (a) Tue 18:19 UTC `reconciliation_degraded` (`UNRESOLVED_GENERATION_MEMBERSHIP`): a fill newer than the cycle's population. Root cause found and fixed in Rev.145. (b) Wed 09:30:56 ET `financial_integrity_blocked` after the manual sells, which is correct behaviour. |
| 7 | `fee_receipts_conserved` after the fee batch posts | **not passed.** The fee batch posted (one $0.01 cash fee, recorded once), but conservation needs the observed generation net, which needs the final report. |
| 8 | `runtime_boundaries_complete`, with the restart exercise performed | **not passed.** The machine froze under memory pressure at 12:50 ET Wed and hard-rebooted at 13:02 ET, so the restart exercise never ran. |
| 9 | `account_lease_acquired` at each runtime start; no `ACCOUNT_LEASE_NOT_HELD` | **passed.** `account_lease_acquired` once at the only start; zero `ACCOUNT_LEASE_NOT_HELD`. |
| 10 | Peak runtime RSS below 1.5 GB | **not decided.** About 430–460 MB on Tue; the final peak was not measured (no report). |
| 11 | Start and end met the verified window; report preserved with its SHA-256 | **not passed.** The run ended early with no report. The database, log, census and offline analysis are preserved in `~/tradepulse-evidence/2026-10-28-a0-run-interrupted/` (manifest sha256 `53290557…`). |

Coverage invariants (`all_asset_classes_exercised`, `slippage_*`, round-trip counts) are recorded, not gating.

**Run outcome (recorded 2026-10-28): NOT PASSED.** Run: Mon 2026-10-26 16:30 ET to Wed 12:49 ET. Phase A0's verification moves to the single 24-hour run after Rev.145, Rev.140, Rev.141, Rev.144, Rev.143 and Rev.142 are built and reviewed. Over the 41 hours before the manual sells, the system itself showed:
- zero errors, zero lane failures, zero rate-limit retries;
- one settlement per fill;
- buy slippage averaging 6.7 bps favourable, worst 0.86 bps adverse.

## Whole-phase review (2026-10-24)

The review covered every Phase A0 change to production code the live runtime uses; the simulator itself was audited separately at Rev.133 to Rev.135.
- **No defect found.** Points checked:
  - Alpaca paper account numbers match `AccountRef`'s pattern.
  - The lease file descriptor is close-on-exec, so no child process inherits it.
  - The soak's restart releases the lease before the next start.
  - The supervised service and the standalone `settle`, `reconcile` and `dashboard` commands take no lease.
  - Alpaca asset keys are byte-identical.
- **One pre-existing bug was found and fixed during Phase A0:** the leaked connection in `AsyncSQLiteDatabase.initialize` (Rev.136).

## Phase A0 verification so far

- **Full suite:** green at every revision. At Rev.138: 1,332 passed, 10 deselected (the integration tests).
- **Conformance kit:** passes on the simulator by default. Read-only on the paper account (2026-10-23), 6 passed.
  - That run showed a malformed activity `page_token` is a 422, while a well-formed one bounds lexicographically. The simulator models both.
- **End to end on the simulator** (Rev.138): entry, settlement, stop exit, fees, reconciliation, replays, a late fee behind the cursor, a lost submission and a lagged fill. All pass with no production change.
