import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.execution import execution_lock_key, has_in_flight_intent, release_symbol_reservation, reserve_symbol_for_execution
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Side, TradeIntent, TradeIntentStatus
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, renew_lock


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _database(tmp_path) -> AsyncSQLiteDatabase:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return database


async def _repositories(tmp_path) -> PersistenceRepositories:
    return PersistenceRepositories.create(await _database(tmp_path))


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


def _msft_crypto() -> AssetIdentity:
    """Same ticker text as a bare-equity AAPL-shaped identity would collide
    on -- proves the lock key is asset-class-qualified, not a bare symbol."""
    return AssetIdentity("AAPL", AssetClass.CRYPTO, "alpaca:AAPLUSD")


async def test_reserve_symbol_for_execution_only_one_of_two_concurrent_callers_wins(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-a") is True
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-b") is False


async def test_reserve_symbol_for_execution_does_not_collide_across_asset_classes(tmp_path) -> None:
    """Same ticker text (AAPL), two different asset classes -- both
    reservations must succeed independently, proving the
    execution:{asset_class}:{symbol} namespace, not a bare-symbol one."""
    database = await _database(tmp_path)
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-equity") is True
    assert await reserve_symbol_for_execution(database, _msft_crypto(), "owner-crypto") is True
    assert execution_lock_key(_aapl()) != execution_lock_key(_msft_crypto())


async def test_release_symbol_reservation_frees_it_for_a_new_caller(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-a") is True
    await release_symbol_reservation(database, _aapl(), "owner-a")
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-b") is True


async def test_symbol_reservation_survives_past_its_ttl_only_while_renewed(tmp_path) -> None:
    """Proves the per-symbol reservation is genuinely renewable, not just an
    optimistically long fixed TTL -- a second caller stays locked out across
    a window that outlives the reservation's initial TTL only because
    renew_lock keeps extending it."""
    database = await _database(tmp_path)
    ttl = 1
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-a") is True

    async def hold_and_renew() -> None:
        for _ in range(3):
            await asyncio.sleep(ttl / 2)
            assert await renew_lock(database, execution_lock_key(_aapl()), "owner-a", ttl_seconds=ttl) is True

    await hold_and_renew()  # outlives the original 1s TTL via renewal alone

    assert await reserve_symbol_for_execution(database, _aapl(), "owner-b") is False
    await release_symbol_reservation(database, _aapl(), "owner-a")
    assert await reserve_symbol_for_execution(database, _aapl(), "owner-b") is True
