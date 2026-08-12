import { parseClosureFills } from './lotAccounting.ts';

export function lotDirection(lot: any) {
  return lot?.position_side === 'short' ? 'short' : 'long';
}

export function signedLotQuantity(lot: any) {
  const quantity = Number(lot?.quantity_remaining) || 0;
  return lotDirection(lot) === 'short' ? -quantity : quantity;
}

export function planSignedLotFill(lots: any[], event: any) {
  const openingDirection = event.side === 'buy' ? 'long' : 'short';
  const closingDirection = openingDirection === 'long' ? 'short' : 'long';
  let alreadyClosed = 0;
  let realizedPnl = 0;
  for (const lot of lots) {
    for (const allocation of parseClosureFills(lot.closure_fill_ids)) {
      if (allocation.fill_id !== event.event_id) continue;
      const quantity = Number(allocation.qty) || 0;
      alreadyClosed += quantity;
      realizedPnl += lotDirection(lot) === 'long'
        ? (event.price - lot.acquisition_price) * quantity
        : (lot.acquisition_price - event.price) * quantity;
    }
  }
  const alreadyOpened = lots
    .filter((lot) => lot.originating_fill_id === event.event_id && lotDirection(lot) === openingDirection)
    .reduce((sum, lot) => sum + (Number(lot.quantity_opened) || 0), 0);
  if (alreadyClosed + alreadyOpened > event.quantity + 0.0001) {
    throw new Error(`INTEGRITY_VIOLATION: FILL_EVENT_OVERALLOCATED ${alreadyClosed + alreadyOpened} > ${event.quantity}`);
  }

  let remaining = event.quantity - alreadyClosed - alreadyOpened;
  const closures: any[] = [];
  const oppositeLots = lots
    .filter((lot) => lotDirection(lot) === closingDirection && ['open', 'partially_closed'].includes(lot.status))
    .sort((left, right) => new Date(left.acquisition_timestamp).getTime() - new Date(right.acquisition_timestamp).getTime());
  for (const lot of oppositeLots) {
    if (remaining <= 0.0001) break;
    const quantity = Math.min(Number(lot.quantity_remaining) || 0, remaining);
    if (quantity <= 0) continue;
    const pnl = closingDirection === 'long'
      ? (event.price - lot.acquisition_price) * quantity
      : (lot.acquisition_price - event.price) * quantity;
    closures.push({ lot, quantity, pnl });
    realizedPnl += pnl;
    remaining -= quantity;
  }

  return {
    openingDirection,
    closures,
    openingQuantity: remaining > 0.0001 ? remaining : 0,
    realizedPnl,
  };
}
