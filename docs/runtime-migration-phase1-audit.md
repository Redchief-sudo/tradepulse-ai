# External Trading Runtime Migration - Phase 1 Audit

Audit date: 2026-08-15. Source: Rev.48 (`442567d`). Findings use the current checkout and read-only production evidence; local workflow files are not treated as deployment proof.

## A. Current architecture

Scheduled path: `Intraday AI Scan` -> `runScanCoordinator` -> `ScanRequest`/scheduled slot -> `runAutonomousScanCycle` -> Base44 `Core.InvokeLLM` passes -> provider data -> risk -> `settleTrade` -> Alpaca -> `Fill`/`SettlementEvent` -> `processSettlementQueue`.

This path ran previously but is not currently draining commands: production has `trading_active=true`, two pending requests from August 15-16, and no `ScanRun` after August 14.

Browser path: `AutonomousTrader.jsx` calls the seven LLM operations in `src/lib/autonomousScan.js`, then `executeTrade`. `CausalContagionGraph`, `SelfLearningMemory`, and `ExecutionBreakdown` also import that module. This is a second live intelligence implementation. `useStartTrader` separately creates durable `ScanRequest` records.

Market data is independently acquired by Dashboard, RealDataPipeline, Watchlist, StopLossScanner, snapshot/stop-loss workflows, execution, and autonomous scans. There is no shared browser runtime snapshot cache.

Execution is centralized today: order surfaces reach `executeTrade`/`settleTrade`; `execution.ts` owns idempotent intent, broker, fill, and settlement-event lifecycle; `processSettlementQueue` alone projects lots, holdings, trades, and financial state.

## B. Integration-credit hot paths

- `runAutonomousScanCycle`: up to five `Core.InvokeLLM` calls per cycle.
- `src/lib/autonomousScan.js`: seven browser-reachable LLM operations.
- `modelGovernance.ts`: weekly LLM hypothesis.
- Interactive LLM: AIAssistant, Watchlist, AddPositionDialog, StressTestSimulator, TradePerformance.
- Built-in email: scan, stop-loss, reconciliation, health, daily summary, trade alert, model promotion.

Configured high-frequency workflow maximums: coordinator 1,440/day; snapshots, health, order reconciliation, settlement recovery, and stop-loss 288/day each; up to 2,880 Base44 workflow invocations/day before nested calls and entity/provider operations. A 15-minute active scan cadence permits 96 LLM scan cycles/day.

## C. Proven duplicate/dead paths

- Duplicate/live scanner: backend autonomous scanner and browser `autonomousScan.js` can both reach execution.
- Duplicate/live exit authority: scheduled `runStopLossCycle` and browser `StopLossScanner` can submit exits.
- Duplicate/live market acquisition across UI and workflows.
- `COINBASE_SPOT` is unused.
- `autonomousScan.js` is not dead and cannot be deleted until four importers are migrated.
- The local Intraday workflow is not currently reflected by production behavior; pending commands are not consumed.

## D. Proposed authority boundaries

Base44 remains authoritative for authentication, configuration, signed command intake/audit, UI, current canonical execution, confirmed-fill ingestion, `SettlementEvent`, single-writer settlement, history, and reports.

The external runtime becomes solely authoritative for scan scheduling/generation, AI provider calls, market-data coordination/cache, opportunities, strategy orchestration, continuous stops/targets, broker/order monitoring, and operational health/recovery.

Initially the runtime submits idempotent commands/facts into the existing execution/accounting boundary. Execution and settlement are not split between two writers.

## E. Files requiring modification

Phase 2 adds runtime contracts/client/gateway/cache and tests. Later cutover touches:

`runAutonomousScanCycle`, `runScanCoordinator`, `marketData`, `getMultiAssetQuotes`, `runStopLossCycle`, `modelGovernance.ts`, `src/lib/autonomousScan.js`, `src/lib/marketData.js`, Dashboard, AIAssistant, Watchlist, AutonomousTrader, AddPositionDialog, StressTestSimulator, TradePerformance, ScanRunStatus, StopLossScanner, TradingSessionControl, CleanRunStatus, `useStartTrader`, and classified workflow files.

## F. Files remaining unchanged initially

