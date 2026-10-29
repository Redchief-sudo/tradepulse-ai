"""Verified dividend attribution (Rev.143, spec §3.4-3.7).

Cash is already recorded (dividends.py). This module decides where each
amount belongs: the strategy's lots, inherited holdings (excluded from run
performance through D_excl), or nowhere yet (D_unresolved). It never
invents an allocation (D-6), and it never reports ``excluded_unallocated``
as ``verified`` (TP143-R4-01).

``decide`` is pure. ``attribute_dividends`` is the reconciliation pass
around it: announcements, evidence, the escalation deadline and alerts.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from tradepulse.persistence.codec import decode_payload, encode_payload

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
# G0 (2026-10-29, evidence 2026-10-29-rev143-g0): the paper account has no
# dividend history, so neither representation is established. Every
# dividend's cash is posted; attribution is refused until the first real
# dividend (the verification event) fixes this constant.
DIVIDEND_REPRESENTATION: Literal["gross_and_separate_withholding", "net_with_withholding_record", "unknown"] = "unknown"
WITHHELD_FIELD = "withheld_amount"  # net representation only; unconfirmed until a real sample exists
EXCLUDED_BUCKETS = frozenset({"adopted", "opening_unadopted", "excluded_unallocated"})
OPEN_STATUSES = frozenset({"pending", "unresolved"})
FINAL_REASONS = frozenset({"DIVIDEND_TYPE_UNSUPPORTED", "DIVIDEND_RECEIPT_SHAPE_UNVERIFIED"})  # never retried


class Unresolved(Exception):
    """Attribution cannot proceed: ``final`` means retrying cannot help."""

    def __init__(self, reason: str, *, final: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final


# --- trade dates (step 2) -------------------------------------------------------------


def trade_date(filled_at: datetime, ex_date: date) -> date:
    """The America/New_York date of the fill. Alpaca reports no trade date
    on fills (G0), so a fill from 20:00 ET on the day before the ex-date
    could be the overnight session of the ex-date itself: ambiguous."""
    local = filled_at.astimezone(NEW_YORK)
    if local.date() == ex_date - timedelta(days=1) and local.time() >= time(20):
        raise Unresolved("DIVIDEND_TRADE_DATE_AMBIGUOUS")
    if local.time() >= time(20):  # an overnight-session fill belongs to the next trading day
        return local.date() + timedelta(days=1)
    return local.date()


@dataclass(frozen=True, slots=True)
class StrategyLot:
    lot_id: str
    opened_quantity: Decimal
    opened_at: datetime
    closures: tuple[tuple[Decimal, datetime], ...]  # (quantity, closing fill time)


def eligible_strategy(lots: Sequence[StrategyLot], ex_date: date) -> dict[str, Decimal]:
    """Step 3, strategy: per lot, what was held at the start of the ex-date."""
    eligible = {}
    for lot in lots:
        if trade_date(lot.opened_at, ex_date) >= ex_date:
            continue  # bought on or after the ex-date: not entitled (DV-I3)
        held = lot.opened_quantity - sum((quantity for quantity, at in lot.closures if trade_date(at, ex_date) < ex_date), Decimal(0))
        if held > 0:
            eligible[lot.lot_id] = held
    return eligible


# --- inherited quantity, evidence E1 (step 3) -------------------------------------------


# Cash-only activity naming the asset. Transfers (ACATC, ACATS, JNLS), corporate
# actions and any unknown type make E1 not established.
QUANTITY_SAFE_TYPES = frozenset({"FEE", "CFEE", "PTC", "PTR", "JNLC", "CSD", "CSW", "TRANS", "INT", "INTNRA", "INTTW"})


@dataclass(frozen=True, slots=True)
class Inherited:
    established: bool
    quantity: Decimal | None  # I, when established
    reason: str | None = None
    bucket: str = "opening_unadopted"  # "adopted" once Rev.142 adopted the opening inventory
    holder_id: str = "opening_unadopted"  # the adoption ID for "adopted"


def inherited_quantity(checkpoint: Mapping[str, Any] | None, population: Mapping[str, Any] | None, observed_at: datetime | None,
                       settled_fill_ids: frozenset[str], asset_key: str, ex_date: date, *,
                       adopted_closings: Mapping[str, Decimal] | None = None, adoption_id: str | None = None) -> Inherited | None:
    """E1: a bound checkpoint opened before the ex-date's trade date began,
    and a complete activity population through the end of the trade date
    before it with no quantity change except fills TradePulse settled.
    None when there is no verification run at all."""
    from tradepulse.broker.symbols import broker_symbol_asset_key
    from tradepulse.models import AssetClass, asset_key_from_broker_symbol

    if checkpoint is None:
        return None
    boundary = datetime.combine(ex_date - timedelta(days=1), time(20), NEW_YORK)  # the ex-date's trade date begins
    opened = datetime.fromisoformat(checkpoint["opened_at"])
    if opened >= boundary:
        return Inherited(False, None, "checkpoint_after_ex_date")
    if population is None or observed_at is None or observed_at < boundary:
        return Inherited(False, None, "population_not_through_ex_date")
    classifications = population["classifications"]
    if any(status == "unresolved_generation_membership" for status in classifications.values()):
        return Inherited(False, None, "population_unresolved")
    for raw in population["activities"]:
        symbol = raw.get("symbol")
        if not symbol or broker_symbol_asset_key(symbol) != asset_key or classifications.get(raw["id"]) == "pre_generation":
            continue
        kind = raw.get("activity_type")
        when = raw.get("transaction_time") or raw.get("created_at") or raw.get("date")
        if when is not None and _instant(when) >= boundary:
            continue
        if kind == "FILL":
            if raw["id"] not in settled_fill_ids:
                return Inherited(False, None, "unsettled_fill:" + raw["id"])
            # Rev.140 never sells unadopted inherited inventory; Rev.142's
            # adopted exits do, and are counted below. Others are strategy.
            continue
        if isinstance(kind, str) and (kind.startswith("DIV") or kind in QUANTITY_SAFE_TYPES):
            continue
        return Inherited(False, None, f"quantity_activity:{kind}:{raw['id']}")
    quantity = Decimal(0)
    for position in checkpoint.get("positions", []):
        if asset_key_from_broker_symbol(AssetClass(position["asset_class"]), position["symbol"]) == asset_key:
            quantity += Decimal(position["qty"])
    for raw in population["activities"]:  # adopted exits settled before the ex-date's trade date
        sold = (adopted_closings or {}).get(raw["id"])
        if sold is not None and raw.get("transaction_time") and _instant(raw["transaction_time"]) < boundary:
            quantity -= sold
    if adoption_id is not None:
        return Inherited(True, quantity, bucket="adopted", holder_id=adoption_id)
    return Inherited(True, quantity)


def _instant(value: str) -> datetime:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(0), NEW_YORK)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=NEW_YORK)


# --- the decision (steps 1, 3-6) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    """One activity of a dividend group."""

    activity_id: str
    component: Literal["income", "withholding"]
    amount: Decimal
    qty: Decimal | None
    rate: Decimal | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Announcement:
    announcement_id: str
    sha256: str
    raw: Mapping[str, Any]

    @property
    def ex_date(self) -> date:
        return date.fromisoformat(self.raw["ex_date"])

    @property
    def payable_date(self) -> date:
        return date.fromisoformat(self.raw["payable_date"])


@dataclass(frozen=True, slots=True)
class Decision:
    attribution_status: Literal["verified", "excluded_unallocated", "pending", "unresolved"]
    entitlement_status: Literal["established", "not_established"] | None
    reason: str | None
    announcement: Announcement | None = None
    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _pending(reason: str) -> Decision:
    return Decision("pending", None, reason)


def _unresolved(reason: str, entitlement: Literal["not_established"] | None = None) -> Decision:
    return Decision("unresolved", entitlement, reason)


def candidates(symbol: str, rate: Decimal, credit: date, announcements: Sequence[Announcement], consumed: frozenset[str]) -> list[Announcement]:
    found = []
    for item in announcements:
        raw = item.raw
        if raw.get("ca_type") != "dividend" or raw.get("ca_sub_type") != "cash":
            continue
        if symbol not in (raw.get("initiating_symbol"), raw.get("target_symbol")) or item.announcement_id in consumed:
            continue
        cash = raw.get("cash")
        if not isinstance(cash, str) or Decimal(cash) != rate or item.payable_date > credit:
            continue
        found.append(item)
    return found


def amount_check(income: Member, withholdings: Sequence[Member], representation: str) -> str | None:
    """Step 4; the reason on failure."""
    if income.qty is None or income.rate is None:
        return "DIVIDEND_RECEIPT_INCOMPLETE"
    gross = (income.qty * income.rate).quantize(CENT, rounding=ROUND_HALF_UP)
    if representation == "gross_and_separate_withholding":
        if any(w.amount >= 0 for w in withholdings):
            return "DIVIDEND_REPRESENTATION_INCONSISTENT"
        if income.amount != gross or sum((-w.amount for w in withholdings), Decimal(0)) > income.amount:
            return "DIVIDEND_AMOUNT_MISMATCH"
        return None
    if representation == "net_with_withholding_record":
        if any(w.amount != 0 for w in withholdings):
            return "DIVIDEND_REPRESENTATION_INCONSISTENT"
        withheld = Decimal(0)
        for w in withholdings:
            value = w.raw.get(WITHHELD_FIELD)
            if not isinstance(value, str):
                return "DIVIDEND_RECEIPT_INCOMPLETE"
            withheld += abs(Decimal(value))
        return None if income.amount + withheld == gross else "DIVIDEND_AMOUNT_MISMATCH"
    return "DIVIDEND_REPRESENTATION_UNKNOWN"


def split(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Largest remainder on cents, ties broken by holder ID; sums exactly."""
    if amount != amount.quantize(CENT):
        raise Unresolved("DIVIDEND_AMOUNT_NOT_CENTS")
    total = sum(weights.values(), Decimal(0))
    cents = int(abs(amount) / CENT)
    sign = -1 if amount < 0 else 1
    exact = {holder: Decimal(cents) * weight / total for holder, weight in weights.items()}
    floors = {holder: int(value) for holder, value in exact.items()}
    left = cents - sum(floors.values())
    for holder in sorted(exact, key=lambda h: (-(exact[h] - floors[h]), h))[:left]:
        floors[holder] += 1
    return {holder: sign * Decimal(count) * CENT for holder, count in floors.items()}


