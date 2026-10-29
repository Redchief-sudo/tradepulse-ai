"""Adopting unmanaged long positions for protective management (Rev.142).

With ``TRADEPULSE_ADOPT_UNMANAGED_POSITIONS`` on, an eligible broker long
position with no local holding or lot becomes an adopted lot plus a
holding, so the existing monitor, gateway and settlement protect and exit it
under the full exit rules. Adoption writes no fill, cash, intent or
settlement (AD-I1), and never authorizes a sale: every exit still passes
Rev.140's sellable bound at the gateway (ADOPT-02).

Identity is in the IDs: the lot is ``adopted:<adoption_id>`` with
``originating_fill_id = adoption:<adoption_id>``, which can never equal a
broker fill ID (AD-I9, AD-I13).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, Literal

from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    Holding,
    PositionLot,
    asset_identity_key,
    asset_key_from_broker_symbol,
)
from tradepulse.models.portfolio import ADOPTED_LOT_PREFIX, ADOPTION_FILL_PREFIX
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload

ADOPTABLE_CLASSES = (AssetClass.EQUITY, AssetClass.OPTION, AssetClass.CRYPTO)
IDENTIFIER_KEYS = frozenset({"account_id", "account_number", "asset_id"})


class AdoptionGateError(RuntimeError):
    """ADOPTION_REQUIRES_REV140:<check>: the runtime refuses to start."""


@dataclass(frozen=True, slots=True)
class AdoptionConfig:
    """Present only when the setting is on and the startup gate passed."""

    account_ref: str
    risk_limits: Any
    market_data: Any


# --- the startup gate (3.1) -------------------------------------------------------------


class _RefusingBroker:
    """Fails the self-check on any call: the refusal must come first."""

    venue: str

    def __init__(self, venue: str) -> None:
        self.venue = venue

    def __getattr__(self, name: str) -> Any:
        raise AdoptionGateError("ADOPTION_REQUIRES_REV140:sellable_enforcement (a broker call came before the refusal)")


async def startup_gate(repositories: PersistenceRepositories, gateway: Any, account_ref: str) -> None:
    """The three operational checks of §3.1, in this process."""
    from tradepulse.execution import ExecutionRequest
    from tradepulse.models import Side
    from tradepulse.persistence.database import verify_schema_triggers
    from tradepulse.reconciliation.unresolved import fill_cursor

    try:
        await repositories.position_adoptions.database.run(verify_schema_triggers)
    except Exception as exc:
        raise AdoptionGateError(f"ADOPTION_REQUIRES_REV140:schema ({exc})") from exc
    if await repositories.fill_cursor_bootstrap.find_by_unique(account_ref) is None or await fill_cursor(repositories) is None:
        raise AdoptionGateError("ADOPTION_REQUIRES_REV140:fill_cursor")
    probe = AssetIdentity("ADOPTIONGATE", AssetClass.EQUITY, "alpaca:ADOPTIONGATE")
    real = gateway._broker
    gateway._broker = _RefusingBroker(real.venue)
    try:
        result = await gateway.execute_intent(ExecutionRequest(
            asset=probe, side=Side.SELL, requested_quantity=Decimal(1), strategy="adoption_gate_self_check",
            decision_id="adoption-gate", exit_reason="stop_loss", symbol_lock_owner_token=None))
    finally:
        gateway._broker = real
    if result.status != "rejected" or result.reasons != ["EXECUTION_RESERVATION_REQUIRED"]:
        raise AdoptionGateError(f"ADOPTION_REQUIRES_REV140:sellable_enforcement ({result.status} {result.reasons})")


# --- classification (3.2) ---------------------------------------------------------------


def _position_asset(position: Any) -> AssetIdentity:
    symbol = position.symbol.upper()
    if position.asset_class == AssetClass.OPTION:
        return AssetIdentity(symbol=symbol, asset_class=position.asset_class, native_asset_id=f"alpaca:{symbol}",
                             contract_multiplier=Decimal(100))
    return AssetIdentity(symbol=symbol, asset_class=position.asset_class, native_asset_id=f"alpaca:{symbol}")


def _open_lots(connection: sqlite3.Connection, key: str) -> list[PositionLot]:
    from tradepulse.quarantine.legacy import converted_lot_ids

    converted = converted_lot_ids(connection)
    lots = [hydrate("position_lots", decode_payload(row[0])) for row in connection.execute("SELECT payload FROM position_lots")]
    return [lot for lot in lots if asset_identity_key(lot.asset) == key and lot.status in ("open", "partially_closed")
            and lot.lot_id not in converted]


def _eligible(connection: sqlite3.Connection, position: Any, key: str) -> str | None:
    """None when eligible; otherwise why not (recorded, today's behaviour kept)."""
    from tradepulse.quarantine.resolve import open_records

    if position.qty <= 0:
        return "UNSUPPORTED_SHORT_POSITION" if position.qty < 0 else "FLAT"
    if position.asset_class not in ADOPTABLE_CLASSES:
        return "ASSET_CLASS_UNSUPPORTED"
    if connection.execute("SELECT 1 FROM holdings WHERE record_id=?", (key,)).fetchone() or _open_lots(connection, key):
        return "PARTIALLY_MANAGED"
    if open_records(connection, key):
        return "QUARANTINED"
    return None


def classify(checkpoint: dict[str, Any] | None, key: str, qty: Decimal) -> tuple[Literal["opening_inventory", "unexpected_external"], str | None]:
    """``opening_inventory`` only with a bound checkpoint listing this asset at
    exactly this quantity; returns (classification, checkpoint_id or None)."""
    if checkpoint is not None:
        for position in checkpoint.get("positions", []):
            if asset_key_from_broker_symbol(AssetClass(position["asset_class"]), position["symbol"]) == key \
                    and Decimal(position["qty"]) == qty:
                return "opening_inventory", checkpoint["checkpoint_id"]
    return "unexpected_external", None


# --- the stability episode (3.3.1) ------------------------------------------------------


def observe(connection: sqlite3.Connection, account_ref: str, key: str, qty: Decimal, now: datetime) -> str | None:
    """Record one sighting of an unexpected position. Returns the episode ID
    when the previous sighting matched this quantity (stable), else None.
    Re-checks under the write lock that the asset is still unmanaged, so a
    cycle that lost a race to an adoption records nothing."""
    if connection.execute("SELECT 1 FROM holdings WHERE record_id=?", (key,)).fetchone() or _open_lots(connection, key):
        return None
    row = connection.execute("SELECT record_id, payload FROM unmanaged_observations WHERE account_ref=? AND asset_key=? "
                             "AND status='observing'", (account_ref, key)).fetchone()
    if row is not None:
        payload = dict(decode_payload(row[1]))
        if Decimal(payload["first_qty"]) == qty and Decimal(payload["last_qty"]) == qty:
            payload.update(last_seen_at=now.isoformat())
            connection.execute("UPDATE unmanaged_observations SET payload=?, updated_at=? WHERE record_id=?",
                               (encode_payload(payload), now.isoformat(), row[0]))
            return row[0]
        _close_episode(connection, row[0], "superseded", now)
    episode = f"{account_ref}:{key}:{now.isoformat()}"
    payload = {"episode_id": episode, "account_ref": account_ref, "asset_key": key, "first_qty": str(qty), "last_qty": str(qty),
               "first_seen_at": now.isoformat(), "last_seen_at": now.isoformat(), "status": "observing"}
    connection.execute("INSERT INTO unmanaged_observations (record_id,account_ref,asset_key,status,payload,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (episode, account_ref, key, "observing", encode_payload(payload), now.isoformat(), now.isoformat()))
    return None


def _close_episode(connection: sqlite3.Connection, episode: str, status: str, now: datetime) -> None:
    row = connection.execute("SELECT payload FROM unmanaged_observations WHERE record_id=?", (episode,)).fetchone()
    payload = {**decode_payload(row[0]), "status": status, "closed_at": now.isoformat()}
    connection.execute("UPDATE unmanaged_observations SET status=?, payload=?, updated_at=? WHERE record_id=?",
                       (status, encode_payload(payload), now.isoformat(), episode))


def close_vanished(connection: sqlite3.Connection, account_ref: str, held: dict[str, Decimal], now: datetime) -> list[str]:
    vanished = []
    for episode, key in connection.execute("SELECT record_id, asset_key FROM unmanaged_observations WHERE account_ref=? "
                                           "AND status='observing'", (account_ref,)).fetchall():
        if held.get(key, Decimal(0)) <= 0:
            _close_episode(connection, episode, "vanished", now)
            vanished.append(episode)
    return vanished


# --- the adoption transaction (3.3.3) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Adoption:
    adoption_id: str
    account_ref: str
    asset_key: str
    asset: AssetIdentity
    classification: Literal["opening_inventory", "unexpected_external"]
    source_id: str
    adopted_quantity: Decimal
    broker_avg_entry_price: Decimal
    adoption_mark_price: Decimal
    adopted_at: datetime
    stop_loss: Decimal
    stop_source: str
    candles_used: dict[str, Any]
    broker_observation: dict[str, Any]
    basis_provenance: str = "broker_avg_entry_unverified"


def adoption_payload(adoption: Adoption) -> dict[str, Any]:
    return {"adoption_id": adoption.adoption_id, "account_ref": adoption.account_ref, "asset_key": adoption.asset_key,
            "asset": adoption.asset, "classification": adoption.classification, "source_id": adoption.source_id,
            "adopted_quantity": adoption.adopted_quantity, "broker_avg_entry_price": adoption.broker_avg_entry_price,
            "adoption_mark_price": adoption.adoption_mark_price, "adopted_at": adoption.adopted_at,
            "basis_provenance": adoption.basis_provenance, "stop_loss": adoption.stop_loss, "stop_source": adoption.stop_source,
            "candles_used": adoption.candles_used, "broker_observation": adoption.broker_observation}


def write_adoption(connection: sqlite3.Connection, adoption: Adoption, episode: str | None) -> bool:
    """One transaction: the adoption row, the lot, the holding and the audit
    event (and, for crypto, the adopted accounting epoch). The eligibility
    re-check makes a concurrent loser do nothing."""
    if connection.execute("SELECT 1 FROM holdings WHERE record_id=?", (adoption.asset_key,)).fetchone() \
            or _open_lots(connection, adoption.asset_key) or connection.execute(
            "SELECT 1 FROM position_adoptions WHERE account_ref=? AND asset_key=? AND source_id=?",
            (adoption.account_ref, adoption.asset_key, adoption.source_id)).fetchone():
        return False
    at = adoption.adopted_at.isoformat()
    connection.execute("INSERT INTO position_adoptions (record_id,account_ref,asset_key,source_id,payload,created_at) VALUES (?,?,?,?,?,?)",
                       (adoption.adoption_id, adoption.account_ref, adoption.asset_key, adoption.source_id,
                        encode_payload(adoption_payload(adoption)), at))
    lot = PositionLot(lot_id=ADOPTED_LOT_PREFIX + adoption.adoption_id, originating_fill_id=ADOPTION_FILL_PREFIX + adoption.adoption_id,
                      asset=adoption.asset, position_side="long", opened_quantity=adoption.adopted_quantity,
                      remaining_quantity=adoption.adopted_quantity, acquisition_price=adoption.broker_avg_entry_price,
                      opened_at=adoption.adopted_at, mfe_price=adoption.adoption_mark_price, mae_price=adoption.adoption_mark_price)
    connection.execute("INSERT INTO position_lots (record_id,originating_fill_id,payload,created_at,updated_at) VALUES (?,?,?,?,?)",
                       (lot.lot_id, lot.originating_fill_id, encode_payload(lot), at, at))
    holding = Holding(asset=adoption.asset, quantity=adoption.adopted_quantity, average_price=adoption.broker_avg_entry_price,
                      updated_at=adoption.adopted_at, stop_loss=adoption.stop_loss, target_price=None)
    connection.execute("INSERT INTO holdings (record_id,payload,created_at,updated_at) VALUES (?,?,?,?)",
                       (adoption.asset_key, encode_payload(holding), at, at))
    if adoption.asset.asset_class == AssetClass.CRYPTO:
        from tradepulse.reconciliation.epochs import adopted_epoch_payload, save_epoch

        _, epoch = adopted_epoch_payload(connection, adoption)
        save_epoch(connection, epoch, adoption.adopted_at)
    event = adoption_event(adoption)
    connection.execute("INSERT OR IGNORE INTO audit_events (record_id,payload,created_at) VALUES (?,?,?)",
                       (event.event_id, encode_payload(event), at))
    if episode is not None:
        _close_episode(connection, episode, "adopted", adoption.adopted_at)
    return True


def adoption_event(adoption: Adoption) -> AuditEvent:
    label = adoption.classification.replace("_", " ")
    fallback = adoption.stop_source.replace("pct_fallback", "% fallback")
    return AuditEvent(
        event_id=f"adopted_position:{adoption.adoption_id}", event_type="adopted_position", severity="warning",
        occurred_at=adoption.adopted_at, entity_type="position_adoption", entity_id=adoption.adoption_id,
        message=(f"Adopted {adoption.asset.symbol} {adoption.adopted_quantity} ({label}) — basis ${adoption.broker_avg_entry_price} "
                 f"(broker, unverified), mark ${adoption.adoption_mark_price}, stop ${adoption.stop_loss} ({fallback})"
                 + (" — stop already breached" if adoption.adoption_mark_price <= adoption.stop_loss else "")),
        details={"adoption_id": adoption.adoption_id, "classification": adoption.classification, "stop_source": adoption.stop_source})


async def adoption_pass(repositories: PersistenceRepositories, positions: list[Any], config: AdoptionConfig, alerts: Any,
                        now: datetime) -> list[str]:
    """§3.4 step 2: adopt every eligible position; returns the new adoption IDs."""
    from tradepulse.alerts.once import alert_once
    from tradepulse.scanner.coordinator import entry_stop_loss
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    database = repositories.position_adoptions.database
    checkpoint = await database.run(load_bound_opening_checkpoint)
    held = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p.qty for p in positions}
    await database.run(lambda c: close_vanished(c, config.account_ref, held, now), write=True)
    adopted = []
    for position in positions:
        try:
            key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
        except (ValueError, KeyError):
            continue
        if await database.run(partial(_eligible, position=position, key=key)) is not None:
            continue
        classification, source_id = classify(checkpoint, key, position.qty)
        episode = None
        if classification == "unexpected_external":
            episode = await database.run(partial(observe, account_ref=config.account_ref, key=key, qty=position.qty, now=now),
                                         write=True)
            if episode is None:
                continue  # stability needs a second matching sighting
            source_id = episode
        asset = _position_asset(position)
        refusal = None
        if position.avg_entry_price <= 0 or position.current_price <= 0:
            refusal = "price"
        stop, stop_source, candles = None, None, []
        if refusal is None:
            if asset.asset_class != AssetClass.OPTION:
                try:
                    candles = await config.market_data.fetch_candles(asset)
                except Exception:  # noqa: BLE001 - a failed fetch is the documented percentage fallback, not a refusal
                    candles = []
            try:
                stop, stop_source = entry_stop_loss(position.avg_entry_price, list(candles), asset.asset_class, config.risk_limits)
            except Exception:  # noqa: BLE001 - an invalid profile refuses adoption
                refusal = "profile"
            if stop is None and refusal is None:
                refusal = "no_stop_policy"
        if refusal is not None or stop is None or stop_source is None:
            await alert_once(repositories.audit_events, alerts, AuditEvent(
                event_id=f"adoption_stop_input_invalid:{key}:{now.date().isoformat()}", event_type="adoption_stop_input_invalid",
                severity="critical", occurred_at=now, entity_type="asset", entity_id=key,
                message=f"ADOPTION_STOP_INPUT_INVALID: {position.symbol} ({refusal}); the position stays unmanaged.",
                details={"asset_key": key, "reason": refusal}))
            continue
        adoption_id = f"{source_id}|{key}" if classification == "opening_inventory" else f"{source_id}"
        adoption = Adoption(
            adoption_id=_safe_id(adoption_id), account_ref=config.account_ref, asset_key=key, asset=asset, classification=classification,
            source_id=str(source_id), adopted_quantity=position.qty, broker_avg_entry_price=position.avg_entry_price,
            adoption_mark_price=position.current_price, adopted_at=now, stop_loss=stop, stop_source=stop_source,
            candles_used={"count": len(candles), "first": candles[0].date if candles else None,
                          "last": candles[-1].date if candles else None},
            broker_observation={k: v for k, v in dict(position.raw).items() if k not in IDENTIFIER_KEYS})
        try:
            written = await database.run(partial(write_adoption, adoption=adoption, episode=episode), write=True)
        except sqlite3.IntegrityError:
            written = False  # a concurrent winner with the same source ID (UNIQUE): nothing to do
        if written:
            adopted.append(adoption.adoption_id)
            await alert_once(repositories.audit_events, alerts, adoption_event(adoption))
    return adopted


def _safe_id(raw: str) -> str:
    from hashlib import sha256

    return "adp_" + sha256(raw.encode()).hexdigest()[:24]


# --- reads used elsewhere ---------------------------------------------------------------


def adoptions(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row[0]: dict(decode_payload(row[1])) for row in connection.execute("SELECT record_id, payload FROM position_adoptions")}


def adoption_for_lot(connection: sqlite3.Connection, lot: PositionLot) -> dict[str, Any]:
    """The adopted lot's opening evidence. Raises for an unknown origin or a
    missing row (fail closed): there is never a fill to fall back on."""
    if lot.origin != "adopted":
        raise ValueError(f"LOT_NOT_ADOPTED: {lot.lot_id}")
    row = connection.execute("SELECT payload FROM position_adoptions WHERE record_id=?", (lot.adoption_id,)).fetchone()
    if row is None:
        raise ValueError(f"ADOPTION_MISSING: {lot.adoption_id}")
    return dict(decode_payload(row[0]))


def reentry_excluded(connection: sqlite3.Connection, account_ref: str | None, key: str) -> bool:
    """§3.9 Q2: a strategy BUY of an adopted asset is refused -- inside a
    generation when adopted during it (or from its checkpoint), and outside
    a generation unconditionally. Derived from immutable rows: durable."""
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    checkpoint = load_bound_opening_checkpoint(connection)
    for payload in adoptions(connection).values():
        if payload["asset_key"] != key or (account_ref is not None and payload["account_ref"] != account_ref):
            continue
        if checkpoint is None:
            return True
        if payload["source_id"] == checkpoint["checkpoint_id"] or datetime.fromisoformat(payload["adopted_at"]) >= \
                datetime.fromisoformat(checkpoint["opened_at"]):
            return True
    return False
