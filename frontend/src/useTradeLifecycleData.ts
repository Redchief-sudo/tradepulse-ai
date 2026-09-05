import { usePolling } from './usePolling'
import { api } from './api'
import type { Fill, Opportunity, SettlementEvent, TradeIntent } from './types'

/** One trade's full forensic chain, joined ONLY from IDs the backend
 * already returns verbatim (opportunity_id / correlation_id / trade_intent_id
 * / fill_id) -- no new financial computation, no new authority. `opportunity`
 * is legitimately null for a monitor-originated exit (a stop/target close
 * has no originating scanner Opportunity) -- that is a normal case to
 * render as "no opportunity," never an error or a dropped row. */
export interface LifecycleEntry {
  opportunity: Opportunity | null
  tradeIntent: TradeIntent
  fills: Fill[]
  settlements: SettlementEvent[]
}

interface TradeLifecycleData {
  opportunities: { data: Opportunity[] | null; error: string | null; loading: boolean }
  tradeIntents: { data: TradeIntent[] | null; error: string | null; loading: boolean }
  fills: { data: Fill[] | null; error: string | null; loading: boolean }
  settlements: { data: SettlementEvent[] | null; error: string | null; loading: boolean }
  entries: LifecycleEntry[]
  /** scan_generation -> count of that generation's trade intents with at
   * least one fill -- the Scanner Funnel's "Filled" stage. Only populated
   * once opportunities/trade-intents/fills have all loaded; never
   * fabricated as 0 while a dependency is still loading or errored. */
  filledCountByGeneration: Record<string, number> | null
}

/** Lifts the four trade-record polls (opportunities/trade-intents/fills/
 * settlements) to ONE shared location so multiple components (the existing
 * panels plus the new Trade Lifecycle view and Scanner Funnel) consume the
 * same poll results instead of each independently re-fetching the same
 * endpoints -- see the plan's explicit performance requirement. Call this
 * once in App.tsx and pass the pieces down as props. */
export function useTradeLifecycleData(): TradeLifecycleData {
  const opportunities = usePolling(() => api.getOpportunities(100), 30000)
  const tradeIntents = usePolling(() => api.getTradeIntents(undefined, 100), 10000)
  const fills = usePolling(() => api.getFills(100), 30000)
  const settlements = usePolling(() => api.getSettlements(100), 30000)

  const entries: LifecycleEntry[] = []
  let filledCountByGeneration: Record<string, number> | null = null

  if (tradeIntents.data && opportunities.data && fills.data && settlements.data) {
    const opportunityById = new Map(opportunities.data.map((o) => [o.opportunity_id, o]))
    const fillsByIntentId = new Map<string, Fill[]>()
    for (const fill of fills.data) {
      const list = fillsByIntentId.get(fill.trade_intent_id) ?? []
      list.push(fill)
      fillsByIntentId.set(fill.trade_intent_id, list)
    }
    const settlementsByFillId = new Map<string, SettlementEvent[]>()
    for (const settlement of settlements.data) {
      const list = settlementsByFillId.get(settlement.fill_id) ?? []
      list.push(settlement)
      settlementsByFillId.set(settlement.fill_id, list)
    }

    const counts: Record<string, number> = {}
    for (const intent of tradeIntents.data) {
      const opportunity = intent.correlation_id ? (opportunityById.get(intent.correlation_id) ?? null) : null
      const intentFills = fillsByIntentId.get(intent.trade_intent_id) ?? []
      const intentSettlements = intentFills.flatMap((f) => settlementsByFillId.get(f.fill_id) ?? [])
      entries.push({ opportunity, tradeIntent: intent, fills: intentFills, settlements: intentSettlements })

      if (opportunity && intentFills.length > 0) {
        counts[opportunity.scan_generation] = (counts[opportunity.scan_generation] ?? 0) + 1
      }
    }
    filledCountByGeneration = counts
  }

  return { opportunities, tradeIntents, fills, settlements, entries, filledCountByGeneration }
}
