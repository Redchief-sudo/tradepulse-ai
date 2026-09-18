# Forensic correction: valuation, active stop, and SOL reconciliation

Baseline: Rev.100 (`e0d7f6359d86dc21d8dc10b49ee6a9a89127d7cb`). All financial tests use temporary fixtures and mocked broker requests. The supplied evidence and existing paper account are not repaired or migrated.

## Evidence and reproduction

Read-only evidence in `forensic_evidence/2026-09-17/`:

- `tradepulse-2026-09-17.db`: SHA-256 `7cc34306e47bac47c5be334142f69b4c6a6f7b9bd97e2c956f117d7994c67444`.
- `dashboard-2026-09-17-164149.mkv`: SHA-256 `aabebb02dbf3604f58c8348002a47f4ec820437b09fb790018d596f816e8326a`.

Neither file belongs in Git or a release archive. Database inspection uses SQLite `mode=ro&immutable=1`. Recording frames were extracted to `/tmp`, not into the evidence directory. Both hashes were checked again after inspection.

Reproduce the database calculations without initializing or migrating it:

```bash
.venv/bin/python scripts/reproduce_forensic_snapshot.py forensic_evidence/2026-09-17/tradepulse-2026-09-17.db
```

The script emits structured JSON and checks the database hash before and after reading. Its recording-derived marked values are explicitly approximate: the video shows rounded prices, not synchronized raw broker position/account responses.

## 1. Valuation: proven and corrected prospectively

Production path: `scanner/coordinator.py::run_scan_cycle` called `risk/engine.py::build_portfolio_snapshot` without marks, then persisted that result to `equity_snapshots`. The risk builder uses local acquisition prices when marks are absent, and also adds pending-order reservations and unprojected settlements. These are useful capacity inputs, but are not held market value. The dashboard supplied marks to that same builder, so it also included stale local holdings and reservations.

The supplied database reproduces:

- Local holding acquisition cost: `33673.29127431199`.
- Accepted V order reservation: `13.195 × 355.94 = 4696.6283`.
- Persisted “holdings_value”: `33673.29127431199 + 4696.6283 = 38369.91957431199`.
- “Other”: local V cost `4973.81915` plus reservation `4696.6283` = `9670.44745`.
- AAPL local cost: `15951.28742`; GOOGL: `2278.7892`; SPY: `6606.24471`; SOL: `3863.15079431199`.

At recording 00:22 the risk panel displays `$39,246.31`; this is also contaminated by the risk-capacity calculation and is not a target authoritative total. At 00:25 the broker position table contains AAPL, GOOGL, SOL/USD and SPY, with displayed market values totaling approximately `$29,565.60`. The database and recording are not a synchronized raw account snapshot; no historical corrected broker total or residual balancing amount is fabricated.

New shared `valuation.py::marked_snapshot` uses signed broker `market_value` and broker `cost_basis`, grouped by the canonical holding's sector. Missing cost basis is explicitly unavailable and fails reconciliation. Pending reservations and stale local-only holdings do not enter the reported market value. Scanner persistence and `/api/risk-exposure` call this same function. The positions table displays the broker market-value field directly; unrealized percentage is computed with Decimal from broker cost basis in the backend.

`valuation.py::record_valuation` persists matched/drift evidence keyed to the snapshot and logs failures. Named account components are retained, including long/short market value and supplied fee/transfer/memo fields. Cash plus signed long/short value, less an explicitly supplied memopost amount, is compared with broker equity. Other fee/transfer fields are retained for diagnosis, not arbitrarily subtracted twice. Position-value totals are also checked against account long/short totals. No undocumented monetary epsilon is introduced: comparisons are exact Decimal. Separate broker calls or broker rounding can therefore produce a visible discrepancy; the patch does not hide it or change the broker equity number.