def decide(*, symbol: str, credit: date, group: Sequence[Member], announcements: Sequence[Announcement], consumed: frozenset[str],
           lots: Sequence[StrategyLot], inherited_for: Any, quarantined: bool,
           representation: str = DIVIDEND_REPRESENTATION) -> Decision:
    """The attribution of one dividend group (one income and its withholdings).
    ``inherited_for(ex_date)`` gives E1 for that ex-date, or None outside a run."""
    income = next((m for m in group if m.component == "income"), None)
    if income is None:
        return _pending("DIVIDEND_WITHHOLDING_UNPAIRED")
    if quarantined:
        return _unresolved("DIVIDEND_ON_QUARANTINED_INSTRUMENT")
    if representation == "unknown":
        return _unresolved("DIVIDEND_REPRESENTATION_UNKNOWN")
    if income.qty is None or income.rate is None:
        return _unresolved("DIVIDEND_RECEIPT_INCOMPLETE")
    found = candidates(symbol, income.rate, credit, announcements, consumed)
    if not found:
        rated = [a for a in announcements if a.raw.get("ca_sub_type") == "cash" and a.payable_date <= credit]
        return _pending("DIVIDEND_RATE_MISMATCH" if rated else "DIVIDEND_ANNOUNCEMENT_NOT_FOUND")
    try:
        evaluated = []
        for item in found:
            strategy = eligible_strategy(lots, item.ex_date)
            evaluated.append((item, strategy, inherited_for(item.ex_date)))
    except Unresolved as exc:
        return _unresolved(exc.reason)
    by_quantity = [(a, s, i) for a, s, i in evaluated
                   if (i is None and sum(s.values(), Decimal(0)) == income.qty)
                   or (i is not None and i.established and sum(s.values(), Decimal(0)) + (i.quantity or 0) == income.qty)]
    if len(by_quantity) > 1:
        return _pending("DIVIDEND_ANNOUNCEMENT_AMBIGUOUS")
    if by_quantity:
        announcement, strategy, inherited = by_quantity[0]
    else:
        unestablished = [(a, s, i) for a, s, i in evaluated if i is None or not i.established]
        if len(unestablished) != len(evaluated):
            return _pending("DIVIDEND_QUANTITY_MISMATCH")
        if len(evaluated) > 1:
            return _unresolved("DIVIDEND_ANNOUNCEMENT_AMBIGUOUS")
        announcement, strategy, inherited = evaluated[0]
    if announcement.ex_date > announcement.payable_date:
        return _unresolved("DIVIDEND_SPECIAL_EX_DATE")
    withholdings = [m for m in group if m.component == "withholding"]
    failure = amount_check(income, withholdings, representation)
    if failure is not None:
        return _pending(failure) if failure == "DIVIDEND_AMOUNT_MISMATCH" else _unresolved(failure)
    s_total = sum(strategy.values(), Decimal(0))
    if inherited is not None and inherited.established:
        weights: dict[str, Decimal] = dict(strategy)
        buckets = {lot_id: ("strategy", "fills") for lot_id in strategy}
        if inherited.quantity:
            weights[inherited.holder_id] = inherited.quantity
            buckets[inherited.holder_id] = (inherited.bucket, "checkpoint_plus_activity")
        status: Literal["verified", "excluded_unallocated"] = "verified"
        entitlement: Literal["established", "not_established"] = "established"
    elif s_total == 0:
        weights = {"excluded_unallocated": Decimal(1)}
        buckets = {"excluded_unallocated": ("excluded_unallocated", "not_established")}
        status, entitlement = "excluded_unallocated", "not_established"
    elif inherited is None:
        if s_total != income.qty:
            return _unresolved("DIVIDEND_ELIGIBLE_QUANTITY_UNEXPLAINED")
        weights, buckets = dict(strategy), {lot_id: ("strategy", "fills") for lot_id in strategy}
        status, entitlement = "verified", "established"
    else:
        return _unresolved("DIVIDEND_INHERITED_QUANTITY_UNESTABLISHED", "not_established")
    if not weights:
        return _unresolved("DIVIDEND_ELIGIBLE_QUANTITY_UNEXPLAINED")
    try:
        rows = []
        for member in group:
            for holder, amount in split(member.amount, weights).items():
                bucket, evidence = buckets[holder]
                rows.append({"activity_id": member.activity_id, "component": member.component, "bucket": bucket,
                             "holder_id": holder, "eligible_qty": None if bucket == "excluded_unallocated" else str(weights[holder]),
                             "quantity_evidence": evidence, "amount": str(amount),
                             "announcement_id": announcement.announcement_id, "announcement_sha256": announcement.sha256})
    except Unresolved as exc:
        return _unresolved(exc.reason)
    return Decision(status, entitlement, None, announcement, tuple(rows))


