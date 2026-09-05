import { AccountPanel } from './components/AccountPanel'
import { ActivityFeed } from './components/ActivityFeed'
import { AlertsPanel } from './components/AlertsPanel'
import { CapabilityPanel } from './components/CapabilityPanel'
import { CommandBar } from './components/CommandBar'
import { EquityCurveChart } from './components/EquityCurveChart'
import { FillsSettlementsPanel } from './components/FillsSettlementsPanel'
import { Footer } from './components/Footer'
import { KpiRow } from './components/KpiRow'
import { OpportunitiesPanel } from './components/OpportunitiesPanel'
import { PendingOrdersPanel } from './components/PendingOrdersPanel'
import { PnlPanel } from './components/PnlPanel'
import { PositionsPanel } from './components/PositionsPanel'
import { RiskExposurePanel } from './components/RiskExposurePanel'
import { ScannerActivityPanel } from './components/ScannerActivityPanel'
import { SessionPanel } from './components/SessionPanel'
import { TradeLifecyclePanel } from './components/TradeLifecyclePanel'
import { useTradeLifecycleData } from './useTradeLifecycleData'

function App() {
  // Lifted here (not self-polled by each panel) so ScannerActivityPanel's
  // funnel, TradeLifecyclePanel, ActivityFeed, OpportunitiesPanel,
  // PendingOrdersPanel, FillsSettlementsPanel, and AlertsPanel all consume
  // the SAME four polls instead of each independently re-fetching the same
  // opportunities/trade-intents/fills/settlements endpoints.
  const lifecycle = useTradeLifecycleData()

  return (
    <div className="dashboard">
      <CommandBar />
      <KpiRow />

      <div className="grid">
        <div className="grid-full-width">
          <ScannerActivityPanel filledCountByGeneration={lifecycle.filledCountByGeneration} />
        </div>

        <EquityCurveChart />
        <RiskExposurePanel />

        <div className="grid-full-width">
          <PositionsPanel />
        </div>

        <OpportunitiesPanel data={lifecycle.opportunities.data} error={lifecycle.opportunities.error} loading={lifecycle.opportunities.loading} />
        <PendingOrdersPanel data={lifecycle.tradeIntents.data} error={lifecycle.tradeIntents.error} loading={lifecycle.tradeIntents.loading} />

        <div className="grid-full-width">
          <TradeLifecyclePanel entries={lifecycle.entries} loading={lifecycle.opportunities.loading || lifecycle.tradeIntents.loading} error={lifecycle.tradeIntents.error} />
        </div>

        <FillsSettlementsPanel fills={lifecycle.fills} settlements={lifecycle.settlements} />

        <AlertsPanel settlements={lifecycle.settlements} tradeIntents={lifecycle.tradeIntents} />
        <ActivityFeed
          title="Activity / Event Stream"
          opportunities={lifecycle.opportunities.data}
          tradeIntents={lifecycle.tradeIntents.data}
          fills={lifecycle.fills.data}
          settlements={lifecycle.settlements.data}
          limit={50}
        />

        <SessionPanel />
        <AccountPanel />
        <PnlPanel />
        <CapabilityPanel />
      </div>
      <Footer />
    </div>
  )
}

export default App
