"""Read-only prove-edge assessment. Never writes financial records or decisions."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .integrity import VerificationError, canonical, digest


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    minimum_days: int = 60
    minimum_round_trips: int = 200
    minimum_win_rate_pct: str = "55.0"
    maximum_drawdown_pct: str = "10.0"
    minimum_net_pnl_exclusive: str = "0"
    minimum_expectancy_exclusive: str = "0"
    maximum_unresolved: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


TABLES = (
    "fills", "settlements", "position_lots", "trade_attributions", "trade_intents", "orders",
    "accounting_epochs", "holdings", "cash_ledger", "pnl_records", "equity_snapshots", "reconciliation_records", "integrity_holds", "trading_sessions", "audit_events",
)


def snapshot_database(path: Path) -> dict:
    """One read transaction: no mixed-version pagination and no hidden row limit."""
    import json

    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("BEGIN")
        from tradepulse.persistence import hydrate
        result = {}
        for table in TABLES:
            result[table] = []
            connection.row_factory = sqlite3.Row
            order = "rowid" if table in {"reconciliation_records", "audit_events"} else "record_id"
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                payload = json.loads(row["payload"])
                if table == 'cash_ledger' and (row['record_id'] != payload['entry_id']
                        or row['idempotency_key'] != payload['idempotency_key']):
                    raise VerificationError('canonical_cash_identity_mismatch')
                if table == 'pnl_records' and row['record_id'] != payload['record_id']:
                    raise VerificationError('canonical_pnl_identity_mismatch')
                if table == 'accounting_epochs':
                    from tradepulse.reconciliation.epochs import STATES
                    if payload.get('fee_accounting_status') not in STATES or row['status'] != payload['fee_accounting_status']:
                        raise VerificationError('invalid_accounting_epoch')
                else:
                    hydrate(table, payload)  # canonical model validation, never a synthetic default row
                if table != 'accounting_epochs' and "status" in row.keys() and row["status"] != payload.get("status", payload.get("state", payload.get("hold_type"))):
                    raise VerificationError("persisted_status_mismatch")
                result[table].append(payload)
        return result
    finally:
        connection.close()


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise VerificationError("invalid_numeric_evidence") from exc
    if not result.is_finite():
        raise VerificationError("nonfinite_evidence")
    return result


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise VerificationError("naive_evidence_timestamp")
    return result


def assess(rows: dict, started_at: str, now: datetime, costs: dict | None) -> dict:
    """One completed opening intent is one round trip, never one partial fill.

    Its entire lot population must be closed, every contributing settlement
    complete and verified, and every closure attributed exactly once. Costs
    are a separately frozen verification model on actual persisted notionals;
    gross authoritative accounting is never rewritten.
    """
    policy = VerificationPolicy()
    start = timestamp(started_at)
    if now.tzinfo is None or now < start:
        raise VerificationError("assessment_time_invalid")
    problems: list[str] = []
    fills = {row["fill_id"]: row for row in rows["fills"]}
    settlements = {row["fill_id"]: row for row in rows["settlements"]}
    intents = {row["trade_intent_id"]: row for row in rows["trade_intents"]}
    if len(fills) != len(rows["fills"]) or len(settlements) != len(rows["settlements"]) or len(intents) != len(rows["trade_intents"]):
        problems.append("duplicate_authoritative_identity")
    for fill in fills.values():
        if fill["execution_mode"] != "paper" or not start <= timestamp(fill["filled_at"]) <= now:
            problems.append("fill_outside_paper_generation")
    attrs = defaultdict(list)
    for row in rows["trade_attributions"]:
        attrs[(row["lot_id"], row["closing_fill_id"])].append(row)
    allocated = defaultdict(lambda: Decimal(0))
    for lot in rows["position_lots"]:
        allocated[lot["originating_fill_id"]] += number(lot["opened_quantity"])
        for fill_id, quantity in lot["closures"].items():
            allocated[fill_id] += number(quantity)
    for fill_id, fill in fills.items():
        if allocated[fill_id] != number(fill["quantity"]):
            problems.append("fill_allocation_not_conserved")
        event = settlements.get(fill_id)
        if event is not None and (any(event[key] != fill[key] for key in ("trade_intent_id", "execution_mode", "side", "asset"))
                                  or number(event["quantity"]) != number(fill["quantity"])
                                  or number(event["price"]) != number(fill["price"])):
            problems.append("settlement_fill_mismatch")
    if settlements.keys() - fills.keys() or allocated.keys() - fills.keys():
        problems.append("missing_authoritative_fill")
    fee_allocations = defaultdict(dict)
    fee_basis = defaultdict(lambda: Decimal(0))
    for lot in rows["position_lots"]:
        for fee_id, quantity in lot.get("asset_fee_quantities", {}).items():
            amount = number(quantity)
            if amount <= 0:
                problems.append("asset_fee_quantity_invalid")
            fee_allocations[fee_id][lot["lot_id"]] = amount
            fee_basis[fee_id] += amount * number(lot["acquisition_price"])
    fee_receipts = {}
    valid_fee_receipts = set()
    lots_by_id = {lot["lot_id"]: lot for lot in rows["position_lots"]}
    from tradepulse.models import asset_identity_key
    from tradepulse.persistence import hydrate
    from tradepulse.reconciliation.asset_fees import parse_asset_fee
    from tradepulse.reconciliation.fee_population import validate_fee_population
    populations = {row['record_id']: row for row in rows['reconciliation_records']
                   if row['record_id'].startswith('asset_fee_population:')}
    memberships = {}
    allocation_versions = {}
    records_by_id = {r["record_id"]: r for r in rows["reconciliation_records"]}
    for member in rows['reconciliation_records']:
        if member['record_id'].startswith('asset_fee_membership:') and member['outcome'] == 'matched':
            memberships[member['subject_id']] = member['actual'].get('population_record_id')
            allocation_versions[member['subject_id']] = member['actual'].get('allocation_record_id')
    for row in rows["reconciliation_records"]:
        if row["reconciliation_type"] != "asset_fee" or row["record_id"] != "asset_fee:" + row["subject_id"]:
            continue
        try:
            fee = parse_asset_fee(row["actual"]["activity"])
            population_id = memberships.get(fee.activity_id) or row['actual'].get('population_record_id')
            population = populations[population_id]
            proof = population['actual']
            if population['outcome'] != 'matched' or proof.get('method') != 'complete_instrument_activity_population':
                raise ValueError('fee population authority invalid')
            checked_fees, checked_proof = validate_fee_population(fee.asset, proof['activities'],
                [hydrate('fills', item) for item in fills.values()],
                [hydrate('trade_intents', item) for item in intents.values()], number(proof['broker_quantity']))
            from hashlib import sha256

            from tradepulse.persistence.codec import encode_payload
            if ('asset_fee_population:' + sha256(encode_payload(proof).encode()).hexdigest() != population_id
                    or encode_payload(checked_proof) != encode_payload(proof) or fee not in checked_fees):
                raise ValueError('fee population evidence mismatch')
            allocation = row['actual']
            if allocation_versions.get(fee.activity_id):
                version_id = allocation_versions[fee.activity_id]
                version = records_by_id[version_id]
                allocation = version['actual']
                if (version['outcome'] != 'corrected' or allocation['ledger_record_id'] != row['record_id']
                        or allocation['activity'] != row['actual']['activity']
                        or allocation['population_record_id'] != population_id
                        or version_id != 'asset_fee_allocation:'+sha256(encode_payload(allocation).encode()).hexdigest()):
                    raise ValueError('fee allocation correction invalid')
            allocations = {key: number(value) for key, value in allocation["allocations"].items()}
            if (fee.activity_id in fee_receipts or fee.activity_id != row["subject_id"]
                    or row["outcome"] != "corrected" or not start <= fee.occurred_at <= now
                    or row["actual"]["asset_key"] != asset_identity_key(fee.asset)
                    or allocations != fee_allocations[fee.activity_id]
                    or any(asset_identity_key(hydrate("fills", fills[lots_by_id[lid]["originating_fill_id"]]).asset) != asset_identity_key(fee.asset) for lid in allocations)
                    or sum(allocations.values(), Decimal(0)) != fee.quantity
                    or fee_basis[fee.activity_id] != number(allocation["cost_basis_debit"])):
                problems.append("asset_fee_receipt_mismatch")
            else:
                valid_fee_receipts.add(fee.activity_id)
            fee_receipts[fee.activity_id] = fee
        except (ValueError, KeyError, TypeError, ArithmeticError):
            problems.append("asset_fee_receipt_invalid")
    if fee_allocations.keys() != fee_receipts.keys():
        problems.append("asset_fee_receipt_missing")
    # USD fees are actual account expenses, separately verified from the
    # frozen modeled overlay. Never infer their order from a fee rate.
    from tradepulse.reconciliation.cash_fees import allocate_cash_fees
    cash_entries = {row['entry_id']: row for row in rows.get('cash_ledger', [])}
    cash_records = {row['subject_id']: row for row in rows['reconciliation_records']
                    if row['record_id'].startswith('asset_cash_fee:')}
    cash_memberships = {}
    cash_allocation_versions = {}
    for row in rows['reconciliation_records']:
        if row['record_id'].startswith('asset_cash_fee_membership:') and row['outcome'] == 'matched':
            cash_memberships[row['subject_id']] = row['actual'].get('population_record_id')
            cash_allocation_versions[row['subject_id']] = row['actual'].get('allocation_record_id')
    cash_expenses = defaultdict(lambda: Decimal(0))
    cash_integrity_valid = True
    cash_invalid_assets = set()
    def cash_asset(fee_id):
        for pop in populations.values():
            order = pop['actual'].get('cash_fee_populations', {}).get(fee_id)
            if order:
                return order.get('asset_key')
        return None
    expected_cash_entries = {'asset_cash_fee:'+fee_id for fee_id in cash_records}
    if {key for key in cash_entries if key.startswith('asset_cash_fee:')} != expected_cash_entries:
        problems.append('cash_fee_ledger_missing_or_orphaned')
        for entry_id in {key for key in cash_entries if key.startswith('asset_cash_fee:')} ^ expected_cash_entries:
            key = cash_asset(entry_id.removeprefix('asset_cash_fee:'))
            if key:
                cash_invalid_assets.add(key)
            else:
                cash_integrity_valid = False
    expected_cash_ids = {fid for pop in populations.values() for fid in pop['actual'].get('cash_fee_populations', {})}
    if expected_cash_ids != cash_records.keys():
        problems.append('cash_fee_receipt_missing')
        for fee_id in expected_cash_ids ^ cash_records.keys():
            key = cash_asset(fee_id)
            if key:
                cash_invalid_assets.add(key)
            else:
                cash_integrity_valid = False
    for fee_id, row in cash_records.items():
        try:
            actual = row['actual']
            if cash_allocation_versions.get(fee_id):
                version_id = cash_allocation_versions[fee_id]
                amended = records_by_id[version_id]
                from hashlib import sha256

                from tradepulse.persistence.codec import encode_payload
                if (amended['outcome'] != 'corrected' or amended['actual']['activity'] != actual['activity']
                        or amended['expected']['original_ledger_record_id'] != row['record_id']
                        or version_id != 'cash_fee_allocation:'+sha256(encode_payload(amended['actual']).encode()).hexdigest()):
                    raise ValueError('cash allocation correction invalid')
                actual = amended['actual']
            pop_id = cash_memberships.get(fee_id) or actual['population_record_id']
            pop = populations[pop_id]
            proof = pop['actual']
            order = actual['order']
            closing = next(fill for fill in fills.values() if fill['trade_intent_id'] in order['trade_intent_ids'])
            _, checked = validate_fee_population(hydrate('fills', closing).asset, proof['activities'],
                [hydrate('fills', item) for item in fills.values()],
                [hydrate('trade_intents', item) for item in intents.values()], number(proof['broker_quantity']))
            from hashlib import sha256

            from tradepulse.persistence.codec import encode_payload
            if (pop['outcome'] != 'matched' or encode_payload(checked) != encode_payload(proof)
                    or 'asset_fee_population:'+sha256(encode_payload(proof).encode()).hexdigest() != pop_id):
                raise ValueError('cash fee population mismatch')
            plans = {raw['id']: (raw, linked, allocation) for raw, linked, allocation in
                     allocate_cash_fees(proof, [hydrate('trade_attributions', item) for item in rows['trade_attributions']])}
            raw, linked, allocations = plans[fee_id]
            entry = cash_entries[actual['cash_entry_id']]
            if (row['record_id'] != 'asset_cash_fee:'+fee_id or actual['cash_entry_id'] != row['record_id']
                    or row['outcome'] != 'corrected' or actual['activity'] != raw or actual['order'] != linked
                    or actual['allocation_policy'] != 'verified_sell_population_proceeds_proportion'
                    or {k: number(v) for k, v in actual['allocations'].items()} != allocations
                    or entry['currency'] != 'USD' or number(entry['amount']) != number(raw['net_amount'])
                    or entry['idempotency_key'] != 'alpaca:CFEE:USD:'+fee_id
                    or timestamp(entry['occurred_at']) != timestamp(raw['created_at'])
                    or not start <= timestamp(raw['created_at']) <= now):
                raise ValueError('cash fee expense mismatch')
            by_id = {item['attribution_id']: item for item in rows['trade_attributions']}
            for identifier, amount in allocations.items():
                cash_expenses[by_id[identifier]['lot_id']] += amount
        except (ValueError, KeyError, TypeError, ArithmeticError, StopIteration):
            problems.append('cash_fee_evidence_invalid')
            key = cash_asset(fee_id)
            if key:
                cash_invalid_assets.add(key)
            else:
                cash_integrity_valid = False
    expected = {(lot["lot_id"], fid) for lot in rows["position_lots"] for fid in lot["closures"]}
    missing = len(expected - attrs.keys())
    duplicates = sum(max(0, len(items) - 1) for items in attrs.values())
    if attrs.keys() - expected:
        problems.append("orphan_attribution")
    # Canonical journal destinations must agree with their immutable sources;
    # neither a projection flag nor an attribution summary substitutes for them.
    from tradepulse.models import contract_multiplier_of
    from tradepulse.persistence import hydrate
    pnl_by_id = {row['record_id']: row for row in rows.get('pnl_records', [])}
    invalid_projection_fills = set()
    for fid, fill in fills.items():
        cash = cash_entries.get('fill:cash:' + fid)
        amount = number(fill['quantity']) * number(fill['price']) * contract_multiplier_of(hydrate('fills', fill).asset)
        amount = (amount if fill['side'] == 'sell' else -amount) - number(fill['fees'])
        if (cash is None or number(cash['amount']) != amount or cash.get('currency') != 'USD'
                or cash.get('idempotency_key') != 'fill:cash:' + fid or cash.get('occurred_at') != fill['filled_at']):
            invalid_projection_fills.add(fid)
        if number(fill['fees']):
            fee = pnl_by_id.get('fill:fee:' + fid)
            if fee is None or number(fee['realized']) != -number(fill['fees']):
                invalid_projection_fills.add(fid)
    for attribution in rows['trade_attributions']:
        pnl = pnl_by_id.get('fill:pnl:' + attribution['attribution_id'])
        if (pnl is None or number(pnl['realized']) != number(attribution['realized_pnl'])
                or pnl.get('asset') != attribution['asset'] or pnl.get('as_of') != attribution['exit_at']):
            invalid_projection_fills.add(attribution['closing_fill_id'])
    if invalid_projection_fills:
        problems.append('canonical_cash_or_pnl_projection_missing_or_conflicting')

    def settled(row):
        return row is not None and row['fill_id'] not in invalid_projection_fills and row["status"] == "completed" and all(row.get(key) is True for key in (
            "lot_projected", "attribution_projected", "cash_projected", "holding_projected", "trade_projected", "integrity_verified"
        ))

    settlement_issues = sum(
        1 for row in rows["settlements"]
        if row["status"] != "completed" or not all(row.get(key) is True for key in (
            "lot_projected", "attribution_projected", "cash_projected", "holding_projected", "trade_projected", "integrity_verified"
        ))
    ) + len(fills.keys() - settlements.keys())
    latest = {}
    for row in rows["reconciliation_records"]:
        kind = "position" if row["reconciliation_type"].startswith("position") else row["reconciliation_type"]
        key = (kind, row["subject_id"])
        # Persisted insertion order breaks equal-time ties (not random UUID order).
        if key not in latest or timestamp(row["occurred_at"]) >= timestamp(latest[key]["occurred_at"]):
            latest[key] = row
    reconciliation_issues = sum(row["outcome"] not in {"matched", "corrected"} for row in latest.values())
    # Equity receipts cannot validate position accounting. Every traded identity
    # needs a broker comparison at or after its latest fill, including closed lots.
    from tradepulse.models import asset_identity_key
    from tradepulse.persistence import hydrate
    latest_fill_by_asset = {}
    for fill in fills.values():
        key = asset_identity_key(hydrate("fills", fill).asset)
        at = timestamp(fill["filled_at"])
        if key not in latest_fill_by_asset or at > latest_fill_by_asset[key]:
            latest_fill_by_asset[key] = at
    unreconciled_assets = set()
    for key, at in latest_fill_by_asset.items():
        receipt = latest.get(("position", key))
        if receipt is None or timestamp(receipt["occurred_at"]) < at:
            problems.append("position_reconciliation_missing_or_stale")
            unreconciled_assets.add(key)
        elif receipt["outcome"] not in {"matched", "corrected"}:
            unreconciled_assets.add(key)

    integrity_issues = len(rows["integrity_holds"]) + sum(
        row.get("state") == "financial_integrity_blocked" or row.get("financial_integrity_manual_reenable_required") is True
        for row in rows["trading_sessions"]
    )
    integrity_events = sorted(
        (row for row in rows["audit_events"] if row.get("details", {}).get("action") in
         {"latch_integrity_block", "reset_integrity", "reset_integrity_forced"}),
        key=lambda row: timestamp(row["occurred_at"]),
    )
    if integrity_events and integrity_events[-1]["details"]["action"] != "reset_integrity":
        integrity_issues += 1
    # Explicit immutable fill fees are expenses too. Allocate them across
    # that fill's proven lot population by quantity, preserving the receipt
    # precision and assigning the exact remainder to the final lot.
    from decimal import ROUND_DOWN
    fill_fee_expenses = defaultdict(lambda: Decimal(0))
    for fid, fill in fills.items():
        fee = number(fill['fees'])
        if not fee:
            continue
        portions = []
        for lot in rows['position_lots']:
            qty = (number(lot['opened_quantity']) if lot['originating_fill_id'] == fid else Decimal(0))
            qty += number(lot['closures'].get(fid, '0'))
            if qty:
                portions.append((lot['lot_id'], qty))
        portions.sort()
        if sum((qty for _, qty in portions), Decimal(0)) != number(fill['quantity']):
            problems.append('fill_fee_population_incomplete')
            continue
        remaining = fee
        quantum = Decimal(1).scaleb(fee.as_tuple().exponent)
        for i, (lot_id, qty) in enumerate(portions):
            expense = remaining if i == len(portions)-1 else (fee*qty/number(fill['quantity'])).quantize(quantum, rounding=ROUND_DOWN)
            fill_fee_expenses[lot_id] += expense
            remaining -= expense
    groups = defaultdict(list)
    for lot in rows["position_lots"]:
        opening = fills.get(lot["originating_fill_id"])
        if opening is None:
            problems.append("lot_missing_opening_fill")
            continue
        groups[opening["trade_intent_id"]].append(lot)
    epoch_by_asset = defaultdict(list)
    for epoch in rows.get('accounting_epochs', []):
        epoch_by_asset[epoch['canonical_asset_key']].append(epoch)
    provisional = []
    accounting_breakdown = []
    net_results: list[Decimal] = []
    population: list[str] = []
    costs_available = costs is not None
    if costs_available:
        if set(costs) != {"fee_bps", "slippage_bps"} or any(number(v) < 0 for v in costs.values()):
            raise VerificationError("invalid_frozen_cost_model")
        cost_rate = (number(costs["fee_bps"]) + number(costs["slippage_bps"])) / Decimal("10000")
    else:
        cost_rate = None
    verified_epoch_ids = set()
    for intent_id, lots in sorted(groups.items()):
        if integrity_issues or invalid_projection_fills or reconciliation_issues:
            continue
        if any(asset_identity_key(hydrate("fills", fills[lot["originating_fill_id"]]).asset) in unreconciled_assets for lot in lots):
            continue  # unresolved positions never contribute eligible round trips or net results
        if not cash_integrity_valid and any(lot['asset']['asset_class'] == 'crypto' for lot in lots):
            continue
        if any(asset_identity_key(hydrate('fills', fills[lot['originating_fill_id']]).asset) in cash_invalid_assets for lot in lots):
            continue
        crypto_keys = {asset_identity_key(hydrate('fills', fills[lot['originating_fill_id']]).asset)
                       for lot in lots}
        epoch_valid = True
        for key in crypto_keys:
            epochs = epoch_by_asset.get(key, [])
            if not epochs or any(e['fee_accounting_status'] != 'reconciled_net' for e in epochs):
                epoch_valid = False
                break
            from tradepulse.reconciliation.epochs import verify_checkpoint
            for epoch in epochs:
                try:
                    if epoch['checkpoint_id'] not in verified_epoch_ids:
                        verify_checkpoint(epoch, records_by_id, list(fills.values()), list(intents.values()))
                        verified_epoch_ids.add(epoch['checkpoint_id'])
                except (KeyError, ValueError, TypeError, ArithmeticError, StopIteration):
                    problems.append('crypto_epoch_population_invalid')
                    epoch_valid = False
            if not epoch_valid:
                continue
            latest_epoch = max(epochs, key=lambda e: e['opened_at'])
            actual_lots = sum((number(lot['remaining_quantity']) for lot in rows['position_lots']
                               if asset_identity_key(hydrate('position_lots', lot).asset) == key), Decimal(0))
            actual_holding = sum((number(h['quantity']) for h in rows.get('holdings', [])
                                  if asset_identity_key(hydrate('holdings', h).asset) == key), Decimal(0))
            if number(latest_epoch['ending_broker_quantity']) != actual_lots or actual_holding != actual_lots:
                problems.append('crypto_checkpoint_current_quantity_mismatch')
                epoch_valid = False
            needed = {lot['originating_fill_id'] for lot in lots} | {fid for lot in lots for fid in lot['closures']}
            covered = {fid for e in epochs for fid in e.get('fill_ids', [])}
            if not needed <= covered:
                epoch_valid = False
                problems.append('crypto_epoch_fill_population_incomplete')
        if not epoch_valid:
            provisional.append(intent_id)
            continue
        intent = intents.get(intent_id)
        if intent is None:
            problems.append("missing_opening_intent")
            continue
        if any(number(lot["remaining_quantity"]) != 0 for lot in lots):
            continue
        if intent["status"] != "filled":
            continue  # incomplete opening orders never become a completed sample
        intent_fills = [fill for fill in fills.values() if fill["trade_intent_id"] == intent_id]
        if sum((number(f["quantity"]) for f in intent_fills), Decimal(0)) != number(intent["filled_quantity"]):
            problems.append("opening_fill_quantity_mismatch")
            continue
        if sum((number(lot["opened_quantity"]) for lot in lots), Decimal(0)) != number(intent["filled_quantity"]):
            problems.append("opening_lot_quantity_mismatch")
            continue
        gross = Decimal(0)
        price_pnl = Decimal(0)
        native_expense = Decimal(0)
        usd_expense = Decimal(0)
        notional = Decimal(0)
        valid = True
        for lot in lots:
            opening = fills[lot["originating_fill_id"]]
            if not settled(settlements.get(opening["fill_id"])):
                valid = False
            if any(fee_id not in valid_fee_receipts for fee_id in lot.get("asset_fee_quantities", {})):
                valid = False
            asset_fee_quantity = sum((number(q) for q in lot.get("asset_fee_quantities", {}).values()), Decimal(0))
            if sum((number(q) for q in lot["closures"].values()), Decimal(0)) + asset_fee_quantity != number(lot["opened_quantity"]):
                problems.append("lot_closure_quantity_mismatch")
                valid = False
            from tradepulse.models import contract_multiplier_of
            from tradepulse.persistence import hydrate
            multiplier = contract_multiplier_of(hydrate("fills", opening).asset)
            notional += number(lot["opened_quantity"]) * number(lot["acquisition_price"]) * multiplier
            lot_pnl = Decimal(0)
            for fill_id, quantity in lot["closures"].items():
                closing = fills.get(fill_id)
                event = settlements.get(fill_id)
                attribution = attrs.get((lot["lot_id"], fill_id), [])
                if closing is None or not settled(event) or len(attribution) != 1:
                    valid = False
                    continue
                row = attribution[0]
                if (number(row["quantity"]) != number(quantity) or row["opening_trade_intent_id"] != intent_id
                        or row["closing_trade_intent_id"] != closing["trade_intent_id"]
                        or row["asset"] != lot["asset"] or closing["asset"] != lot["asset"]
                        or number(row["exit_price"]) != number(closing["price"])
                        or number(row["entry_price"]) != number(lot["acquisition_price"])):
                    problems.append("attribution_evidence_mismatch")
                    valid = False
                lot_pnl += number(row["realized_pnl"])
                notional += number(quantity) * number(closing["price"]) * multiplier
            if lot_pnl != number(lot["realized_pnl"]):
                problems.append("attribution_pnl_mismatch")
                valid = False
            price_pnl += lot_pnl
            native_expense += asset_fee_quantity * number(lot['acquisition_price'])
            usd_expense += cash_expenses[lot['lot_id']] + fill_fee_expenses[lot['lot_id']]
            gross += (lot_pnl - asset_fee_quantity * number(lot["acquisition_price"])
                      - cash_expenses[lot["lot_id"]] - fill_fee_expenses[lot['lot_id']])
        if valid:
            population.append(intent_id)
            accounting_breakdown.append({'trade_intent_id': intent_id, 'gross_price_pnl': str(price_pnl),
                                         'actual_asset_fee_basis_expense': str(native_expense),
                                         'actual_cash_fee_expense': str(usd_expense), 'actual_result': str(gross),
                                         'modeled_cost_overlay': str(notional*cost_rate) if cost_rate is not None else None})
            if cost_rate is not None:
                net_results.append(gross - notional * cost_rate)
    count = len(population)
    net = sum(net_results, Decimal(0)) if costs_available else None
    win_rate = Decimal(sum(value > 0 for value in net_results)) / count * 100 if count and costs_available else None
    expectancy = net / count if count and net is not None else None
    snapshots = sorted(rows["equity_snapshots"], key=lambda row: (timestamp(row["as_of"]), row["snapshot_id"]))
    drawdown = None
    if snapshots:
        if any(row["source"] != "broker" or not start <= timestamp(row["as_of"]) <= now for row in snapshots):
            problems.append("equity_outside_broker_generation")
        peak = number(snapshots[0]["total_equity"])
        if peak <= 0:
            problems.append("nonpositive_initial_equity")
        else:
            drawdown = Decimal(0)
            for row in snapshots:
                equity = number(row["total_equity"])
                if equity < 0:
                    raise VerificationError("negative_equity")
                peak = max(peak, equity)
                drawdown = max(drawdown, (peak - equity) / peak * 100)
        if fills and (timestamp(snapshots[0]["as_of"]) > min(timestamp(f["filled_at"]) for f in fills.values())
                      or timestamp(snapshots[-1]["as_of"]) < max(timestamp(f["filled_at"]) for f in fills.values())):
            problems.append("equity_series_does_not_cover_fills")
    criteria = {}

    def criterion(name, actual, operator, required):
        passed = actual is not None and {">=": lambda: actual >= required, "<=": lambda: actual <= required,
                                        ">": lambda: actual > required, "==": lambda: actual == required}[operator]()
        criteria[name] = {"actual": str(actual) if isinstance(actual, Decimal) else actual,
                          "operator": operator, "required": str(required) if isinstance(required, Decimal) else required,
                          "passed": passed}

    criterion("duration_days", (now - start).days, ">=", policy.minimum_days)
    criterion("eligible_round_trips", count, ">=", policy.minimum_round_trips)
    criterion("win_rate_pct", win_rate, ">=", number(policy.minimum_win_rate_pct))
    criterion("maximum_drawdown_pct", drawdown, "<=", number(policy.maximum_drawdown_pct))
    criterion("net_realized_pnl", net, ">", number(policy.minimum_net_pnl_exclusive))
    criterion("net_expectancy", expectancy, ">", number(policy.minimum_expectancy_exclusive))
    criterion("reconciliation_issues", reconciliation_issues if latest else None, "==", policy.maximum_unresolved)
    criterion("settlement_issues", settlement_issues, "==", policy.maximum_unresolved)
    criterion("integrity_issues", integrity_issues, "==", policy.maximum_unresolved)
    criterion("missing_attributions", missing, "==", policy.maximum_unresolved)
    criterion("duplicate_attributions", duplicates, "==", policy.maximum_unresolved)
    criterion("evidence_errors", len(problems), "==", policy.maximum_unresolved)
    integrity_failed = bool(problems or missing or duplicates or settlement_issues or reconciliation_issues or integrity_issues)
    if integrity_failed:
        status = "PROVE_EDGE_FAILED_INTEGRITY"
    elif all(row["passed"] for row in criteria.values()):
        status = "PROVE_EDGE_PASSED"
    elif not criteria["duration_days"]["passed"] or not criteria["eligible_round_trips"]["passed"]:
        status = "PROVE_EDGE_IN_PROGRESS"
    else:
        status = "PROVE_EDGE_THRESHOLD_NOT_MET"
    return {"status": status, "criteria": criteria, "errors": sorted(set(problems)),
            "population": population, "evidence_sha256": digest(canonical(rows)),
            "assessed_at": now.isoformat(), "cost_model": costs,
            "provisional_population": provisional, "accounting_breakdown": accounting_breakdown}