def mirror(original_rows: Sequence[Mapping[str, Any]], reversal: Sequence[Member], originals: Sequence[Member]) -> tuple[dict[str, Any], ...] | None:
    """§3.5: a reversal group mirrors a verified group row for row, sign flipped,
    when every member reverses exactly one original member (same component,
    same qty, equal and opposite amount). None when it does not."""
    if len(reversal) != len(originals):
        return None
    pairs = {}
    remaining = list(originals)
    for member in reversal:
        match = next((o for o in remaining if o.component == member.component and o.amount == -member.amount and o.qty == member.qty), None)
        if match is None:
            return None
        remaining.remove(match)
        pairs[match.activity_id] = member.activity_id
    return tuple({**row, "activity_id": pairs[row["activity_id"]], "amount": str(-Decimal(row["amount"]))}
                 for row in original_rows if row["activity_id"] in pairs)


# --- persistence ---------------------------------------------------------------------


def statuses(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row[0]: dict(decode_payload(row[1])) for row in connection.execute("SELECT record_id, payload FROM dividend_attribution_status")}


def write_status(connection: sqlite3.Connection, payload: Mapping[str, Any], now: datetime) -> None:
    connection.execute("UPDATE dividend_attribution_status SET payload=?, updated_at=? WHERE record_id=?",
                       (encode_payload(dict(payload)), now.isoformat(), payload["activity_id"]))


