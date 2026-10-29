"""Rev.142: lot identity, the shared stop policy, schema and the source guard (T1, T6, T23, T26, T31)."""
from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradepulse.adoption import classify
from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Candle, PositionLot
from tradepulse.persistence import AsyncSQLiteDatabase, hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.persistence.database import DatabaseError
from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price, entry_stop_loss

ROOT = Path(__file__).resolve().parent.parent
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
AT = datetime(2026, 11, 2, 15, tzinfo=UTC)


def _old_policy(reference: Decimal, candles: list[Candle], asset_class: AssetClass, limits) -> Decimal | None:  # type: ignore[no-untyped-def]
    """The scanner's inline selection before the extraction, verbatim."""
    if asset_class == AssetClass.OPTION:
        return _stop_loss_price(reference, limits.options_premium_stop_pct, AssetClass.OPTION) if limits.options_premium_stop_pct > 0 else None
    return ((_atr_stop_loss_price(reference, candles, limits.atr_stop_multiplier, asset_class, limits.min_stop_distance_pct,
                                  limits.max_stop_distance_pct) if limits.atr_stop_multiplier > 0 else None)
            or (_stop_loss_price(reference, limits.stop_loss_pct, asset_class) if limits.stop_loss_pct > 0 else None))


def _candles(n: int, spread: str = "2") -> list[Candle]:
    return [Candle(f"2026-10-{k + 1:02d}", Decimal(100), Decimal(100) + Decimal(spread), Decimal(100) - Decimal(spread), Decimal(100),
                   Decimal(1000)) for k in range(n)]


@pytest.mark.parametrize("profile", ["conservative", "balanced", "aggressive"])
@pytest.mark.parametrize(("asset_class", "candles", "source"), [
    (AssetClass.EQUITY, _candles(30), "atr"), (AssetClass.EQUITY, [], "pct_fallback"),
    (AssetClass.EQUITY, _candles(30, "40"), None), (AssetClass.CRYPTO, _candles(30), "atr"), (AssetClass.OPTION, [], "option_premium_pct"),
])
def test_entry_stop_loss_reproduces_the_scanner(profile: str, asset_class: AssetClass, candles: list[Candle], source: str | None) -> None:
    """T1: the same stop as before the extraction, with its source recorded."""
    limits = risk_limits_for_profile(profile)
    stop, used = entry_stop_loss(Decimal(100), candles, asset_class, limits)
    assert stop == _old_policy(Decimal(100), candles, asset_class, limits)
    if source is not None:
        assert used == source
    else:  # an out-of-band ATR falls back to the percentage
        assert used == "pct_fallback"


