"""Deterministic paper journal repair, optionally followed by broker reconciliation.

Run on an offline database copy. Broker access is read-only. Existing immutable
fills and correction receipts are preserved; no balances or flags are patched.
"""
import argparse
import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.settlement.accounting import accounting_issues, replay_accounting


async def run(database_path, apply, report_path, reconcile=False):
    database = AsyncSQLiteDatabase('sqlite:///' + str(database_path))
    repositories = PersistenceRepositories.create(database)
    before = await accounting_issues(repositories)
    snapshots = {}
    backup = None
    changed = 0
    summary = None
    broker = None
    try:
        if reconcile:
            from tradepulse.cli import _load_dotenv, _build_broker
            from tradepulse.config import Settings
            from tradepulse.valuation import marked_snapshot
            _load_dotenv()
            settings = Settings.from_env()
            if settings.execution_mode != 'paper':
                raise ValueError('repair broker verification requires paper mode')
            broker = _build_broker(settings)
            account, positions = await asyncio.gather(broker.get_account(), broker.get_positions())
            snapshots['before'] = decode_payload(encode_payload(await marked_snapshot(repositories, account, positions)))
        if apply:
            def stopped(connection):
                if connection.execute('SELECT 1 FROM locks WHERE expires_at > ?',
                                      (datetime.now(UTC).isoformat(),)).fetchone():
                    raise ValueError('ACCOUNTING_REPAIR_RUNTIME_LEASE_ACTIVE')
                if connection.execute("SELECT 1 FROM fills WHERE json_extract(payload,'$.execution_mode') != 'paper' LIMIT 1").fetchone():
                    raise ValueError('ACCOUNTING_REPAIR_REQUIRES_PAPER_DATABASE')
            await database.run(stopped)
            backup = database_path.with_name(database_path.name + '.accounting-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.bak')
            def copy():
                source = sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True)
                target = sqlite3.connect(backup)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            await asyncio.to_thread(copy)
            changed = await replay_accounting(repositories)
        if broker is not None:
            from tradepulse.alerts import TelegramAlerter
            from tradepulse.settlement import SettlementProcessor
            from tradepulse.reconciliation.coordinator import run_reconciliation
            from tradepulse.valuation import record_valuation, reconciliation_outcome
            if apply:
                alerts = TelegramAlerter(None, None)
                summary = decode_payload(encode_payload(await run_reconciliation(
                    repositories, broker, SettlementProcessor(repositories, alerts), alerts)))
            account, positions = await asyncio.gather(broker.get_account(), broker.get_positions())
            snapshot = await marked_snapshot(repositories, account, positions)
            snapshots['after'] = decode_payload(encode_payload(snapshot))
            snapshots['overall_outcome'] = reconciliation_outcome(snapshot).value
            if apply:
                await record_valuation(repositories, snapshot)
        after = await accounting_issues(repositories)
        decision = 'NOT READY FOR PAPER OPERATION'
        if snapshots and not after:
            results = snapshots['after']['reconciliation_results']
            if (not results.get('integrity_holds') and all(v == 'matched' for field in
                    ('position_quantities', 'holdings_versus_lots') for v in results[field].values())
                    and snapshots['after']['equity_reconciliation_status'] == 'matched'):
                decision = 'READY FOR PAPER OPERATION, PROVE-EDGE BLOCKED'
        report = {'database': str(database_path), 'backup': str(backup) if backup else None,
                  'algorithm_version': 1, 'inserted_rows': changed, 'before': before, 'after': after,
                  'broker_snapshots': snapshots, 'reconciliation_summary': summary,
                  'readiness': decision, 'readiness_scope': 'this database copy and observed broker checkpoint only'}
        await asyncio.to_thread(report_path.write_text, json.dumps(report, indent=2) + '\n')
        logging.getLogger(__name__).info('accounting_repair_complete', extra={'inserted_rows': changed, 'unresolved': len(after), 'report': str(report_path)})
        return 1 if after else 0
    finally:
        if broker is not None:
            await broker.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reconcile', action='store_true', help='read paper broker and run canonical reconciliation when applying')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error('database must exist')
    try:
        return asyncio.run(run(args.database.resolve(), args.apply, args.report.resolve(), args.reconcile))
    except Exception as exc:
        logging.getLogger(__name__).error('accounting_repair_failed', extra={'reason': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
