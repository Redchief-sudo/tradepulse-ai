**Paper-accounting correction — 2026-09-22**

The repaired database copy is `forensic_evidence/accounting-correction/repaired.db`. The original `tradepulse.db` was not modified. Readiness applies to the corrected source and repaired copy, not to the original database or an already-frozen prove-edge generation.

The decision is **READY FOR PAPER OPERATION, PROVE-EDGE BLOCKED**, subject to the final test results below. Quantity and projection evidence is complete; instrument-level net PnL remains unresolved where account-level fees cannot be attributed exactly. The fresh overall reconciliation outcome is `incomplete_evidence`, not `matched`.

**Root causes and corrections**

- `SettlementProcessor._handlers().project_cash` previously returned without a write. `cash_projected` nevertheless became true. `project_trade` updated only `TradeIntent`; it never wrote `pnl_records`. The tables remain canonical: risk fallback reads `pnl_records`, the dashboard reads it, and fee accounting reads/writes `cash_ledger`. There was no migration superseding either table.
- Production now writes signed fill cash to `cash_ledger`, gross realized PnL for every closed-lot attribution to `pnl_records`, and explicit fill fees as separate expense records. Stable fill/projection and lot/fill keys prevent duplicate effects. Opening fills require durable opening-lot and intent evidence rather than placeholder zero-PnL rows.
- Each checkpoint revalidates its canonical journal destinations inside the same SQLite write transaction that records the flag. Cash/PnL writes precede their checkpoints; completion requires all projection flags and valid underlying evidence. Partial failures remain retryable through the existing durable settlement state machine. SQLite I/O runs off the asyncio event loop.
- SOL/USD's false `0.091809079` residual came from four omitted native-unit fee receipts. Complete broker history supports fee replay against the existing lots. The replay preserves original fills and lot IDs, records the old and new derived values in an immutable correction receipt, and updates lot closures, attributions and canonical PnL together. Its two USD fees total `$9.31`; native-fee acquisition-basis expense is `$9.6674960187`. Corrected SOL gross price PnL is `-$134.157609727268`, and its verified observed net result is `-$153.135105745968`.
- V's broker sell order `c0d2cc89-c18b-4501-8a9a-facaa509fd82` had filled `13.195` shares while its local intent remained `accepted` with zero fills. The existing pending-order recovery path fetches that known order independently of the daily activity lookback and uses the canonical fill/settlement writer. Three immutable broker fills are added; no closing quantity is invented. V's corrected gross realized PnL is `-$31.92020`.
- Five V opening settlements predate the attribution-checkpoint field. The repair performs an explicit version-1 contract migration only after validating their complete fill/lot/attribution/journal evidence. Five immutable `accounting_stage_migration:v1:*` receipts preserve the original and migrated payloads. An explicitly false checkpoint is not silently treated as the old missing-field contract.
- Previously, the valuation receipt's top-level outcome used only account-equity arithmetic. It now composes position quantities, holdings/lots, projection evidence, holds and eligibility requirements. Zero equity difference cannot mask a quantity mismatch. A material mismatch latches financial integrity. Uncoordinated market-value observations are preserved and never automatically declared harmless or transient.
- Non-crypto population checkpoints now persist complete broker activity evidence and cursors. Unlinked account fees keep their epochs provisional. Failed new observations supersede older verified epochs rather than leaving stale eligibility active. Prove-edge validates cash/PnL rows and epoch receipts; unresolved mandatory reconciliation evidence or an integrity hold prevents counters advancing.

**Repaired state**

| Canonical asset | Broker quantity | Internal lot quantity | Internal holding quantity | Closed quantity | Gross realized PnL |
| --- | ---: | ---: | ---: | ---: | ---: |
| crypto:default:alpaca:SOL/USD | 0 | 0 | 0 | 36.631822251 | -134.157609727268 |
| equity:default:alpaca:V | 0 | 0 | 0 | 13.195 | -31.92020 |
| equity:default:alpaca:AAPL | 0 | 0 | 0 | 50.303 | 1101.93261 |
| equity:default:alpaca:GOOGL | 6.588 | 6.588 | 6.588 | 0 | 0 |
| equity:default:alpaca:SPY | 8.721 | 8.721 | 8.721 | 0 | 0 |

Before repair, SOL/USD had `0.091809079` internal units and V had `13.195`, despite broker quantities of zero. AAPL was already closed but had no canonical cash or PnL rows. GOOGL and SPY quantities were unchanged by repair.