`base44/shared/execution.ts`, `executeTrade`, `processSettlementQueue`, `lotAccounting.ts`, `cashLedger.ts`, and the Fill, TradeIntent, SettlementEvent, PositionLot, CashEntry, Holding, Trade, and PnlRecord schemas. Changes require a separate end-to-end execution/accounting migration.

## G. Migration sequence

1. Versioned contracts and authenticated server gateway; no authority change.
2. Central browser runtime snapshot cache.
3. Runtime market-data cache; eliminate overlapping provider calls.
4. External AI provider/runtime boundary.
5. Atomic scheduler cutover; disable Base44 and browser scanners together.
6. Atomic stop/take-profit cutover.
7. Broker monitoring/reconciliation discovery with Base44 settlement single-writer.
8. Remove proven dead paths and reduce administrative workflows.

## H. Risks

Dual schedulers or exit monitors can duplicate orders. Runtime retries require stable request/trade IDs. Symbol identity must remain canonical. Runtime fills must pass Fill deduplication and settlement conservation. Runtime failure must not reactivate old autonomous fallbacks. A Base44 timeout must not be mistaken for command rejection after durable runtime acceptance.

## Entity authority map

| Entity | Classification |
|---|---|
| AITradeDecision | AUDIT_RECORD |
| AuditEvent | AUDIT_RECORD |
| BrokerCredential | CONFIGURATION_AUTHORITY |
| CashEntry | ACCOUNTING_AUTHORITY |
| Fill | ACCOUNTING_AUTHORITY |
| Holding | ACCOUNTING_AUTHORITY settled projection; broker is live display truth |
| MarketDataCredential | CONFIGURATION_AUTHORITY |
| Opportunity | RUNTIME_MIRROR |
| PnlRecord | ACCOUNTING_AUTHORITY |
| PositionLot | ACCOUNTING_AUTHORITY |
| PriceSnapshot | RUNTIME_MIRROR |
| ReconciliationEvent | AUDIT_RECORD |
| ScanLock | RUNTIME_MIRROR; deprecated only after runtime lease proof |
| ScanRequest | CONFIGURATION_AUTHORITY command/outbox |
| ScanRun | AUDIT_RECORD |
| SettlementEvent | ACCOUNTING_AUTHORITY |
| StrategyModel | CONFIGURATION_AUTHORITY |
| Trade | ACCOUNTING_AUTHORITY |
| TradeIntent | ACCOUNTING_AUTHORITY |
| TradingSession | AUDIT_RECORD |
| User | CONFIGURATION_AUTHORITY |
| WatchlistItem | UI_ONLY |

## Workflow classification

| Workflow | Classification and evidence |
|---|---|
| Capture Price Snapshots | MOVE_TO_RUNTIME; latest snapshot August 12, not proven current |
| Daily Summary | KEEP_IN_BASE44 / EVENT_DRIVEN |
| Daily Trading Report | KEEP_IN_BASE44 |
| Health Check | REDUCE_FREQUENCY / EVENT_DRIVEN |
| Intraday AI Scan | MOVE_TO_RUNTIME; previously active, currently not consuming commands |
| Market Open Alert | KEEP_IN_BASE44 / EVENT_DRIVEN |
| Model Governance | MOVE_TO_RUNTIME; contains production InvokeLLM |
| Order Reconciliation | MOVE_TO_RUNTIME; proven active about every five minutes August 16 |
| Outcome Labeling | MOVE_TO_RUNTIME / REDUCE_FREQUENCY |
| Position Reconciliation | REMOVE_AS_DUPLICATE after cutover; order reconciliation invokes it |
| Settlement Processor | EVENT_DRIVEN plus low-frequency recovery; preserve single writer |
| Stop-Loss Monitor | MOVE_TO_RUNTIME atomically with browser exit path |

## Frontend polling inventory

- Dashboard: 45-second broker/entity refresh, 60-second quotes, visibility refresh, Holding subscription.
- ScanRunStatus: four Base44 reads every five seconds plus visibility refresh.
- CleanRunStatus: multiple entity reads every 30 seconds.
- Other quote/AI operations are user-triggered but duplicate provider boundaries.

Target: one authenticated consolidated runtime snapshot cache with bounded polling/subscription. Components never launch autonomous work.
