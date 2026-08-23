from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.execution import has_in_flight_intent
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Side, TradeIntent, TradeIntentStatus
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _seed_intent(repositories: PersistenceRepositories, status: TradeIntentStatus) -> None:
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("5"), status=status,
    )
    await repositories.trade_intents.create_once("ti-1", intent, status=status.value, unique_value=intent.idempotency_key)


async def test_no_intents_means_not_in_flight(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    assert await has_in_flight_intent(repositories, "AAPL") is False


async def test_accepted_intent_blocks_the_symbol(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, TradeIntentStatus.ACCEPTED)
    assert await has_in_flight_intent(repositories, "AAPL") is True
    assert await has_in_flight_intent(repositories, "aapl") is True  # case-insensitive


async def test_submission_unknown_blocks_the_symbol(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, TradeIntentStatus.SUBMISSION_UNKNOWN)
    assert await has_in_flight_intent(repositories, "AAPL") is True


async def test_terminal_intent_does_not_block(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, TradeIntentStatus.FILLED)
    assert await has_in_flight_intent(repositories, "AAPL") is False


async def test_different_symbol_is_unaffected(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, TradeIntentStatus.ACCEPTED)
    assert await has_in_flight_intent(repositories, "MSFT") is False