The copy contains 38 fills, 38 settlements, 31 preserved lots, 29 trade attributions, 53 cash rows, 35 PnL rows, five epochs, five activity cursors and 105 immutable broker activity receipts. Only GOOGL and SPY remain open. Every completed settlement satisfies all projection flags and canonical evidence checks. No active order-level integrity hold exists; the session does not require manual financial-integrity re-enablement.

AAPL's signed execution cash, immutable fill economics and closed-lot gross PnL all equal `$1,101.93261`. This is gross price PnL, not a claim of fee-final net PnL. The 13 account-level FEE receipts total `$2.68`; they have durable cash debits and complete original receipts, but no exact fill/order allocation. They are not assigned by date coincidence or inferred fee rates. AAPL and the other affected equities therefore expose `net: null` and unresolved fee status. Recognized zero allocated expenses does not mean zero actual fees.

SOL's epoch is `reconciled_net`, checkpoint version 1. AAPL, V, GOOGL and SPY epochs are `fee_pending`, with explicit unresolved account-fee/order-finality reasons. Future activity can supersede an observed checkpoint; no assertion of perpetual broker fee finality is made.

**Reproducible evidence**

- `forensic_evidence/accounting-correction/repair-report.json`: original before/after repair observations.
- `forensic_evidence/accounting-correction/final-checkpoint.json`: fresh account/position observations and compositional outcome, observed at `2026-09-22T12:38:47.414879+00:00`.
- `forensic_evidence/accounting-correction/invariants.json`: exact per-asset quantities, PnL, canonical counts and financial hashes. Replaying the exact captured evidence changes none of the financial tables or correction receipts and inserts zero rows. Original fill payloads and reconciliation receipts are preserved; original lot IDs remain present.
- `forensic_evidence/accounting-correction/source-review.patch`: review diff against repository HEAD. Shared files already contained uncommitted work at the start of this correction; those changes were preserved. This is a review artifact for the current workspace, not a claim of a clean-branch release.

Repair an offline copy using the production path:

```bash
.venv/bin/python -m tradepulse.settlement.repair \
  --database forensic_evidence/accounting-correction/repaired.db \
  --apply --reconcile \
  --report forensic_evidence/accounting-correction/final-checkpoint.json
```

Verify original evidence preservation, exact quantities and identical-evidence replay:

```bash
.venv/bin/python scripts/verify_accounting_repair.py \
  --original tradepulse.db \
  --repaired forensic_evidence/accounting-correction/repaired.db \
  --report forensic_evidence/accounting-correction/invariants.json
```

For a paper run using the repaired copy, explicitly select it; the default database remains the original:

```bash
TRADEPULSE_DATABASE_URL=sqlite:////home/damien/tradepulse-ai/forensic_evidence/accounting-correction/repaired.db .venv/bin/tradepulse run
```

No trading run was launched as part of this repair, and no broker order was placed, cancelled or replaced.

**Production files changed in this correction**

`tradepulse/settlement/accounting.py`, `tradepulse/settlement/repair.py`, `tradepulse/settlement/engine.py`, `tradepulse/models/settlement.py`, `tradepulse/models/enums.py`, `tradepulse/models/reconciliation.py`, `tradepulse/reconciliation/fee_replay.py`, `tradepulse/reconciliation/epochs.py`, `tradepulse/reconciliation/equity_epochs.py`, `tradepulse/reconciliation/coordinator.py`, `tradepulse/valuation.py`, and `tradepulse/verification/evidence.py`. The reproducible audit is `scripts/verify_accounting_repair.py`.

No cash/PnL tables were removed. Existing table layouts are retained. New reconciliation receipt kinds/outcomes and the explicit evidence-backed attribution-field migration are validated by the canonical models.

**Validation**

The final full-suite result is recorded in `forensic_evidence/accounting-correction/final-suite.log` and `final-suite.xml`. An earlier complete run passed 891 tests with one optional live-credential test skipped and no pytest warnings. The final additional epoch-focused run passed 68 tests. Final counts are appended below after the complete final-source run finishes.

Regression coverage includes failed PnL writes and safe retries, no-op duplicate replay, false checkpoint rejection, legacy migration provenance, exact fractional quantities, canonical identities with colliding display symbols, zero-equity/position-mismatch composition, missing journal evidence blocking prove-edge, new broker receipts superseding verified epochs, and failed new population observations invalidating old eligibility without duplicate supersession receipts.
