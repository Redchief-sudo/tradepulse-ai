# Rev.104: historical SOL replay, fee attribution and valuation observations

Rev.104 follows Rev.103. It implements the audited historical replay and adds the USD crypto-fee correction subsequently authorized by the user. The operating database and broker account were not changed during development; reproduction used temporary SQLite copies.

## Proven cause and attribution

Gross SOL buys total `36.723631330`. Four authenticated Alpaca CFEE receipts debit `0.091809079 SOL`, leaving `36.631822251`. Two later sell fills sold exactly that net position. The old local projection omitted the unit fees, leaving a false `0.091809079` holding after the sale. Rev.103 refused to restate already-completed closures.

A fresh, unfiltered, fully paginated paper-account capture contains 83 fills, six CFEE activities and the other account activities. All six SOL fills match the local broker fill IDs, quantities, prices, timestamps and TradePulse intents' broker order IDs. No unexplained SOL inventory movement exists in that complete feed. Net SOL from the complete activity population equals the broker's current zero position.

Fifty non-SOL fills have no counterpart in the current database. The implementation therefore does **not** assert that the whole account is TradePulse-exclusive. It proves instrument-population membership from the complete history instead: every fill for the instrument must be locally owned, every local fill must occur in the feed, unknown inventory movements fail, and the resulting net units must match a fresh broker position. Asset/time coincidence alone is insufficient. An isolated fee receipt cannot authorize a mutation.

The first recorded unit-fee batch is `0.041661622 SOL` at `2026-09-03 19:35:14.115340 UTC`. Equal timestamps within a batch do not establish a finer broker ordering. No fee percentage or fee-to-fill relationship is inferred.

The new capture also contains two USD CFEE debits: `$3.54` and `$5.77`. The complete preceding crypto/USD sell history contains exactly one broker order, so their order-level attribution is unambiguous. No rate or date-window heuristic is used. A receipt with multiple possible source orders requires an authoritative order link; otherwise `CASH_FEE_ORDER_ATTRIBUTION_AMBIGUOUS` fails closed. This is a real support boundary for future transactions, not an assertion that every future cash fee can be attributed.

Alpaca documents fees in the credited asset/currency and the CFEE receipt shape at https://docs.alpaca.markets/us/docs/crypto-fees . The observed unlinked receipt shape does not carry a per-fill fee assignment.

## Atomic correction

- `fee_population.validate_fee_population` validates complete instrument ownership and the cash-fee order population against broker activities and local fill/intent evidence. Pagination or data-shape errors are not interpreted as empty feeds or zero positions.
- `historical_fees.preview_fee_replay` is the pure planning function used by the actual replay boundary. It reconstructs long FIFO allocations chronologically using the existing signed-lot planner, preserves lot IDs/opened quantities/acquisition prices, and recomputes gross realized results. Unknown shorts, oversales, missing sources and ambiguous fill/fee ordering fail explicitly.
- `fee_replay.replay_asset_fees` revalidates local sources under one SQLite write transaction, executed off the event loop. It updates lot closures, trade attributions, settlement derived realized results, intent realized summaries and holdings together. Completed flags, source fill quantities/prices, order economics and existing cash entries remain intact. The original derived rows are retained in an immutable correction receipt. New attribution pairs use original entry provenance; unobserved historical price extrema are left unknown.
- Each native fee has a stable immutable ledger record and proven population membership. Each USD fee creates one idempotent cash-ledger debit and an expense receipt. Cash expense is allocated across the verified closing order's FIFO attributions proportionally to executed proceeds, rounding intermediate allocations down to the receipt amount precision and assigning the exact remainder to the last allocation. This is an explicit cost-allocation policy, **not** a claim that Alpaca supplied per-lot/per-fill fee amounts. Gross price P&L remains separately identifiable.
- Repeated successful application changes no financial rows and adds no evidence rows. Feed observations append only on outcome/content transitions. Recovery transitions remain visible. Records grow with new evidence, not unchanged polling cycles.
- Prove-edge revalidates population receipts, lot fee allocations, cash ledger entries and cash-expense allocations. It deducts actual native-fee basis and USD expenses once. Missing or tampered fee evidence excludes the affected crypto results. The separately frozen modeled fee/slippage overlay is unchanged and remains an additional model, not a claimed broker charge.

Reconciliation remains a separate command/cadence; this change does not introduce an internal scheduling loop or order-side balance adjustment. New unit fees can be replayed even after an exit. Pending settlement defers application; unsupported or ambiguous evidence persists failure and retains financial-integrity handling. Existing manual integrity latches are not silently cleared.

## Before and after: operating-history copy

| Quantity/result | Before | Corrected copy |
| --- | ---: | ---: |
| Remaining SOL | 0.091809079 | 0 |
| Gross realized price P&L | -134.175696115831 | -134.157609727268 |
| Native-fee acquisition-basis expense | omitted | 9.6674960187 |
| USD crypto-fee expense | omitted | 9.31 |
| Result after these actual fees | not corrected | -153.135105745968 |

Every sell quantity is conserved. Original fills and original cash entries are byte-for-byte unchanged; the two authoritative USD cash debits are additional entries. Repeated application has no further effect. These calculations are not a claim that an official prove-edge generation passed or that unrelated account expenses have been audited.

## Valuation separation

`valuation.marked_snapshot` separates account equity arithmetic from the difference between independent account/position responses. It preserves the exact cross-response difference and labels the observations uncoordinated. Local HTTP response-receipt times are persisted explicitly; they are **not** broker market-price timestamps or evidence of a common price instant. No magnitude or timing tolerance dismisses a discrepancy as harmless. The dashboard displays the separate observation notice. Actual equity arithmetic failures and quantity reconciliation retain their independent checks.

`holdings_value` and sector exposure remain broker market values. Cost basis and active/original stops retain the Rev.102 fixes.

## Evidence distribution and limits

Raw evidence is distributed separately from source, never committed:

- Original receipt capture: `forensic_evidence/2026-09-18/alpaca-sol-activities-20260918T233558Z.json`.
  SHA-256 `cf431e14eeb839109be02cd1e17fe22e04fcda14c6a9aafccf946ef24e4277a9`.
- Complete account-history capture: `forensic_evidence/2026-09-20/alpaca-account-history-20260920T222348Z.json`.
  SHA-256 `c09cca683c172fa0dd068cf99616f77cc04e667768e7b9f2570db4baf9ce52d2`.

The separate evidence archive includes both captures and checksums. The originally supplied database/video remain unchanged and excluded from Git and source archives. No broker order was placed, replaced or cancelled. No broker balance, operating database, session state or integrity latch was changed by this development session.

Remaining limits are explicit: unlinked USD fees with multiple candidate orders cannot be guessed; a newly discovered fee that would change an already-committed immutable fee allocation requires separate ledger-restatement support; no common broker price timestamp is available from the observed account/positions response contracts. No blanket official-run readiness claim is made.

## Validation

The release report records the final full Python suite, relevant focused/subsystem tests, frontend suite/build, compilation and diff checks. Lint is checked only for the changed fee/replay modules; this is **not** a claim that project-wide lint passes. Existing project-wide lint debt is outside this correction.