Prospective JSON payload version 2 adds `holdings_cost_basis`, `sector_cost_basis`, `broker_equity_components`, `equity_reconciliation_status`, `equity_reconciliation_difference`, and `valuation_errors`. Existing rows remain version 1 when decoded; no historical row is rewritten. The SQLite table schema is unchanged. `ReconciliationType` adds `equity`. Internal risk sizing/reservation calculations and all risk thresholds remain unchanged.

Exact regression example: marked value `20.246913578`, cost basis `10.617283945`; moving the marked value to `22` leaves cost basis unchanged. A discrepancy of `0.000000001` fails reconciliation rather than disappearing into a tolerance.

## 2. Active stop: proven and corrected

`monitor/coordinator.py::_breached` already enforces `current_stop` when present. `/api/positions` previously serialized only `stop_loss`, and `PositionsPanel` displayed it.

The evidence contains original AAPL stop `305.96` and current stop `319.4384280853317375`. The recording shows `$305.96`. The API now exposes `initial_stop` and `active_stop`; the primary Stop column displays `$319.44`, preserving the full decimal string internally. Only an absent current stop selects the original. A malformed current stop yields explicit HTTP 503 `POSITION_DATA_INVALID`, not a fallback. No stored stop or monitor behavior changes.

## 3. SOL: mismatch proven; origin and automatic repair remain unresolved

The complete local path was inspected: Alpaca activity normalization; `execution/fill_attribution.py::attribute_order_fills`; settlement lot and holding projection; canonical reconciliation; and dashboard broker-position serialization.

Four unique SOL buy fills are present, with four completed settlements and matching open lots:

| UTC fill time on 2026-09-03 | Quantity |
|---|---:|
| 19:35:11.843845 | 13.9083 |
| 19:35:11.843861 | 2.75634854 |
| 21:26:06.116982 | 14.0172 |
| 21:26:06.116993 | 6.04178279 |

Their sum, lot balance and local holding are all `36.72363133`. The recording shows broker-backed `36.631822251`; the exact difference is `0.091809079 SOL`. This cannot be price movement. There are zero reconciliation records in the supplied database. Local fills contain zero modeled fees; the ingestion path requests FILL activities, not asset-fee receipts. That proves a limitation of the local evidence, not which specific broker fee event caused this difference.

Timestamped CFEE/other non-trade activity receipts and historical broker position balances are absent from the supplied evidence. Earlier conversational claims about retrieved fee receipts could not be independently revalidated from currently available local artifacts. No first-divergence timestamp, fee-to-fill allocation, or fee debit is asserted as proven here. No speculative balance, lot, historical fill or settlement mutation was implemented.

The supported correction is detection and evidence integrity:

- `_reconcile_positions` retains exact Decimal comparisons and existing financial-integrity latching for accounting drift, leaving mismatched lots and holdings untouched.
- Persisted position subjects now use canonical asset identities instead of bare display symbols.
- Fully closed lots also receive explicit broker-zero comparisons, rather than silently disappearing from reconciliation evidence.
- `verification/evidence.py::assess` requires a canonical position receipt at or after the asset's latest fill. Equity-only, bare-symbol, stale or failed position evidence cannot validate it. Unreconciled assets contribute no eligible round trips or net-result population, and cannot pass the gate. Thresholds are unchanged.
- Repeated mismatch reconciliation does not alter holdings/lots or apply repeated quantity debits; each observation remains an audit record.

A real historical repair remains blocked pending authoritative event evidence and an audited accounting allocation. No automatic asset-fee projection is claimed.

## Changed functions and boundaries