def test_classification() -> None:
    """T6: the checkpoint at exactly this quantity is opening inventory; otherwise unexpected."""
    checkpoint = {"checkpoint_id": "cp", "positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    key = "equity:default:alpaca:AAPL"
    assert classify(checkpoint, key, Decimal(5)) == ("opening_inventory", "cp")
    assert classify(checkpoint, key, Decimal(4))[0] == "unexpected_external"
    assert classify(None, key, Decimal(5))[0] == "unexpected_external"


def test_adopted_identity_and_legacy_defaults() -> None:
    """T31: identity is derived from the IDs; round trips keep it; legacy
    rows are fill lots; inconsistent IDs are refused."""
    lot = PositionLot(lot_id="adopted:adp_1", originating_fill_id="adoption:adp_1", asset=AAPL, position_side="long",
                      opened_quantity=Decimal(5), remaining_quantity=Decimal(5), acquisition_price=Decimal(100), opened_at=AT)
    again = hydrate("position_lots", __import__("json").loads(encode_payload(lot)))
    assert (again.origin, again.adoption_id) == ("adopted", "adp_1")
    legacy = PositionLot(lot_id="L", originating_fill_id="fill-1", asset=AAPL, position_side="long", opened_quantity=Decimal(1),
                         remaining_quantity=Decimal(1), acquisition_price=Decimal(1), opened_at=AT)
    assert (legacy.origin, legacy.adoption_id) == ("fill", None)
    for lot_id, fill_id in (("adopted:a", "fill-1"), ("L", "adoption:a"), ("adopted:a", "adoption:b")):
        with pytest.raises(ValueError, match="adopted lot"):
            PositionLot(lot_id=lot_id, originating_fill_id=fill_id, asset=AAPL, position_side="long", opened_quantity=Decimal(1),
                        remaining_quantity=Decimal(1), acquisition_price=Decimal(1), opened_at=AT)
    with pytest.raises(ValueError, match="only long"):
        PositionLot(lot_id="adopted:a", originating_fill_id="adoption:a", asset=AAPL, position_side="short", opened_quantity=Decimal(1),
                    remaining_quantity=Decimal(1), acquisition_price=Decimal(1), opened_at=AT)


async def test_position_adoptions_is_immutable(tmp_path: Path) -> None:
    """T23."""
    path = tmp_path / "a.db"
    await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO position_adoptions VALUES ('a','acct','k','s','{}','t')")
        for statement in ("UPDATE position_adoptions SET payload='x'", "DELETE FROM position_adoptions"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO position_adoptions VALUES ('b','acct','k','s','{}','t')")  # UNIQUE(account, asset, source)
        connection.execute("DROP TRIGGER quarantine_no_update_position_adoptions")
    with pytest.raises(DatabaseError, match="SCHEMA_TRIGGER_MISSING:quarantine_no_update_position_adoptions"):
        await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()


# Every read of originating_fill_id, by (module, enclosing function), and how its adopted case is handled.
APPROVED_READS = {
    ("tradepulse/settlement/engine.py", "_project_attribution"): "adopted branch before the fill lookup (T11 runtime)",
    ("tradepulse/settlement/engine.py", "_entry_protective_levels"): "adopted branch: the adoption's stop (T11)",
    ("tradepulse/settlement/accounting.py", "pnl_evidence"): "compares to the fill; an adoption: ID never equals one",
    ("tradepulse/settlement/accounting.py", "_opening_evidence"): "adopted branch: the adoption row (T11)",
    ("tradepulse/settlement/accounting.py", "__init__"): "_AdoptedOpening: the adoption: ID as the opening reference, never a lookup",
    ("tradepulse/adoption/__init__.py", "write_adoption"): "write-only: the lot row's UNIQUE column",
    ("tradepulse/persistence/hydration.py", "decode_position_lot"): "write-only: hydration copies the field",
    ("tradepulse/settlement/lots.py", "plan_signed_lot_fill"): "matches the event's fill; never an adoption: ID",
    ("tradepulse/reconciliation/coordinator.py", "_rebuild_holding_from_lots"): "adopted branch: the adoption's stop (T11)",
    ("tradepulse/reconciliation/historical_fees.py", "preview_fee_replay"): "adopted lots refused first (fail closed)",
    ("tradepulse/reconciliation/fee_replay.py", "apply"): "adopted lots refused first (fail closed)",
    ("tradepulse/reconciliation/epochs.py", "finalize_population"): "selects by adoption_id for an adopted epoch (T28)",
    ("tradepulse/reconciliation/dividend_attribution.py", "_context"): "only fill-origin lots are strategy lots",
    ("tradepulse/quarantine/legacy.py", "load"): "short lots only; an adopted lot is long and refused first",
    ("tradepulse/verification/evidence.py", "assess"): "adopted lots are their own class; asset from the lot (T24)",
    ("tradepulse/models/portfolio.py", "__post_init__"): "validates the adoption: prefix",
}


class _Reads(ast.NodeVisitor):
    def __init__(self, module: str, found: set[tuple[str, str]]) -> None:
        self.module, self.found, self.stack = module, found, []  # type: list[str]

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _hit(self) -> None:
        self.found.add((self.module, self.stack[-1] if self.stack else "<module>"))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "originating_fill_id" and isinstance(node.ctx, ast.Load):
            self._hit()
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Constant) and node.slice.value == "originating_fill_id":
            self._hit()
        self.generic_visit(node)


def _reads() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "tradepulse").rglob("*.py")):
        _Reads(str(path.relative_to(ROOT)), found).visit(ast.parse(path.read_text()))
    return found


def test_every_originating_fill_id_read_is_approved() -> None:
    """T26: a new or moved read fails until it is listed with its adopted-lot handling."""
    reads = _reads()
    assert reads - set(APPROVED_READS) == set(), "unlisted originating_fill_id reads"
    assert set(APPROVED_READS) - reads == set(), "listed sites that no longer exist"
