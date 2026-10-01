# Rev.111: accounting soundness audit after soak 1 (2026-09-29)

Rev.111 follows Rev.110 (`5ee3db0`). It records a full audit of the accounting arithmetic, persistence, and verification evidence, using the preserved soak database (`data/soaks/20260929T145151Z/`, not modified), the legacy `tradepulse.db`, the 2026-09-25 soak, and the live paper account's complete activity history. It corrects three defects. No thresholds, fee/slippage overlay rates, risk parameters, or trading logic changed.

## Audit results that needed no change

- **Arithmetic is exact.** Every fill's cash entry, lot conservation (opened = closures + native fees + remaining), holding-versus-lots quantity, and realized P&L attribution was independently recomputed from quantity x price x contract multiplier. Across 43 fills, 36 lots and 29 P&L attributions in three databases: 0 mismatches. All 29 attributions were actually compared, not skipped.
- **No floating point in money paths.** `float()` appears only in ATR and regime indicators. Rounding happens at three sites: broker per-fill cent settlement, broker account-value cent rounding, and fee allocation (round down, remainder to the last portion, so the total is conserved).
- **Option multipliers persist.** The real IWM option carries `contract_multiplier: 100` on its fill, lot, holding, intent, and settlement. Hydration restores every model field. `contract_multiplier` is folded into `metadata`, which is persisted.
- **Every real broker activity shape parses.** All 131 live activities (106 FILL; 18 FEE across the OCC, ORF, CAT, REG and TAF subtypes; 6 CFEE; 1 JNLC) pass the production receipt parsers with 0 errors.
- **Broker cash reconciles to the cent.** 101,335.69 - 304.00 (option premium) - 0.03 OCC - 0.02 ORF - 0.01 CAT - 0.07 REG - 0.01 TAF = 101,031.55, exactly Alpaca's cash.

## Corrections

1. **Checkpoint churn on unchanged populations.** Every recurring activity read records a per-page `received_at` transport time. That time went into the membership record body, the epoch's stored pagination proof, and therefore the fee-population proof hash. Every poll looked like new evidence: soak 1 superseded one option's checkpoint 368 times with no new broker activity (368 false `drift_detected` records), and stored about 155 MB of duplicate membership, population, and epoch records in 10 hours. Each valuation re-reads every reconciliation record, so cycles also slowed over time.
   - `activity_cursor.observation_independent` removes `received_at` from recurring membership and checkpoint pagination evidence. Request, activity ids, and response hashes remain.
   - Identical populations now hash identically. The existing `INSERT OR IGNORE` and the unchanged-epoch early return dedupe them, and the verifiers are unchanged.
   - The opening checkpoint keeps its receipt times. They prove it was read before the generation opened.
   - A genuinely new receipt still supersedes exactly once. An epoch whose stored state no longer matches its immutable receipt is still versioned.
   - The Rev.109 test asserting that a transport-only refresh versions the checkpoint was changed to assert that it does not.
2. **Equity evidence window failed every bound generation.** The equity check bounded snapshots by the caller's start and end. Two snapshots fall outside that window by design:
   - The guarded startup reconciliation snapshot precedes both `started.json` and the runtime-start event (soak 1: 14:51:58.240, against 14:51:58.424 and 14:51:58.436). This fails the official generation as well as soaks.
   - The runner's mandatory post-shutdown reconciliation follows the last segment end (00:48:29.75, against 00:48:00.61).

   The fixes:
   - `evidence.equity_evidence_window_start` bounds a bound generation's snapshots from its opening checkpoint.
   - `soak._assessment_end` extends a soak assessment to the latest persisted generation evidence. Freeze still re-derives this deterministically from the database, and now requires report creation at or after `assessment_ended_at`.
   - Re-analysing the real soak 1 database now yields no assessment errors.
3. **Silent 1x option multiplier.** `contract_multiplier_of` defaulted to 1 for any identity without metadata, including options. It now raises for an option without a multiplier. No live path builds such an option.

## Broker behaviour that constrains soak operation (not defects)

- **Fee records post at end of day; cash moves at trade time.** Alpaca paper debited the OCC fee from cash at the fill but published no fee activity until its end-of-day batch (about 00:43-00:52 UTC). The batch carries a midnight-of-trade-date id (`20260929000000000::...`) that sorts before the day's fills. Until then, generation cash is `mismatch` and each equity record is `incomplete_evidence`, as intended: the ledger never guesses a fee. A soak's final reconciliation must therefore run after the end-of-day fee batch for its last trading day (after about 18:00 PT), or the latest equity record stays incomplete.
- **A trade made before a generation opens, on the same day, latches the generation.** Its end-of-day fees have ids before the opening cursor and no order link, so they classify `unresolved_generation_membership`, which latches the financial-integrity block. This is fail-closed and correct. Before opening a generation, make sure no pre-generation trade is still waiting for its end-of-day fee batch.
- **Coverage depends on trade rate.** In 10 hours, soak 1 scanned 94 times and found 436 candidates. 433 were rejected by frozen entry criteria (confidence below 80, deterministic-signal disagreement, non-actionable recommendation), leaving 3 opportunities and 1 fill. A passing soak needs completed round trips in equities, options and crypto, plus a late-fee reopening, so it needs a run long enough for entries and exits to occur.

## Validation

Full Python suite passed. New coverage is in `python_tests/test_rev111_soundness.py`:
- identical observations with different transport times share one membership record, and changed populations still mint new ones
- transport-only refreshes never supersede equity or crypto checkpoints
- a late fee supersedes exactly once and is then stable
- the generation-opening equity window, while unbound snapshots before start still fail
- the soak assessment includes the runner's final reconciliation
- an option without a multiplier fails closed
