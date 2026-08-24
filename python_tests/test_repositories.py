from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.models import AIResponse, AssetClass, AssetIdentity, Holding
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def test_list_all_returns_every_row_for_non_status_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    holding = Holding(asset, Decimal("10"), Decimal("150"), NOW)
    assert await repositories.holdings.create_once("holding-1", holding)

    rows = await repositories.holdings.list_all()
    assert len(rows) == 1
    assert hydrate("holdings", rows[0]["payload"]) == holding


async def test_list_all_respects_limit_bounds(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        await repositories.holdings.list_all(limit=0)


async def test_list_all_orders_by_created_at_ascending(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    for i in range(3):
        await repositories.holdings.create_once(f"holding-{i}", Holding(asset, Decimal(i + 1), Decimal("150"), NOW))
    rows = await repositories.holdings.list_all()
    assert [row["record_id"] for row in rows] == ["holding-0", "holding-1", "holding-2"]


async def test_delete_is_permitted_on_holdings(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    await repositories.holdings.create_once("holding-1", Holding(asset, Decimal("10"), Decimal("150"), NOW))

    assert await repositories.holdings.delete("holding-1") is True
    assert await repositories.holdings.get("holding-1") is None


async def test_delete_is_rejected_on_append_only_tables(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.fills.delete("fill-1")
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.settlements.delete("settlement-1")
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.trade_intents.delete("intent-1")


async def test_ai_response_persists_for_audit_trail(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    response = AIResponse(
        request_id="req-1", provider="anthropic", model="claude-haiku-4-5", schema_version="1.0",
        completed_at=NOW, result={"candidates": []}, latency_ms=250,
    )
    assert await repositories.ai_responses.create_once("req-1", response)

    row = await repositories.ai_responses.get("req-1")
    assert row is not None
    assert hydrate("ai_responses", row["payload"]) == response

    # A retried scan with the same request_id must not duplicate the audit row.
    assert await repositories.ai_responses.create_once("req-1", response) is False