- `tradepulse/broker/types.py`: `AlpacaAccount`, `AlpacaPosition` retain components/cost basis.
- `tradepulse/broker/alpaca_client.py`: `get_account`, `get_positions` decode those fields; required equity/cash/market-value inputs no longer silently become zero.
- `tradepulse/models/portfolio_snapshot.py`: `PortfolioSnapshot` validates prospective reporting metadata.
- `tradepulse/models/reconciliation.py`: `ReconciliationType` accepts equity evidence.
- `tradepulse/persistence/hydration.py`: `decode_equity_snapshot` preserves historical semantics and decodes version 2.
- `tradepulse/valuation.py`: `marked_snapshot`, `record_valuation`.
- `tradepulse/scanner/coordinator.py`: `run_scan_cycle` persists broker-valued snapshots with reconciliation evidence; valuation outages return a failed scan rather than killing the worker.
- `tradepulse/web/app.py`: `get_positions`, `get_risk_exposure`.
- `tradepulse/reconciliation/coordinator.py`: `_reconcile_positions`.
- `tradepulse/verification/evidence.py`: `assess`, only the position-receipt/eligible-population correction within the earlier uncommitted verification feature.
- `frontend/src/components/PositionsPanel.tsx`: active stop, broker market value, backend Decimal percentage.
- `frontend/src/components/RiskExposurePanel.tsx`: visible valuation failure.
- `frontend/src/types.ts`: corresponding response fields.
- `scripts/reproduce_forensic_snapshot.py`: read-only evidence reproduction.

## Regression purposes and validation

`python_tests/test_forensic_corrections.py` adds 14 parameterized cases: marked value/cost/sector precision and persistence; changing marks without changing acquisition cost; four missing/discrepant valuation cases with idempotent records; signed short/memopost components; matching and mismatched SOL fractional quantities with unchanged holdings/lots; duplicate canonical broker identities; cross-class bare-symbol collisions; unresolved-position exclusion; and three invalid position-receipt cases.

`python_tests/test_web_app.py` adds exact active/original stop and malformed-current-stop tests. `frontend/src/components/PositionsPanel.test.tsx` adds presentation-only rounding and direct broker-market-value verification. Existing scanner, reconciliation and restart-survival fixtures/assertions are adjusted for the extra broker position read, canonical subjects, explicit closed-position receipts, and persisted equity reconciliation. `RiskExposurePanel.test.tsx` uses the corrected position response shape. The existing paper-verification passing fixture now supplies a canonical subject.

Final combined-tree validation:

- Focused valuation, dashboard, reconciliation and verification suite: 101 passed. The subsequent full run also includes the final explicit eligible-population exclusion assertions.
- Complete Python suite: 832 passed, 1 skipped (136.12 seconds). This includes accounting, settlement, fill attribution, risk/snapshot, position monitor, restart survival, dashboard and prove-edge tests.
- Frontend: 28 tests passed across 8 files.
- `npm run build`: TypeScript and Vite passed.
- `python -m compileall -q tradepulse python_tests scripts`: passed.
- Ruff on new correction modules/tests/reproduction script: passed. Comparing touched existing files to Rev.100 found no newly introduced diagnostics; 97 existing diagnostics remain (104 in that baseline selection).
- Frontend lint exited successfully with five warnings in unchanged files (`EquityCurveChart`, `usePolling`, `LaneCard`, `CommandBar`). Existing React act warnings also remain in CommandBar tests.
- `git diff --check`: passed. Read-only reproduction and both evidence SHA-256 checks passed.

No live runtime/order test or prove-edge performance claim is made.

## Scope audit

The initial dirty tree also contained earlier authorized Rev.101 work: `tradepulse/cli.py`, `tradepulse/verification/`, `python_tests/test_paper_verification.py`, and `docs/paper-verification-integrity.md`. That prior feature was reported explicitly rather than silently removed. This correction changes only the position-evidence portion of its assessor and the matching test fixture; it does not introduce another verification policy or change prove-edge thresholds. The user authorized two separate releases: Rev.101 contains that verification feature; Rev.102 contains these forensic corrections and the necessary position-evidence fix.

No strategy, AI prompt, confidence threshold, sizing policy, risk limit, scan cadence, asset universe, market-hours rule, order-submission path, persistent learning, or multi-broker design was changed for this correction. No broker state/orders, current operating database, historical fills/settlements, or forensic evidence files were changed.
