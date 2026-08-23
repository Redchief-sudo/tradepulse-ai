from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.models import AssetClass, AssetIdentity, Holding
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
