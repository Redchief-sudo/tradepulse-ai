"""Read-only evidence reproduction; recording quotes are rounded, not raw receipts."""
import argparse
import hashlib
import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path


def reproduce(path: Path) -> dict:
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        rows = {table: [json.loads(r[0]) for r in connection.execute(f'SELECT payload FROM {table}')]
                for table in ('holdings', 'fills', 'position_lots', 'settlements', 'equity_snapshots', 'reconciliation_records')}
    finally:
        connection.close()
    holdings = {r['asset']['symbol']: r for r in rows['holdings']}
    sol = [f for f in rows['fills'] if f['asset']['native_asset_id'] == 'alpaca:SOL/USD']
    lots = [r for r in rows['position_lots'] if r['asset']['native_asset_id'] == 'alpaca:SOL/USD']
    quantity = sum((Decimal(f['quantity']) for f in sol), Decimal(0))
    # Visible at 00:25. These rounded quotes cannot reproduce a synchronized account response.
    marks = {'AAPL': '336.36', 'GOOGL': '346.80', 'SOL/USD': '101.40', 'SPY': '762.12'}
    quantities = {'AAPL': '50.303', 'GOOGL': '6.588', 'SOL/USD': '36.631822251', 'SPY': '8.721'}
    marked = {s: Decimal(quantities[s])*Decimal(mark) for s, mark in marks.items()}
    result = {
        'database_sha256': before,
        'latest_snapshot': max(rows['equity_snapshots'], key=lambda r: r['as_of']),
        'local_cost_excluding_reservations': sum((Decimal(r['quantity'])*Decimal(r['average_price']) for r in holdings.values()), Decimal(0)),
        'approximate_marked_values_from_recording': marked,
        'approximate_marked_sum': sum(marked.values(), Decimal(0)),
        'initial_stop': holdings['AAPL']['stop_loss'], 'active_stop': holdings['AAPL']['current_stop'],
        'sol_unique_fill_ids': len({f['broker_fill_id'] for f in sol}), 'sol_fill_quantity': quantity,
        'sol_remaining_lot_quantity': sum((Decimal(r['remaining_quantity']) for r in lots), Decimal(0)),
        'sol_holding_quantity': holdings['SOL/USD']['quantity'], 'sol_recorded_broker_quantity': quantities['SOL/USD'],
        'sol_difference': quantity-Decimal(quantities['SOL/USD']), 'sol_fill_times': [f['filled_at'] for f in sol],
        'sol_settlement_statuses': [r['status'] for r in rows['settlements'] if r['fill_id'] in {f['fill_id'] for f in sol}],
        'reconciliation_records': len(rows['reconciliation_records']),
        'limitation': 'No timestamped broker fee receipts or position history supplied; first divergence unproven. No balance mutation.',
    }
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger(__name__).info(json.dumps(reproduce(args.database), default=str, sort_keys=True))
