import { AccountPanel } from './components/AccountPanel'
import { AlertsPanel } from './components/AlertsPanel'
import { CapabilityPanel } from './components/CapabilityPanel'
import { FillsSettlementsPanel } from './components/FillsSettlementsPanel'
import { OpportunitiesPanel } from './components/OpportunitiesPanel'
import { PendingOrdersPanel } from './components/PendingOrdersPanel'
import { PnlPanel } from './components/PnlPanel'
import { PositionsPanel } from './components/PositionsPanel'
import { RiskExposurePanel } from './components/RiskExposurePanel'
import { ScannerActivityPanel } from './components/ScannerActivityPanel'
import { SessionPanel } from './components/SessionPanel'

function App() {
  return (
    <div className="dashboard">
      <header>
        <h1>TradePulse Dashboard</h1>
        <p className="muted">Local operator view -- bound to 127.0.0.1 only. No remote access, no authentication (phase 1).</p>
      </header>
      <div className="grid">
        <SessionPanel />
        <CapabilityPanel />
        <AccountPanel />
        <RiskExposurePanel />
        <PositionsPanel />
        <PnlPanel />
        <ScannerActivityPanel />
        <PendingOrdersPanel />
        <OpportunitiesPanel />
        <FillsSettlementsPanel />
        <AlertsPanel />
      </div>
    </div>
  )
}

export default App
