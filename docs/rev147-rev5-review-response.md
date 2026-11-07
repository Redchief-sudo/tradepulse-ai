# Rev.147 rev. 5 — response to the forensic audit's design findings D01–D05

**Spec:** `docs/superpowers/specs/2026-11-01-run-wind-down-design.md` (rev. 5).
**Findings answered:** D01–D05 of `TradePulse_Rev147_Forensic_Audit.pdf` (2026-11-04), "Amend Rev.147 before implementation". The Rev.150 assessment (2026-11-06) asked for this document before the next audit.
**Scope:** design only. No production code changed, no broker calls, no orders, no database writes.
**Base:** main `b010090d` (Rev.149 merged: F01–F06 and R149-01..05).

## Findings answered

| Finding | Audit remediation | Rev. 5 answer | Sections |
|---|---|---|---|
| **D01** HIGH, predecessor discovery | An account- and environment-bound registry outside the databases, with a durable head, predecessor identity and chain digest. Crash-safe updates, a bootstrap rule, and refusal on missing, forked or stale evidence. | **The registry:** one per `<venue>-<environment>-<account_number>`, in the lease directory's resolver location. **Entries:** atomic, digest-chained entry files (`genesis`, `frozen`, `sealed`, `abandoned`), appended under `flock` with no-replace links. **Freeze:** reads the registry first. It refuses when the registry is missing (unless bootstrapping), the head is unsealed, the chain is invalid, the chain is forked, the account differs, or the head is stale. **Stale head:** corroborated by the broker, because every FILL after the head's seal must be cited by an amendment. **Predecessor artifacts:** located by digest and `database_id`, not by path, so renamed or moved artifacts are verified rather than lost. **Ordering:** the `frozen` entry comes before the database bind, the `sealed` entry after the handover, both idempotent on re-run. **Abandon:** allowed only for a generation with no intents. **Stated limits:** per machine, like the lease; a deleted registry only downgrades (rebuilt stops, unresolved late fees). | §7.2, §7.3, §7.4, §7.7, §10, §11, §12 q. 7 |
| **D02** HIGH, late-fee cash | A signed, exactly-once predecessor-cash adjustment in the successor's bridge, excluded from strategy P&L, included in physical cash and generation equity. Linked to the origin amendment and the raw activity digest. Debit, split, reversal, and before/after-opening cases. | **The bridge:** each `predecessor_obligation` activity gets one `generation_predecessor_cash:<id>` record and one `broker:predecessor_cash:<id>` cash entry, with the broker's signed amount. **Links:** it cites the raw digest and the predecessor amendment's entry digest. The amendment is written first, and the bridge never exists without it. **Counting:** included in `expected_cash`, excluded from the successor's equity and P&L like a capital flow (`excluded_predecessor_cash`). **Validation:** `predecessor_cash_bridge_missing` and `predecessor_cash_bridge_unlinked`. **Independent check:** the assessment applies the same term. **Reversals:** bridged as a signed credit; rev. 4 posted nothing, which left cash unexplained. **Before the opening:** no bridge, because the fee is already in the opening cash. **The $1,000/$999 case** is a named end-to-end test. | §7.6, §7.6.1, §10, §11, §12 q. 9 |
| **D03** HIGH, carried lots | A versioned carried-lot opening record: immutable original provenance and a distinct successor identity, original age and basis kept apart from the successor's opening quantity and adoption time. Prior closures, native fees and realized results defined. Projections updated, and adopted lots aggregated per asset exactly once. | **One adoption row per lot** (`rev147-handover-v1`). **Identity:** a new successor ID with the existing `adopted:` / `adoption:` prefixes. **Quantity:** re-based to what remains, with closures and fees empty and realized P&L 0, so conservation holds. **Kept:** original price, date and MFE/MAE. **Provenance:** the chain from the root plus the predecessor history, as evidence only and never re-applied. **Opening proof:** a versioned branch of `_AdoptedOpening`, mirrored in the independent projection. **Aggregation:** exclusions summed per asset with V0 subtracted once, and the checkpoint guard summed. **A further gap found in the code:** `_project_holding` re-derives `target_price` as None for an adopted lot, so a carried target would be lost on the first successor fill. The carried adoption now supplies stop and target, and the average-price identity is checked at freeze (§7.2.1 check 6). | §7.1, §7.2, §7.2.1, §7.2.2, §10, §11 |
| **D04** HIGH, entry-dispatch race | A serialized entry-dispatch barrier shared by the wind-down and the gateway. Close approved-unsent work, or wait for every dispatch to be durably classified; keep unknown-submission recovery. A boolean read followed by awaits is not atomic. | **The barrier:** the `RISK_APPROVED` → `SUBMITTED` write becomes a `BEGIN IMMEDIATE` compare-and-set that re-reads the run state, and the holds, in the same transaction, and rejects at dispatch (`RUN_WIND_DOWN_ACTIVE_AT_DISPATCH` / `RUN_PAUSED_AT_DISPATCH`). **The transition:** the event that stops entries is written in a `BEGIN IMMEDIATE` transaction that rejects every `RISK_APPROVED` BUY. SQLite's write lock means each BUY is either dispatched before the transition or never sent. **Dispatched BUYs:** each enters the observed-order register by client order ID, and the wind-down cannot complete until it is classified. A census showing no broker order proves nothing. Since R149-03, nothing is awaited between the `SUBMITTED` write and the POST. **Tests:** a deterministic pause at every await before the POST, repeated for pause, cancel-during-acceptance, crash and unknown submission. | §8, §10, §11 |
| **D05** MEDIUM, mark timestamps | A mark evidence schema and source, with observed time, receipt time and session validity. A prior-session mark allowed for an out-of-session evaluation, kept separate from the fresh executable evidence an in-session exit requires. Never label a receipt time as a market time. | **The record:** price, source (market-data quote or trade), `observed_at` (the data's own timestamp), `received_at`, a calendar session label, and the acceptance result. **Positions price:** `positions.current_price` is recorded as `broker_position_price` with its receipt time only, and never accepted. **Acceptance:** `prior_session_mark` before the open (observed in the latest completed regular session; the outcome stays `out_of_session`), and `fresh_executable` in session (the existing quote bound). **No acceptable mark** (an illiquid option): `mark_unverified`, assumed per position. Its `assumed_at` is null, its asset is refused for entries (`CARRIED_MARK_UNVERIFIED`), it is assumed at its first fresh in-session quote, and it is alerted if none arrives. Rev. 4 refused the whole start, which would leave every position unmonitored. | §7.5 check 4, §7.5.1, §8, §10, §11, §12 q. 8 |

## Code facts checked for rev. 5 (at `b010090d`)

- **D01:**
  - `verification/service.py::store_for` puts the store at `<database>.paper-verification`.
  - `_require_fresh` refuses any database that is already bound or holds evidence.
  - So a predecessor cannot be found from the successor's own store.
  - `runtime_lease.py::lease_directory` already resolves an account-scoped, local, non-network directory, which the registry reuses.
- **D02:**
  - `valuation.py::_generation_invariants` computes `expected_cash = opening cash + Σ broker_settled_cash(cash_ledger)`.
  - Equity already excludes capital flows (`generation_adjustment_totals`). The bridge follows that pattern.
- **D03:**
  - `PositionLot` enforces conservation with closures and asset fees, and derives `origin` from the ID prefixes.
  - `_AdoptedOpening` compares the broker average, the adopted quantity and the adoption time with the lot.
  - `adopted_opening_exclusions` assigns `exclusions[key]` once per lot, so a later lot overwrites an earlier one.
  - `_project_holding` recomputes `average_price` from the lots, and `_entry_protective_levels` returns `target_price = None` for adopted lots. This last point was not in the audit.
- **D04:** between the `RISK_APPROVED` write and `place_order`, the gateway awaits:
  - the market clock and the entry window;
  - the lease;
  - for exits, a positions recheck;
  - the `SUBMITTED` write.

  The quote recheck after the `SUBMITTED` write is synchronous (Rev.149 R149-03). `_recover_stranded_intents` already resolves a `SUBMITTED` intent that has no broker ID, by client order ID.
- **D05:** `AlpacaPosition` has `received_at` but no observation time. `AuthoritativeQuote.observed_at` is the market data's own timestamp, and `quote_age_problem` holds the 30 s / 60 s executable bounds.

## Decisions for the reviewer (§12)

7. **Registry scope:** per machine with broker-fill corroboration until cloud hosting, or a shared authority before the first run?
8. **Per-position assumption** for a carried position without an acceptable mark, instead of refusing the whole successor start.
9. **Reversal credits** bridged automatically (alerted, not P&L), or held unresolved until the operator acknowledges them.

`FEE_WAIT_MINUTES` (open question 3) is still provisional until G1.

## Next, if approved

1. Rev.147 G1: the read-only CFEE history check (the operator's go-ahead is needed for the broker read).
2. The implementation plan.
3. The build on its own branch and worktree, nothing in the main checkout during verification work.
4. An independent audit, focused on crash recovery, duplicate-order prevention, stop restoration, inventory conservation and fee attribution.
5. A merge on the operator's approval, then DBAE G1b and G2.
