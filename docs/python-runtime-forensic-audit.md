# Standalone Python Runtime - Forensic Audit

Reference: Rev.48 (`442567d`), inspected 2026-08-15. This report uses current source caller tracing and read-only production records.

## 1. Proven functionality to preserve

- Canonical asset class/session logic: `assetSessions.ts`, `opportunity.ts`.
- Provider-normalized quotes/candles with explicit failures: `marketDataAdapter.ts`, `alpaca.ts`.
- Deterministic technical, momentum, risk, regime, and governed factor scoring: `quantScore.ts`, `regime.ts`, `modelGovernance.ts`.
- Central risk limits and fail-closed drawdown/data/account checks: `riskEngine.ts`, `execution.ts`.
- Stable TradeIntent/client IDs, broker confirmation, partial fill increments, Fill deduplication, and paper/live separation: `execution.ts`.
- Fill -> SettlementEvent -> one settlement projector -> CashEntry/PositionLot/Holding/Trade/PnL architecture: `processSettlementQueue`, `cashLedger.ts`, `lotAccounting.ts`.
- Broker order/activity and position reconciliation with explicit drift: `runOrderReconciliation`, `syncBrokerPositions`.
- Serialized scan lifecycle/generations and persisted disposition conservation: `runScanCoordinator`, `runAutonomousScanCycle`, `scanState.ts`, `scanConservation.ts`.
- Stops/targets routed through canonical execution: `runStopLossCycle`.
- Session, kill-switch, integrity recovery, audit, and EOD evidence: `sessionState.ts`, `sessionControl.ts`, `generateDailyTradingReport`.

## 2. Base44-specific functionality to replace

All `base44.entities.*`, `base44.functions.invoke`, `Core.InvokeLLM`, `Core.SendEmail`, workflow cron orchestration, browser polling, and browser-started intelligence/execution become Python repositories, workers, provider interfaces, scheduler, notification adapter, and optional authenticated API.

## 3. Duplicate/dead implementations

- Backend `runAutonomousScanCycle` and browser `src/lib/autonomousScan.js` are both live and can lead to `executeTrade`.
- `runStopLossCycle` and browser `StopLossScanner` are both live exit authorities.
- Provider acquisition is duplicated across scan, snapshot, stop-loss, dashboard, watchlist, and RealDataPipeline.
- `COINBASE_SPOT` is unused.
- `autonomousScan.js` is not dead: four modules import it.
- Production currently has active trading and two pending ScanRequests, but no ScanRun after August 14; the local minute workflow is not proof of a functioning coordinator.

## 4. Current authority boundaries

Base44 owns persistence and orchestration. `execution.ts` is the canonical order/fill gateway. `processSettlementQueue` is the sole financial projector. Alpaca is authoritative for live account/order/fill/position facts. Browser components can still create competing AI scans and stop exits, which is a defect to eliminate rather than preserve.

## 5. Proposed Python authority boundaries

Python owns the entire operational and financial runtime: provider cache, one ScanCoordinator, AI/research, strategies, one RiskManager, TradeIntent, paper/live gateways, confirmed Fill ingestion, durable SettlementEvent, one settlement writer, portfolio/cash/lots/PnL, reconciliation, sessions, stops/targets, audit, and recovery. Base44 becomes optional and has no runtime authority.

## 6. Migration risks

Do not operate Python and Base44 autonomous execution concurrently. Preserve stable identifiers and native asset identity. Do not infer fills from submission. Do not split settlement writers. SQLite operations must be repository-contained and transactionally serialize settlement. Live mode requires an explicit second enable flag and credentials. Restart must recover pending orders/settlements without duplicating them.

## 7. Rev.48 behavioral reference files

Primary: `base44/shared/{alpaca,assetSessions,cashLedger,execution,lotAccounting,marketDataAdapter,modelGovernance,opportunity,quantScore,regime,riskEngine,scanConservation,scanState,sessionControl,sessionState}.ts`.

Lifecycle functions: `runAutonomousScanCycle`, `runScanCoordinator`, `executeTrade`, `processSettlementQueue`, `runOrderReconciliation`, `syncBrokerPositions`, `runStopLossCycle`, `generateDailyTradingReport`.

Duplicate/UI reachability references: `src/lib/autonomousScan.js`, `src/lib/marketData.js`, `src/pages/{AutonomousTrader,Dashboard,AIAssistant,Watchlist}.jsx`, `src/components/{ScanRunStatus,StopLossScanner,TradingSessionControl,TradePerformance,StressTestSimulator,AddPositionDialog}.jsx`.
