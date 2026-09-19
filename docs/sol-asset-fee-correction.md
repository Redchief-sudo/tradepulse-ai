# SOL quantity correction using authoritative Alpaca asset-fee receipts

This follows Rev.102. The initial forensic files did not contain fee receipts; a subsequent read-only request to the Alpaca paper account obtained them. The operating database and broker account were not changed.

## Proven cause

Four distinct FILL activities match the four immutable local fills and their completed settlements. Gross executed quantity is `36.72363133 SOL`. Four executed CFEE activities then debit SOL units, with `net_amount = 0` and description `Coin Pair Transaction Fee (Non USD)`:

| Receipt created_at (UTC) | SOL debit |
|---|---:|
| 2026-09-03 19:35:14.115340 | 0.006890872 |
| 2026-09-03 19:35:14.115340 | 0.034770750 |
| 2026-09-03 21:26:10.662719 | 0.035043000 |
| 2026-09-03 21:26:10.662719 | 0.015104457 |
| Total | **0.091809079** |

The first recorded debit batch is therefore `0.041661622 SOL` at `19:35:14.115340 UTC`. The receipts have equal timestamps within each batch, so no finer broker ordering is asserted. No percentage fee rate or association with a particular fill ID is guessed.

`36.72363133 − 0.091809079 = 36.631822251 SOL`, exactly the broker quantity in the supplied recording. Fill ingestion previously requested FILL activities only. Settlement correctly projected their gross executed quantities, but no path accounted for the separate native-asset fee debits.

Raw receipts were saved outside Git at `forensic_evidence/2026-09-18/alpaca-sol-activities-20260918T233558Z.json`, SHA-256 `cf431e14eeb839109be02cd1e17fe22e04fcda14c6a9aafccf946ef24e4277a9`. API keys and secrets are not in that file. The two originally supplied evidence files retain their original hashes.

## Correction

`reconciliation/asset_fees.py` parses the observed executed, asset-denominated CFEE shape. It validates identity, timestamps, exact Decimal quantities and zero cash debit; unsupported, malformed, floating-point or inconsistent receipts fail explicitly. CFEE fetches start at UTC midnight of the earliest local crypto lot because activity date filtering differs from the precise `created_at` accounting timestamp. Receipts are processed by creation time with a deterministic ID tie-break.

`apply_asset_fee` uses one SQLite `BEGIN IMMEDIATE` transaction, offloaded by the existing async database boundary. It records the broker activity once under `asset_fee:<activity_id>`, allocates units using the existing FIFO inventory policy, updates affected lots and the aggregate holding together, and records the allocated cost-basis debit and before/after quantities. It preserves protective stops. An insert failure rolls back every lot and holding write. A replay verifies the receipt and allocations and does not debit again.

The lot JSON payload adds `asset_fee_quantities`, mapping activity IDs to debited units. Executed fill quantities and original lot opened quantities remain unchanged. Replayed fills consequently cannot recreate fee-consumed inventory. Fee debits are not synthetic fills, closing trades, cash movements, or changed historical settlements. The existing reconciliation table stores the fee ledger and provenance; no SQL table migration is added.

`run_reconciliation` invokes this boundary before comparing broker/local positions. In-flight settlement defers fee application and returns a degraded result without blocking settlement completion. Unexplained or invalid fee evidence produces persisted reconciliation failure and financial-integrity latching. Ordinary command scheduling, strategy, sizing, risk thresholds, order submission, and broker account state are unchanged.

The prove-edge assessor validates every fee allocation against its authoritative receipt, canonical asset and recorded basis debit. Closed-lot conservation is `executed closing units + fee units = opened units`. Missing or inconsistent fee evidence excludes the affected sample. Actual inventory-fee basis is expensed once; the explicitly frozen modeled fee/slippage overlay remains separately applied. This is an additional modeling overlay, not a claim that its hypothetical costs were charged by the broker. No fee rates or prove-edge thresholds are selected or altered.

`AlpacaClient.get_activities` also rejects malformed list responses and incomplete/repeated pagination instead of treating them as an empty successful feed.

## Real-evidence reproduction and limits

A temporary SQLite copy of the supplied September 17 database was corrected using the retrieved raw CFEE receipts:

- Holding before: `36.72363133`.
- Holding after: `36.631822251`.
- Sum of lot balances after: `36.631822251`.
- Four immutable fee ledger records; replay added zero duplicates and debited zero further units.
- All fills and settlements unchanged, including their original payloads.
- Original evidence and receipt hashes unchanged.

This proves the supplied mismatch can be corrected without inventing an adjustment or altering executions. It is not a claim that the current operating database was modified.

A newly discovered fee preceding an already-projected sale, or preceding an already-applied later fee, returns `ASSET_FEE_HISTORICAL_REPLAY_REQUIRED`. Reallocating completed historical closures is a separate accounting replay; this patch does not silently debit whichever residual lot happens to remain. The provided September 17 SOL lots had no closures, so that evidence reproduction is supported. Already-closed operating history must be evaluated separately before any local-state repair.

## Tests

Twenty fee regression cases cover the four exact quantities, replay and concurrent replay, replay of original fills after fee depletion, transaction rollback, changed receipts, historical-sale refusal, settlement deferral, malformed receipts, creation-time ordering, persisted failure/latching, canonical receipt/lot conservation and fee expense in prove-edge results. The existing cross-asset reconciliation test now distinguishes fee-feed observations from position observations.

The complete suite passed **852 tests, 1 skipped**. Focused checks, compilation, lint and the read-only source-copy reproduction are also recorded in the handoff. No broker order was submitted, cancelled or replaced. Raw forensic files remain untracked.