def write_rows(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], now: datetime) -> None:
    for row in rows:
        key = f"{row['activity_id']}|{row['component']}|{row['bucket']}|{row['holder_id']}"
        connection.execute(
            "INSERT INTO dividend_attributions (record_id,activity_id,component,bucket,holder_id,payload,created_at) VALUES (?,?,?,?,?,?,?)",
            (key, row["activity_id"], row["component"], row["bucket"], row["holder_id"], encode_payload(dict(row)), now.isoformat()))


def attribution_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(decode_payload(row[0])) for row in connection.execute("SELECT payload FROM dividend_attributions ORDER BY record_id")]


def dividend_equity(connection: sqlite3.Connection) -> dict[str, Any]:
    """§3.7: D_excl, D_unresolved and the activities whose entitlement is not established."""
    d_excl = sum((Decimal(row["amount"]) for row in attribution_rows(connection) if row["bucket"] in EXCLUDED_BUCKETS), Decimal(0))
    current = statuses(connection)
    open_ids = sorted(a for a, s in current.items() if s["attribution_status"] in OPEN_STATUSES)
    d_unresolved = sum((Decimal(current[a]["cash_amount"]) for a in open_ids), Decimal(0))
    return {"d_excl": d_excl, "d_unresolved": d_unresolved, "unresolved_activity_ids": open_ids,
            "entitlement_not_established": sorted(a for a, s in current.items() if s["attribution_status"] == "excluded_unallocated")}


