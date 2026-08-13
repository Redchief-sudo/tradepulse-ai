import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { fetchQuotes } from '../../shared/marketDataAdapter.ts';
import { usMarketSession, isUsMarketOpen } from '../../shared/marketHours.ts';
import { snapshotConservation } from '../../shared/operationalTruth.ts';

// Scheduled price-snapshot capture. Records a point-in-time price for every
// symbol that has an open AI buy decision (plus current holdings + SPY benchmark),
// so the outcome-labeling engine can compute forward returns and excursions.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const key = secrets.get('FINNHUB_API_KEY');
    if (!key) return Response.json({ error: 'FINNHUB_API_KEY not set' }, { status: 500 });

    const sr = base44.asServiceRole;
    const decisions = await sr.entities.AITradeDecision.filter({ user_id: user.id }, '-created_date', 200);
    const openSyms = new Set();
    decisions.filter((d) => d.action === 'buy' && d.status === 'executed').forEach((d) => {
      const sym = String(d.symbol).toUpperCase();
      if (d.outcome_status !== 'realized') openSyms.add(sym);
    });
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const assetClassBySym = {};
    holdings.forEach((h) => { assetClassBySym[String(h.symbol).toUpperCase()] = h.asset_class || 'stocks'; });
    decisions.forEach((d) => { if (!assetClassBySym[String(d.symbol).toUpperCase()]) assetClassBySym[String(d.symbol).toUpperCase()] = d.asset_class || 'stocks'; });
    assetClassBySym['SPY'] = 'stocks';
    holdings.forEach((h) => openSyms.add(String(h.symbol).toUpperCase()));
    openSyms.add('SPY');

    const symbols = [...openSyms].slice(0, 40);
    const items = symbols.map((s) => ({ symbol: s, asset_class: assetClassBySym[s] || 'stocks' }));
    const now = new Date().toISOString();
    const session = usMarketSession();
    const marketOpen = isUsMarketOpen();
    const quotes = await fetchQuotes(items, key);
    let captured = 0;
    let providerFailed = 0;
    let persistenceFailed = 0;
    const failures = [];
    for (const quote of quotes.filter((quote) => quote.error || quote.price <= 0)) {
      providerFailed++;
      failures.push({ symbol: quote.symbol, failure_kind: 'provider_failed', provider: quote.provider, error_code: quote.error_code, http_status: quote.http_status, error: quote.error || 'Provider returned no positive price' });
    }
    await Promise.all(quotes.filter((q) => !q.error && q.price > 0).map(async (q) => {
      try {
        // Record the provider's authoritative observation time, not the database
        // insertion time. An after-hours close fetched at 9pm must not appear fresh.
        if (!q.quote_timestamp) throw new Error('Provider timestamp missing');
        const providerTs = new Date(q.quote_timestamp * 1000).toISOString();
        await sr.entities.PriceSnapshot.create({
          user_id: user.id,
          symbol: q.symbol, price: q.price, timestamp: now,
          provider_timestamp: providerTs,
          market_session: session,
          is_market_open: marketOpen,
          source: q.asset_class === 'crypto' ? 'coinbase' : 'finnhub',
        });
        captured++;
      } catch (e) {
        // Persist the failure instead of silently skipping — operations needs
        // to know which symbols failed and why. (Fixes Rev.10 defect #19.)
        persistenceFailed++;
        failures.push({ symbol: q.symbol, failure_kind: 'persistence_failed', error: e.message });
      }
    }));

    // Record snapshot capture failures as audit events so they're visible.
    if (failures.length) {
      try {
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: 'snapshot_capture_failed',
          severity: 'warning',
          entity_type: 'PriceSnapshot',
          message: `${providerFailed + persistenceFailed}/${symbols.length} snapshots failed`,
          details: JSON.stringify(failures.slice(0, 10)),
        });
      } catch (e) { /* audit itself failed — nothing more we can do */ }
    }

    const summary = snapshotConservation(symbols.length, captured, providerFailed, persistenceFailed);
    return Response.json({ ...summary, failures }, { status: summary.ok ? 200 : 502 });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
