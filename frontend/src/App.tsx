import { AccountPanel } from './components/AccountPanel'
import { AlertsPanel } from './components/AlertsPanel'
import { CapabilityPanel } from './components/CapabilityPanel'
import { FillsSettlementsPanel } from './components/FillsSettlementsPanel'
import { Footer } from './components/Footer'
import { Header } from './components/Header'
import { KpiRow } from './components/KpiRow'
import { OpportunitiesPanel } from './components/OpportunitiesPanel'
import { Panel } from './components/Panel'
import { PendingOrdersPanel } from './components/PendingOrdersPanel'
import { PnlPanel } from './components/PnlPanel'
import { PositionsPanel } from './components/PositionsPanel'
import { RiskExposurePanel } from './components/RiskExposurePanel'
import { ScannerActivityPanel } from './components/ScannerActivityPanel'
import { SessionPanel } from './components/SessionPanel'

function EquityCurvePlaceholder() {
  return (
    <Panel title="Equity Curve">
      <div className="placeholder-card">
        Equity history — not yet available. No backend route currently exposes historical equity snapshots; this is a
        natural next-phase addition, not a dropped requirement.
      </div>
    </Panel>
  )
}

function App() {
  return (
    <div className="dashboard">
      <Header />
      <KpiRow />
      <div className="grid">
        <ScannerActivityPanel />
        <RiskExposurePanel />
        <PositionsPanel />
        <SessionPanel />
        <AccountPanel />
        <PnlPanel />
        <EquityCurvePlaceholder />
        <OpportunitiesPanel />
        <PendingOrdersPanel />
        <FillsSettlementsPanel />
        <AlertsPanel />
        <CapabilityPanel />
      </div>
      <Footer />
    </div>
  )
}

export default App