def strategy_dividend_income(connection: sqlite3.Connection) -> dict[str, Decimal]:
    """§3.6: per strategy lot, its dividend income, apart from price P&L."""
    income: dict[str, Decimal] = {}
    for row in attribution_rows(connection):
        if row["bucket"] == "strategy":
            income[row["holder_id"]] = income.get(row["holder_id"], Decimal(0)) + Decimal(row["amount"])
    return income


# --- the reconciliation pass (steps 1-7) ----------------------------------------------


def _member(receipt: Mapping[str, Any]) -> Member:
    return Member(receipt["activity_id"], "income" if receipt["economic_type"] == "dividend_income" else "withholding",
                  receipt["amount"], receipt["qty"], receipt["per_share_amount"], receipt["raw"])


def _context(connection: sqlite3.Connection) -> dict[str, Any]:
    """Everything the pass reads from the database, in one snapshot."""
    from tradepulse.persistence import hydrate
    from tradepulse.quarantine.legacy import converted_lot_ids
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    from .announcements import stored_announcements
    from .dividends import dividend_receipt

    receipts = {}
    for (payload,) in connection.execute("SELECT payload FROM reconciliation_records WHERE record_id LIKE 'generation_dividend:%'"):
        raw = decode_payload(payload)["actual"]["activity"]
        receipt = dict(dividend_receipt(raw) or {})
        receipt["raw"] = raw
        receipts[receipt["activity_id"]] = receipt
    fills = {row[0]: hydrate("fills", decode_payload(row[1])) for row in connection.execute("SELECT record_id, payload FROM fills")}
    converted = converted_lot_ids(connection)
    lots: dict[str, list[StrategyLot]] = {}
    from tradepulse.models import asset_identity_key

    for (payload,) in connection.execute("SELECT payload FROM position_lots"):
        lot = hydrate("position_lots", decode_payload(payload))
        if lot.position_side != "long" or lot.lot_id in converted or lot.originating_fill_id not in fills:
            continue
        closures = tuple((quantity, fills[fill_id].filled_at) for fill_id, quantity in lot.closures.items() if fill_id in fills)
        lots.setdefault(asset_identity_key(lot.asset), []).append(
            StrategyLot(lot.lot_id, lot.opened_quantity, fills[lot.originating_fill_id].filled_at, closures))
    membership = connection.execute(
        "SELECT payload FROM reconciliation_records WHERE json_extract(payload,'$.reconciliation_type')='generation_membership' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()
    population = decode_payload(membership[0]) if membership else None
    adopted_closings: dict[str, Decimal] = {}
    adoption_by_asset: dict[str, str] = {}
    for (payload,) in connection.execute("SELECT payload FROM position_lots"):
        lot = hydrate("position_lots", decode_payload(payload))
        if lot.origin != "adopted":
            continue
        for fill_id, quantity in lot.closures.items():
            if fill_id in fills and fills[fill_id].broker_fill_id:
                adopted_closings[fills[fill_id].broker_fill_id] = quantity
    for (payload,) in connection.execute("SELECT payload FROM position_adoptions"):
        adoption = decode_payload(payload)
        if adoption["classification"] == "opening_inventory":
            adoption_by_asset[adoption["asset_key"]] = adoption["adoption_id"]
    return {"adopted_closings": adopted_closings, "adoption_by_asset": adoption_by_asset,
            "receipts": receipts, "statuses": statuses(connection), "lots": lots,
            "checkpoint": load_bound_opening_checkpoint(connection),
            "population": population["actual"] if population else None,
            "observed_at": datetime.fromisoformat(population["occurred_at"]) if population else None,
            "settled_fill_ids": frozenset(f.broker_fill_id for f in fills.values() if f.broker_fill_id),
            "announcements": {symbol: stored_announcements(connection, symbol)
                              for symbol in {r["symbol"] for r in receipts.values()}},
            "rows": attribution_rows(connection)}


def _groups(receipts: Mapping[str, Mapping[str, Any]]) -> list[list[str]]:
    """Dividend groups: one income and the withholdings with its symbol and
    credit date; reversals group separately. A withholding whose income is
    not unique stays alone (unpaired)."""
    keyed: dict[tuple[str, str | None, bool], list[str]] = {}
    for activity_id, receipt in sorted(receipts.items()):
        keyed.setdefault((receipt["symbol"], receipt["credit_date"], bool(receipt["reversal"])), []).append(activity_id)
    groups = []
    for ids in keyed.values():
        incomes = [a for a in ids if receipts[a]["economic_type"] == "dividend_income"]
        others = [a for a in ids if a not in incomes]
        if len(incomes) == 1:
            groups.append(incomes + [a for a in others if receipts[a]["economic_type"] == "dividend_withholding"])
            groups.extend([a] for a in others if receipts[a]["economic_type"] != "dividend_withholding")
        else:
            groups.extend([a] for a in ids)
    return groups


async def attribute_dividends(repositories: Any, broker: Any, alerts: Any, now: datetime, *,
                              representation: str | None = None) -> dict[str, str]:
    """Step 7: attempt every open dividend group; returns activity -> status.
    Safeguards hold from the first observation (the activity is in
    D_unresolved while open); the deadline only escalates."""
    from tradepulse.alerts.once import alert_once
    from tradepulse.calendar import market_calendar, ny_date
    from tradepulse.calendar.checks import CalendarGate, session_deadline
    from tradepulse.models import AuditEvent
    from tradepulse.quarantine import quarantined_positions

    from .announcements import refresh_announcements

    representation = representation or DIVIDEND_REPRESENTATION
    database = repositories.audit_events.database
    snapshot = await database.run(_context)
    open_ids = {a for a, s in snapshot["statuses"].items() if s["attribution_status"] in OPEN_STATUSES}
    if not open_ids:
        return {}
    calendar = market_calendar(ny_date(now))
    venue = getattr(broker, "venue", None)
    gate = CalendarGate(repositories, calendar, (venue,) if isinstance(venue, str) else ())
    today = ny_date(now).isoformat()
    refreshed = set()
    for activity_id in sorted(open_ids):
        status = snapshot["statuses"][activity_id]
        receipt = snapshot["receipts"][activity_id]
        if status["escalation_deadline"] is None:
            deadline, source = await session_deadline(gate, datetime.fromisoformat(status["first_observed_at"]))
            status.update(escalation_deadline=deadline.isoformat(), deadline_source=source)
        if status["reason"] == "DIVIDEND_TYPE_UNSUPPORTED":
            await alert_once(repositories.audit_events, alerts, AuditEvent(
                event_id=f"dividend_type_unsupported:{activity_id}", event_type="dividend_type_unsupported", severity="critical",
                message=f"DIVIDEND_TYPE_UNSUPPORTED: {receipt['activity_type']} {receipt['symbol']} {receipt['amount']} "
                        f"(activity {activity_id}); cash recorded, attribution unresolved.",
                occurred_at=now, entity_type="dividend", entity_id=activity_id, details={"activity_id": activity_id}))
        symbol = receipt["symbol"]
        if status.get("announcements_refreshed_on") != today and symbol not in refreshed and representation != "unknown":
            try:
                credit = date.fromisoformat(receipt["credit_date"]) if receipt["credit_date"] else ny_date(now)
                await refresh_announcements(repositories, broker, symbol, credit, now)
                refreshed.add(symbol)
            except Exception as exc:  # noqa: BLE001 - a failed fetch leaves the activity pending until its deadline
                logger.warning("dividend_announcements_unavailable", extra={
                    "event": "dividend_announcements_unavailable", "symbol": symbol, "error": str(exc)})
        if symbol in refreshed:
            status["announcements_refreshed_on"] = today
    if refreshed:
        snapshot = {**snapshot, "announcements": (await database.run(_context))["announcements"]}

    statuses_now = snapshot["statuses"]
    consumed_by_symbol: dict[str, set[str]] = {}
    for activity_id, status in statuses_now.items():
        if status["attribution_status"] in ("verified", "excluded_unallocated") and status.get("announcement_id"):
            consumed_by_symbol.setdefault(snapshot["receipts"][activity_id]["symbol"], set()).add(status["announcement_id"])
    results: dict[str, str] = {}
    for group_ids in _groups(snapshot["receipts"]):
        if not set(group_ids) <= open_ids:
            continue
        members = [_member(snapshot["receipts"][a]) for a in group_ids]
        receipt = snapshot["receipts"][group_ids[0]]
        if any(statuses_now[a]["reason"] in FINAL_REASONS for a in group_ids):
            continue
        symbol, asset_key = receipt["symbol"], receipt["asset_key"]
        if receipt["reversal"]:
            decision = _reverse(snapshot, members)
        else:
            quarantined = bool(await quarantined_positions(repositories, asset_key))
            announcements = [Announcement(a["raw"]["id"], a["sha256"], a["raw"]) for a in snapshot["announcements"].get(symbol, [])]
            credit = date.fromisoformat(receipt["credit_date"]) if receipt["credit_date"] else ny_date(now)

            def e1(ex_date: date, asset_key: str = asset_key) -> Inherited | None:
                return inherited_quantity(snapshot["checkpoint"], snapshot["population"], snapshot["observed_at"],
                                          snapshot["settled_fill_ids"], asset_key, ex_date,
                                          adopted_closings=snapshot["adopted_closings"],
                                          adoption_id=snapshot["adoption_by_asset"].get(asset_key))

            decision = decide(symbol=symbol, credit=credit, group=members, announcements=announcements,
                              consumed=frozenset(consumed_by_symbol.get(symbol, set())), lots=snapshot["lots"].get(asset_key, []),
                              inherited_for=e1, quarantined=quarantined, representation=representation)
        for activity_id in group_ids:
            status = statuses_now[activity_id]
            settled = decision.attribution_status in ("verified", "excluded_unallocated")
            if settled:
                status.update(attribution_status=decision.attribution_status, entitlement_status=decision.entitlement_status,
                              reason=None, announcement_id=decision.announcement.announcement_id if decision.announcement else None,
                              announcement_sha256=decision.announcement.sha256 if decision.announcement else None)
            else:
                status["reason"] = decision.reason
                if decision.attribution_status == "unresolved" or status["attribution_status"] == "unresolved":
                    status["attribution_status"] = "unresolved"
                if decision.entitlement_status:
                    status["entitlement_status"] = decision.entitlement_status
            results[activity_id] = status["attribution_status"]
        if decision.attribution_status in ("verified", "excluded_unallocated"):
            if decision.announcement:
                consumed_by_symbol.setdefault(symbol, set()).add(decision.announcement.announcement_id)
            rows = decision.rows

            def commit(connection: sqlite3.Connection, rows: tuple[dict[str, Any], ...] = rows, ids: list[str] = group_ids) -> None:
                write_rows(connection, rows, now)
                for activity_id in ids:
                    write_status(connection, statuses_now[activity_id], now)

            await database.run(commit, write=True)
            label = "strategy" if decision.attribution_status == "verified" and all(r["bucket"] == "strategy" for r in rows) else \
                "inherited (excluded from run performance)"
            total = sum((m.amount for m in members), Decimal(0))
            await alert_once(repositories.audit_events, alerts, AuditEvent(
                event_id=f"dividend_attributed:{group_ids[0]}", event_type="dividend_attributed", severity="info",
                message=f"Dividend {symbol} ${total} — {label}", occurred_at=now, entity_type="dividend", entity_id=group_ids[0],
                details={"activity_ids": group_ids, "attribution_status": decision.attribution_status}))

    for activity_id in sorted(open_ids):
        status = statuses_now[activity_id]
        if status["attribution_status"] in OPEN_STATUSES and now >= datetime.fromisoformat(status["escalation_deadline"]):
            status["attribution_status"] = "unresolved"
            if status.get("alerted_at") is None:
                receipt = snapshot["receipts"][activity_id]
                await alert_once(repositories.audit_events, alerts, AuditEvent(
                    event_id=f"dividend_attribution_unresolved:{activity_id}", event_type="dividend_attribution_unresolved",
                    severity="critical", occurred_at=now, entity_type="dividend", entity_id=activity_id,
                    message=(f"DIVIDEND_ATTRIBUTION_UNRESOLVED: {receipt['symbol']} {receipt['amount']} (activity {activity_id}): "
                             f"{status['reason']}. Cash is recorded; run evidence stays incomplete until it is attributed."),
                    details={"activity_id": activity_id, "reason": status["reason"]}))
                status["alerted_at"] = now.isoformat()
            results[activity_id] = "unresolved"
        if status["attribution_status"] in OPEN_STATUSES:
            await database.run(lambda c, s=status: write_status(c, s, now), write=True)
    return results


def _reverse(snapshot: Mapping[str, Any], members: Sequence[Member]) -> Decision:
    """§3.5: match exactly one verified original group and mirror its rows."""
    receipts, statuses_now, rows = snapshot["receipts"], snapshot["statuses"], snapshot["rows"]
    symbol = receipts[members[0].activity_id]["symbol"]
    originals = []
    for group_ids in _groups(receipts):
        first = receipts[group_ids[0]]
        if first["symbol"] != symbol or first["reversal"]:
            continue
        if not all(statuses_now.get(a, {}).get("attribution_status") in ("verified", "excluded_unallocated") for a in group_ids):
            continue
        group = [_member(receipts[a]) for a in group_ids]
        mirrored = mirror([r for r in rows if r["activity_id"] in group_ids], members, group)
        if mirrored is not None:
            originals.append((group_ids, mirrored))
    if len(originals) != 1:
        return _unresolved("DIVIDEND_REVERSAL_UNMATCHED")
    group_ids, mirrored = originals[0]
    original = statuses_now[group_ids[0]]
    announcement = Announcement(original["announcement_id"], original["announcement_sha256"], {}) if original.get("announcement_id") else None
    return Decision(original["attribution_status"], original["entitlement_status"], None, announcement, mirrored)
