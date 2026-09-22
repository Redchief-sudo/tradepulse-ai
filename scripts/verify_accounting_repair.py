"""Verify a repaired database copy and replay its exact captured fee population.

No broker calls, fabricated executions, or edits to immutable source records.
"""
import argparse
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from tradepulse.models import asset_identity_key
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.reconciliation.fee_replay import replay_asset_fees
from tradepulse.settlement.accounting import accounting_issues, replay_accounting


TABLES = ('fills', 'settlements', 'position_lots', 'holdings', 'trade_attributions',
          'cash_ledger', 'pnl_records', 'accounting_epochs', 'broker_activity_inbox', 'broker_activity_cursors')


def read(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return {table: {rid: json.loads(payload) for rid, payload in connection.execute(
            f'SELECT record_id,payload FROM {table} ORDER BY record_id')}
            for table in (*TABLES, 'reconciliation_records', 'trading_sessions')}
    finally:
        connection.close()


def digest(value):
    return hashlib.sha256(encode_payload(value).encode()).hexdigest()


async def verify(original, repaired, report):
    source, before = await asyncio.gather(asyncio.to_thread(read, original), asyncio.to_thread(read, repaired))
    repositories = PersistenceRepositories.create(AsyncSQLiteDatabase('sqlite:///' + str(repaired.resolve())))
    assert not await accounting_issues(repositories)
    assert all(before['fills'].get(k) == v for k, v in source['fills'].items())
    assert all(before['reconciliation_records'].get(k) == v for k, v in source['reconciliation_records'].items())
    assert source['position_lots'].keys() <= before['position_lots'].keys()
    inserted = await replay_accounting(repositories)
    assert inserted == 0
    for epoch in before['accounting_epochs'].values():
        if not epoch['canonical_asset_key'].startswith('crypto:'):
            continue
        proof = before['reconciliation_records'][epoch['population_proof_id']]['actual']
        asset = next(hydrate('fills', f).asset for f in before['fills'].values()
                     if asset_identity_key(hydrate('fills', f).asset) == epoch['canonical_asset_key'])
        by_id = {raw['id']: raw for raw in proof['activities']}
        raw = [by_id[i] for i in epoch['pagination_proof']['activity_ids']]
        changed = await replay_asset_fees(repositories, asset, raw, Decimal(proof['broker_quantity']),
            now=datetime.fromisoformat(epoch['reconciled_at']), pagination=epoch['pagination_proof'])
        assert not changed
    after = await asyncio.to_thread(read, repaired)
    assert all(before[table] == after[table] for table in TABLES)
    assert before['reconciliation_records'] == after['reconciliation_records']
    assert not await accounting_issues(repositories)
    assets = {}
    for raw in after['position_lots'].values():
        lot = hydrate('position_lots', raw)
        key = asset_identity_key(lot.asset)
        item = assets.setdefault(key, {'lot_quantity': Decimal(0), 'holding_quantity': Decimal(0),
                                       'closed_quantity': Decimal(0), 'gross_pnl': Decimal(0), 'fill_cash': Decimal(0)})
        item['lot_quantity'] += lot.signed_quantity
    for key, holding in after['holdings'].items():
        assets[key]['holding_quantity'] += Decimal(holding['quantity'])
    for attr in after['trade_attributions'].values():
        key = asset_identity_key(hydrate('trade_attributions', attr).asset)
        assets[key]['closed_quantity'] += Decimal(attr['quantity'])
        assets[key]['gross_pnl'] += Decimal(attr['realized_pnl'])
    for fill in after['fills'].values():
        key = asset_identity_key(hydrate('fills', fill).asset)
        assets[key]['fill_cash'] += Decimal(after['cash_ledger']['fill:cash:' + fill['fill_id']]['amount'])
    for record in after['reconciliation_records'].values():
        if (record['reconciliation_type'] == 'position' and record['subject_id'] in assets
                and record['occurred_at'] >= assets[record['subject_id']].get('broker_checkpoint_at', '')):
            assets[record['subject_id']]['broker_quantity'] = Decimal(record['expected']['broker_qty'])
            assets[record['subject_id']]['broker_checkpoint_at'] = record['occurred_at']
    for key, item in assets.items():
        assert item['broker_quantity'] == item['lot_quantity'] == item['holding_quantity'], key
    assert {k for k, v in assets.items() if v['lot_quantity']} == {'equity:default:alpaca:GOOGL', 'equity:default:alpaca:SPY'}
    aapl = assets['equity:default:alpaca:AAPL']
    assert aapl['closed_quantity'] == Decimal('50.303')
    assert aapl['gross_pnl'] == aapl['fill_cash'] == Decimal('1101.93261')
    assert len({f['broker_fill_id'] for f in after['fills'].values()}) == len(after['fills'])
    assert len({e['fill_id'] for e in after['settlements'].values()}) == len(after['settlements'])
    assert len({(a['lot_id'], a['closing_fill_id']) for a in after['trade_attributions'].values()}) == len(after['trade_attributions'])
    assert len({c['idempotency_key'] for c in after['cash_ledger'].values()}) == len(after['cash_ledger'])
    unallocated = [row for identifier, row in after['cash_ledger'].items() if identifier.startswith('broker:fee:')]
    result = {'assets': assets, 'original_fill_payloads_preserved': True, 'original_reconciliation_receipts_preserved': True,
        'original_lot_ids_preserved': True, 'counts': {t: len(after[t]) for t in TABLES},
        'same_evidence_replay_inserted_rows': inserted, 'same_evidence_financial_and_receipt_hashes_unchanged': True,
        'financial_sha256': {t: digest(after[t]) for t in TABLES}, 'projection_issues': {},
        'unallocated_account_fee_count': len(unallocated),
        'unallocated_account_fee_expense': -sum((Decimal(r['amount']) for r in unallocated), Decimal(0)),
        'aapl_net_pnl': None, 'aapl_net_pnl_status': 'unresolved_account_fee_attribution',
        'epochs': {e['canonical_asset_key']: {'status': e['fee_accounting_status'], 'checkpoint_version': e['checkpoint_version'],
            'checkpoint_id': e['checkpoint_id'], 'reason': e.get('reason')} for e in after['accounting_epochs'].values()}}
    await asyncio.to_thread(report.write_text, json.dumps(decode_payload(encode_payload(result)), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--repaired', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.original, args.repaired, args.report))
