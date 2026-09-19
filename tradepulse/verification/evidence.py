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
    "equity_snapshots", "reconciliation_records", "integrity_holds", "trading_sessions", "audit_events",
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
                hydrate(table, payload)  # canonical model validation, never a synthetic default row
                if "status" in row.keys() and row["status"] != payload.get("status", payload.get("state", payload.get("hold_type"))):
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
    for row in rows["reconciliation_records"]:
        if row["reconciliation_type"] != "asset_fee" or row["record_id"] != "asset_fee:" + row["subject_id"]:
            continue
        try:
            fee = parse_asset_fee(row["actual"]["activity"])
            allocations = {key: number(value) for key, value in row["actual"]["allocations"].items()}
            if (fee.activity_id in fee_receipts or fee.activity_id != row["subject_id"]
                    or row["outcome"] != "corrected" or not start <= fee.occurred_at <= now
                    or row["actual"]["asset_key"] != asset_identity_key(fee.asset)
                    or allocations != fee_allocations[fee.activity_id]
                    or any(asset_identity_key(hydrate("fills", fills[lots_by_id[lid]["originating_fill_id"]]).asset) != asset_identity_key(fee.asset) for lid in allocations)
                    or sum(allocations.values(), Decimal(0)) != fee.quantity
                    or fee_basis[fee.activity_id] != number(row["actual"]["cost_basis_debit"])):
                problems.append("asset_fee_receipt_mismatch")
            else:
                valid_fee_receipts.add(fee.activity_id)
            fee_receipts[fee.activity_id] = fee
        except (ValueError, KeyError, TypeError, ArithmeticError):
            problems.append("asset_fee_receipt_invalid")
    if fee_allocations.keys() != fee_receipts.keys():
        problems.append("asset_fee_receipt_missing")
    expected = {(lot["lot_id"], fid) for lot in rows["position_lots"] for fid in lot["closures"]}
    missing = len(expected - attrs.keys())
    duplicates = sum(max(0, len(items) - 1) for items in attrs.values())
    if attrs.keys() - expected:
        problems.append("orphan_attribution")
    def settled(row):
        return row is not None and row["status"] == "completed" and all(row.get(key) is True for key in (
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
    groups = defaultdict(list)
    for lot in rows["position_lots"]:
        opening = fills.get(lot["originating_fill_id"])
        if opening is None:
            problems.append("lot_missing_opening_fill")
            continue
        groups[opening["trade_intent_id"]].append(lot)
    net_results: list[Decimal] = []
    population: list[str] = []
    costs_available = costs is not None
    if costs_available:
        if set(costs) != {"fee_bps", "slippage_bps"} or any(number(v) < 0 for v in costs.values()):
            raise VerificationError("invalid_frozen_cost_model")
        cost_rate = (number(costs["fee_bps"]) + number(costs["slippage_bps"])) / Decimal("10000")
    else:
        cost_rate = None
    for intent_id, lots in sorted(groups.items()):
        if any(asset_identity_key(hydrate("fills", fills[lot["originating_fill_id"]]).asset) in unreconciled_assets for lot in lots):
            continue  # unresolved positions never contribute eligible round trips or net results
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
            gross += lot_pnl - asset_fee_quantity * number(lot["acquisition_price"])
        if valid:
            population.append(intent_id)
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
            "assessed_at": now.isoformat(), "cost_model": costs}
