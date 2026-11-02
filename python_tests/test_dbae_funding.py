"""DBAE funding reservations (spec §7): every buy's funding stays committed,
across symbols, until a fill is attributed or the broker confirms a terminal
state."""

import ast
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from python_tests.test_risk_engine import LIMITS, NOW, _aapl, _repositories, _seed_settlement_event
from tradepulse.broker.capabilities import AccountEvidence
from tradepulse.dbae.funding import (
    PendingFunding,
    funding_available,
    max_funding,
    pending_buy_funding,
    projected_buy_fee,
)
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Holding,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.persistence import PersistenceRepositories
from tradepulse.risk.engine import (
    FEE_PCT_PER_SIDE,
    PendingIntentRemainder,
    build_portfolio_snapshot,
    pending_intent_remainders,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

EQUITY = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
CRYPTO = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTCUSD")
OPTION_100 = AssetIdentity(
    "AAPL261120C00100000", AssetClass.OPTION, "alpaca:AAPL261120C00100000", metadata={"contract_multiplier": "100"},
)
OPTION_10 = AssetIdentity(
    "AAPL261120C00100001", AssetClass.OPTION, "alpaca:AAPL261120C00100001", metadata={"contract_multiplier": "10"},
)
OPTION_BARE = AssetIdentity("AAPL261120C00100002", AssetClass.OPTION, "alpaca:AAPL261120C00100002")

EQ_SLIP = LIMITS.slippage_limit_for(AssetClass.EQUITY)
CRYPTO_SLIP = LIMITS.slippage_limit_for(AssetClass.CRYPTO)

PENDING_STATUSES = (
    TradeIntentStatus.RISK_APPROVED, TradeIntentStatus.SUBMITTED, TradeIntentStatus.ACCEPTED,
    TradeIntentStatus.PARTIALLY_FILLED, TradeIntentStatus.SUBMISSION_UNKNOWN,
)
RELEASED_STATUSES = (
    TradeIntentStatus.PROPOSED, TradeIntentStatus.FILLED, TradeIntentStatus.REJECTED,
    TradeIntentStatus.CANCELED, TradeIntentStatus.EXPIRED, TradeIntentStatus.FAILED,
)


def _dbae_block(submitted: str, max_funding_value: Decimal | str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "original_requested_quantity": "20", "risk_approved_quantity": submitted,
        "shaped_quantity": submitted, "submitted_quantity": submitted,
    }
    if max_funding_value is not None:
        block["max_funding"] = str(max_funding_value)
    return {"dbae": block}


def _approved_block(submitted: str, price: str, asset: AssetIdentity = EQUITY,
                    slippage: Decimal | None = None) -> dict[str, Any]:
    """The dbae block the gateway freezes at RISK_APPROVED: max_funding on
    the submitted quantity at the approving profile's slippage, with the
    inputs it was computed from (DBAE-146-01, I1)."""
    slip = LIMITS.slippage_limit_for(asset.asset_class) if slippage is None else slippage
    snapshot = _dbae_block(submitted, max_funding(Decimal(submitted), Decimal(price), asset, slip))
    snapshot["dbae"].update({
        "reference_price": price, "contract_multiplier": str(contract_multiplier_of(asset)),
        "slippage_pct": str(slip), "fee_pct": str(FEE_PCT_PER_SIDE.get(asset.asset_class, Decimal(0))),
    })
    return snapshot


async def _seed_buy(
    repositories: PersistenceRepositories, intent_id: str, *, quantity: str, price: str | None,
    status: TradeIntentStatus = TradeIntentStatus.ACCEPTED, asset: AssetIdentity = EQUITY,
    side: Side = Side.BUY, risk_snapshot: dict[str, Any] | None = None, sector: str | None = "Tech",
) -> TradeIntent:
    intent = TradeIntent(
        intent_id, intent_id, intent_id, asset, side, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal(quantity), reference_price=None if price is None else Decimal(price),
        risk_snapshot=(_dbae_block(quantity) if price is None else _approved_block(quantity, price, asset))
        if risk_snapshot is None else risk_snapshot,
        status=status, sector=sector,
    )
    await repositories.trade_intents.create_once(intent_id, intent, status=status.value, unique_value=intent_id)
    return intent


def _reserve(qty: Decimal, price: Decimal, slip: Decimal, fee_pct: Decimal = Decimal(0), cm: Decimal = Decimal(1)) -> Decimal:
    notional = qty * price * cm
    return notional * (1 + slip / 100) + notional * fee_pct / 100


def _account(**overrides: Any) -> AccountEvidence:
    base = AccountEvidence(
        status="ACTIVE", trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
        suspend_trade=False, closing_transactions_only=False, fractional_trading=True,
        equity=Decimal(100), cash=Decimal(100), buying_power=Decimal(400),
        non_marginable_buying_power=Decimal(90), account_multiplier=Decimal(4), crypto_status="ACTIVE",
        options_trading_level=None, received_at=datetime(2026, 10, 30, 15, 0, tzinfo=UTC),
    )
    return replace(base, **overrides)


# --- the extracted RISK-091-01 helper ---------------------------------------


async def test_snapshot_unchanged_after_extraction(tmp_path) -> None:
    """A mixed book through build_portfolio_snapshot gives exactly the
    figures the pre-extraction loop gave (values computed by hand from
    that loop), and the helper exposes the same buckets."""
    repositories = await _repositories(tmp_path)
    await repositories.holdings.create_once(
        asset_identity_key(_aapl()),
        Holding(asset=_aapl(), quantity=Decimal(4), average_price=Decimal(100), updated_at=NOW, sector="Tech"),
    )
    # Partially filled: 4 settled (projected), 3 attributed but unsettled, 3 open.
    await _seed_buy(repositories, "ti-p", quantity="10", price="100", status=TradeIntentStatus.PARTIALLY_FILLED,
                    asset=_aapl())
    await _seed_settlement_event(repositories, settlement_event_id="se-p1", trade_intent_id="ti-p",
                                 quantity=Decimal(4), price=Decimal(100), holding_projected=True)
    await _seed_settlement_event(repositories, settlement_event_id="se-p2", trade_intent_id="ti-p",
                                 quantity=Decimal(3), price=Decimal(110), holding_projected=False)
    # Risk-approved, nothing sent: full notional, its own sector.
    await _seed_buy(repositories, "ti-r", quantity="2", price="50", status=TradeIntentStatus.RISK_APPROVED,
                    sector="Energy")
    # Pending without a reference price: no pending notional (today's rule).
    await _seed_buy(repositories, "ti-u", quantity="5", price=None, status=TradeIntentStatus.SUBMITTED)
    # A pending sell still counts in the snapshot exactly as before.
    await _seed_buy(repositories, "ti-s", quantity="1", price="100", status=TradeIntentStatus.ACCEPTED, side=Side.SELL)
    # Terminal intent with an unsettled fill: caught by the global term.
    await _seed_buy(repositories, "ti-f", quantity="2", price="100", status=TradeIntentStatus.FILLED)
    await _seed_settlement_event(repositories, settlement_event_id="se-f", trade_intent_id="ti-f",
                                 quantity=Decimal(2), price=Decimal(101), holding_projected=False)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal(0), now=NOW)

    # holdings 400; ti-p open 3*100=300 + unsettled 3*110=330; ti-r 100; ti-s 100; ti-f 202.
    assert snapshot.holdings_value == Decimal(1432)
    assert snapshot.sector_exposure == {"Tech": Decimal(1332), "Energy": Decimal(100)}
    assert snapshot.outstanding_orders == 3

    remainders = {r.intent.trade_intent_id: r for r in await pending_intent_remainders(repositories)}
    assert set(remainders) == {"ti-p", "ti-r", "ti-u", "ti-s"}
    assert remainders["ti-p"].remaining_qty == Decimal(3)
    assert remainders["ti-p"].unsettled_event_ids == frozenset({"se-p2"})
    assert remainders["ti-r"].remaining_qty == Decimal(2)
    assert remainders["ti-r"].unsettled_event_ids == frozenset()
    assert isinstance(remainders["ti-u"], PendingIntentRemainder)


def test_fee_table_is_public() -> None:
    assert FEE_PCT_PER_SIDE == {AssetClass.CRYPTO: Decimal("0.25")}
    assert projected_buy_fee(AssetClass.CRYPTO, Decimal(10)) == Decimal("0.025")
    assert projected_buy_fee(AssetClass.EQUITY, Decimal(10)) == Decimal(0)
    assert projected_buy_fee(AssetClass.OPTION, Decimal(10)) == Decimal(0)


# --- §7.2 max_funding ---------------------------------------------------------


def test_contract_vs_account_multiplier() -> None:
    """An account_multiplier of 4 never enters a funding figure: a $10
    equity order reserves $10 plus its allowance, not $40. The crypto fee is
    0.25% of the order notional (quantity × price × contract multiplier),
    not of the slippage-buffered figure."""
    account = _account(account_multiplier=Decimal(4))
    assert account.account_multiplier == Decimal(4)
    assert max_funding(Decimal(1), Decimal(10), EQUITY, Decimal("0.5")) == Decimal("10.05")
    assert max_funding(Decimal(1), Decimal(10), CRYPTO, Decimal("0.5")) == Decimal("10.075")
    # Fee on the buffered notional would have been 10.05 + 0.025125.
    assert max_funding(Decimal(1), Decimal(10), CRYPTO, Decimal("0.5")) != Decimal("10.075125")
    assert funding_available(account, AssetClass.EQUITY, Decimal("10.05")) == Decimal("89.95")


def test_option_multiplier_from_contract() -> None:
    assert max_funding(Decimal(1), Decimal(2), OPTION_100, Decimal(0)) == Decimal(200)
    assert max_funding(Decimal(1), Decimal(2), OPTION_10, Decimal(0)) == Decimal(20)
    assert max_funding(Decimal(2), Decimal(2), OPTION_100, Decimal(1)) == Decimal(404)
    with pytest.raises(ValueError, match="option contract multiplier missing"):
        max_funding(Decimal(1), Decimal(2), OPTION_BARE, Decimal(0))


# --- §7.4 pending_buy_funding ------------------------------------------------


async def test_partial_fill_reserves_only_remainder(tmp_path) -> None:
    """A $10 order with $6 attributed reserves 4 × (1 + slippage), plus the
    projected fee on the $4 still unattributed."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-eq", quantity="10", price="1", status=TradeIntentStatus.PARTIALLY_FILLED)
    await _seed_settlement_event(repositories, settlement_event_id="se-eq", trade_intent_id="ti-eq",
                                 quantity=Decimal(6), price=Decimal(1), holding_projected=False)
    await _seed_buy(repositories, "ti-c", quantity="10", price="1", status=TradeIntentStatus.PARTIALLY_FILLED,
                    asset=CRYPTO)
    await _seed_settlement_event(repositories, settlement_event_id="se-c", trade_intent_id="ti-c",
                                 quantity=Decimal(6), price=Decimal(1), holding_projected=True)

    pending = await pending_buy_funding(repositories, LIMITS)

    expected = _reserve(Decimal(4), Decimal(1), EQ_SLIP) + _reserve(Decimal(4), Decimal(1), CRYPTO_SLIP, Decimal("0.25"))
    assert pending == PendingFunding(total=expected, mismatches=(), unpriced=())


async def test_ambiguous_buy_stays_fully_reserved(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-x", quantity="10", price="3", status=TradeIntentStatus.SUBMISSION_UNKNOWN)

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending.total == max_funding(Decimal(10), Decimal(3), EQUITY, EQ_SLIP)


@pytest.mark.parametrize("status", PENDING_STATUSES + RELEASED_STATUSES)
async def test_cancel_releases_only_after_terminal(tmp_path, status: TradeIntentStatus) -> None:
    """A buy is released only by a terminal (or never-approved) status; a
    cancel that is merely requested leaves it ACCEPTED and fully reserved.
    A canceled partial fill reserves nothing: its attributed part is
    already in cash and holdings."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-1", quantity="10", price="2", status=status)
    await _seed_settlement_event(repositories, settlement_event_id="se-1", trade_intent_id="ti-1",
                                 quantity=Decimal(3), price=Decimal(2), holding_projected=False)

    pending = await pending_buy_funding(repositories, LIMITS)

    if status in PENDING_STATUSES:
        assert pending.total == _reserve(Decimal(7), Decimal(2), EQ_SLIP)
    else:
        assert pending.total == Decimal(0)


@pytest.mark.parametrize("recorded_fee", [Decimal(0), Decimal("0.05"), Decimal("7.50")])
async def test_fee_correction_counted_once(tmp_path, recorded_fee: Decimal) -> None:
    """The actual fee recorded on an attributed portion (including a
    corrected one) never adds to P, and no estimate is kept for that
    portion: P's fee term depends only on the unattributed remainder."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-c", quantity="10", price="100", status=TradeIntentStatus.PARTIALLY_FILLED,
                    asset=CRYPTO)
    await _seed_settlement_event(repositories, settlement_event_id="se-c", trade_intent_id="ti-c",
                                 quantity=Decimal(4), price=Decimal(100), holding_projected=False)
    event_row = await repositories.settlements.get("se-c")
    assert event_row is not None
    from tradepulse.persistence import hydrate
    event = hydrate("settlements", event_row["payload"])
    await repositories.settlements.update("se-c", replace(event, fees=recorded_fee), status=event_row["status"])

    pending = await pending_buy_funding(repositories, LIMITS)

    remainder_notional = Decimal(6) * Decimal(100)
    assert pending.total == remainder_notional * (1 + CRYPTO_SLIP / 100) + projected_buy_fee(
        AssetClass.CRYPTO, remainder_notional,
    )


async def test_sells_never_reserve(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-s", quantity="10", price="5", side=Side.SELL)

    assert (await pending_buy_funding(repositories, LIMITS)).total == Decimal(0)


async def test_buys_across_symbols_sum(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-a", quantity="2", price="5")
    await _seed_buy(repositories, "ti-b", quantity="1", price="7", asset=AssetIdentity(
        "MSFT", AssetClass.EQUITY, "alpaca:MSFT"))

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending.total == _reserve(Decimal(2), Decimal(5), EQ_SLIP) + _reserve(Decimal(1), Decimal(7), EQ_SLIP)


async def test_identity_mismatch_reserves_larger(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-m", quantity="7", price="1", risk_snapshot=_approved_block("9", "1"))
    await _seed_buy(repositories, "ti-n", quantity="9", price="1", risk_snapshot=_approved_block("7", "1"))

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending.total == 2 * _reserve(Decimal(9), Decimal(1), EQ_SLIP)
    assert pending.mismatches == ("ti-m", "ti-n")
    assert pending.unpriced == ()


async def test_pre_dbae_intent_is_unpriced(tmp_path) -> None:
    """DBAE-146-01: a pending buy with no dbae block has no frozen
    authorization, so its commitment cannot be reconstructed: unpriced."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-old", quantity="7", price="2",
                    risk_snapshot={"requested_quantity": "50", "approved_quantity": "50"})

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending == PendingFunding(total=Decimal(0), mismatches=(), unpriced=("ti-old",))


async def test_fully_attributed_pre_dbae_intent_is_not_flagged(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-old", quantity="5", price="2", status=TradeIntentStatus.PARTIALLY_FILLED,
                    risk_snapshot={"requested_quantity": "5"})
    await _seed_settlement_event(repositories, settlement_event_id="se-o", trade_intent_id="ti-old",
                                 quantity=Decimal(5), price=Decimal(2), holding_projected=False)

    assert await pending_buy_funding(repositories, LIMITS) == PendingFunding(Decimal(0), (), ())


# --- DBAE-146-01: P from the frozen authorization -----------------------------

FROZEN_SLIP = Decimal("1.5")  # the approving profile's equity slippage (aggressive)
LOWER = replace(LIMITS, slippage_limit_pct=Decimal("0.1"))
HIGHER = replace(LIMITS, slippage_limit_pct=Decimal(3))


@pytest.mark.parametrize("status", [
    TradeIntentStatus.RISK_APPROVED, TradeIntentStatus.ACCEPTED, TradeIntentStatus.PARTIALLY_FILLED,
    TradeIntentStatus.SUBMISSION_UNKNOWN,
])
@pytest.mark.parametrize(("limits", "slip"), [(LOWER, FROZEN_SLIP), (HIGHER, Decimal(3))],
                         ids=["profile_lowered", "profile_raised"])
async def test_profile_change_never_reduces_pending_funding(tmp_path, status: TradeIntentStatus,
                                                            limits: Any, slip: Decimal) -> None:
    """A $10 order frozen at 1.5% slippage. Lowering the profile's slippage
    afterwards never shrinks the commitment (the frozen rate stays);
    raising it raises P (max of frozen and current)."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-f", quantity="10", price="1", status=status,
                    risk_snapshot=_approved_block("10", "1", slippage=FROZEN_SLIP))
    attributed = Decimal(6) if status == TradeIntentStatus.PARTIALLY_FILLED else Decimal(0)
    if attributed:
        await _seed_settlement_event(repositories, settlement_event_id="se-f", trade_intent_id="ti-f",
                                     quantity=attributed, price=Decimal(1), holding_projected=False)

    pending = await pending_buy_funding(repositories, limits)

    assert pending == PendingFunding(_reserve(10 - attributed, Decimal(1), slip), (), ())
    assert pending.total >= _reserve(10 - attributed, Decimal(1), FROZEN_SLIP)


async def test_definitive_fill_reduces_to_frozen_remainder(tmp_path) -> None:
    """Amendment (a), (b): $10 with $6 definitively filled reserves the
    unfilled $4 at the frozen 1.5% (crypto: plus the fee on the $4), not
    $10; with the profile lowered afterwards, still never below that."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-eq", quantity="10", price="1", status=TradeIntentStatus.PARTIALLY_FILLED,
                    risk_snapshot=_approved_block("10", "1", slippage=FROZEN_SLIP))
    await _seed_settlement_event(repositories, settlement_event_id="se-eq", trade_intent_id="ti-eq",
                                 quantity=Decimal(6), price=Decimal(1), holding_projected=False)
    await _seed_buy(repositories, "ti-c", quantity="10", price="1", status=TradeIntentStatus.PARTIALLY_FILLED,
                    asset=CRYPTO, risk_snapshot=_approved_block("10", "1", CRYPTO, slippage=FROZEN_SLIP))
    await _seed_settlement_event(repositories, settlement_event_id="se-c", trade_intent_id="ti-c",
                                 quantity=Decimal(6), price=Decimal(1), holding_projected=True)
    remainder = _reserve(Decimal(4), Decimal(1), FROZEN_SLIP) + _reserve(Decimal(4), Decimal(1), FROZEN_SLIP,
                                                                          Decimal("0.25"))
    lowered = replace(LIMITS, slippage_limit_pct=Decimal(0))

    assert (await pending_buy_funding(repositories, LIMITS)).total == remainder
    assert (await pending_buy_funding(repositories, lowered)).total == remainder
    assert remainder < _reserve(Decimal(10), Decimal(1), FROZEN_SLIP)


@pytest.mark.parametrize("status", [TradeIntentStatus.CANCELED, TradeIntentStatus.EXPIRED, TradeIntentStatus.REJECTED])
async def test_terminal_after_partial_fill_reserves_nothing(tmp_path, status: TradeIntentStatus) -> None:
    """Amendment (c): a broker-confirmed terminal state after a partial fill
    leaves nothing reserved, whatever the profile does."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-t", quantity="10", price="1", status=status,
                    risk_snapshot=_approved_block("10", "1", slippage=FROZEN_SLIP))
    await _seed_settlement_event(repositories, settlement_event_id="se-t", trade_intent_id="ti-t",
                                 quantity=Decimal(6), price=Decimal(1), holding_projected=False)

    for limits in (LOWER, LIMITS, HIGHER):
        assert await pending_buy_funding(repositories, limits) == PendingFunding(Decimal(0), (), ())


async def test_submission_unknown_keeps_full_frozen_commitment(tmp_path) -> None:
    """Amendment (d): an unresolved submission keeps the full frozen
    commitment, even with the profile's slippage lowered."""
    repositories = await _repositories(tmp_path)
    block = _approved_block("10", "3", slippage=FROZEN_SLIP)
    await _seed_buy(repositories, "ti-x", quantity="10", price="3", status=TradeIntentStatus.SUBMISSION_UNKNOWN,
                    risk_snapshot=block)

    pending = await pending_buy_funding(repositories, LOWER)

    assert pending.total == Decimal(block["dbae"]["max_funding"]) == _reserve(Decimal(10), Decimal(3), FROZEN_SLIP)


@pytest.mark.parametrize("frozen", [
    None, "abc", "NaN", "Infinity", "-Infinity", "0", "-1", True, "",
    "9.99",  # below the recomputed max_funding
    "20.01",  # far above it
])
async def test_invalid_frozen_max_funding_is_unpriced(tmp_path, frozen: object) -> None:
    """A dbae block whose max_funding is missing, unparsable, non-finite,
    non-positive or not the recomputation of its frozen inputs cannot be
    reconstructed: the buy is unpriced (DBAE_FUNDING_UNVERIFIED)."""
    repositories = await _repositories(tmp_path)
    block = _approved_block("10", "1")
    del block["dbae"]["max_funding"]
    if frozen is not None:
        block["dbae"]["max_funding"] = frozen
    await _seed_buy(repositories, "ti-bad", quantity="10", price="1", risk_snapshot=block)
    await _seed_buy(repositories, "ti-ok", quantity="1", price="2")

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending.unpriced == ("ti-bad",)
    assert pending.total == _reserve(Decimal(1), Decimal(2), EQ_SLIP)


# --- DBAE-146-01 (I1): the frozen record reconciles exactly --------------------
#
# The block freezes max_funding with every input it was computed from. It is
# priced only when the recomputation from those inputs equals max_funding
# exactly and the price and multiplier are the intent's own.

FROZEN_RATE_FIELDS = ("max_funding", "reference_price", "contract_multiplier", "slippage_pct", "fee_pct")


async def _priced_alone(tmp_path, block: dict[str, Any], *, asset: AssetIdentity = EQUITY, quantity: str = "10",
                        price: str = "1", limits: Any = LIMITS) -> PendingFunding:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-r", quantity=quantity, price=price, asset=asset, risk_snapshot=block)
    return await pending_buy_funding(repositories, limits)


async def test_exactly_reconciled_record_is_priced(tmp_path) -> None:
    block = _approved_block("10", "3.37", slippage=FROZEN_SLIP)

    pending = await _priced_alone(tmp_path, block, price="3.37", limits=LOWER)

    assert pending == PendingFunding(Decimal(block["dbae"]["max_funding"]), (), ())
    assert pending.total == _reserve(Decimal(10), Decimal("3.37"), FROZEN_SLIP)


async def test_block_without_frozen_rates_is_unpriced(tmp_path) -> None:
    """A dbae block from before I1 (submitted_quantity and max_funding only)
    cannot be reconciled: unpriced, like a pre-DBAE buy."""
    legacy = _dbae_block("10", max_funding(Decimal(10), Decimal(1), EQUITY, EQ_SLIP))

    assert (await _priced_alone(tmp_path, legacy)).unpriced == ("ti-r",)


@pytest.mark.parametrize("field", FROZEN_RATE_FIELDS)
async def test_missing_frozen_field_is_unpriced(tmp_path, field: str) -> None:
    block = _approved_block("10", "1")
    del block["dbae"][field]

    assert (await _priced_alone(tmp_path, block)) == PendingFunding(Decimal(0), (), ("ti-r",))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN", "abc", "", True])
@pytest.mark.parametrize("field", FROZEN_RATE_FIELDS)
async def test_non_finite_frozen_field_is_unpriced(tmp_path, field: str, value: object) -> None:
    block = _approved_block("10", "1")
    block["dbae"][field] = value

    assert (await _priced_alone(tmp_path, block)) == PendingFunding(Decimal(0), (), ("ti-r",))


@pytest.mark.parametrize("field", FROZEN_RATE_FIELDS)
async def test_non_positive_frozen_field_is_unpriced(tmp_path, field: str) -> None:
    """Zero and negative values are unpriced, except a zero fee_pct (the
    equity and option buy-side fee) or slippage_pct (a zero allowance):
    test_zero_frozen_slippage_reconciles."""
    for value in (["-1", "-0.01"] if field in ("fee_pct", "slippage_pct") else ["0", "-1", "-0.01"]):
        block = _approved_block("10", "1", CRYPTO)
        block["dbae"][field] = value
        assert (await _priced_alone(tmp_path / value, block, asset=CRYPTO)) == PendingFunding(
            Decimal(0), (), ("ti-r",)), value


@pytest.mark.parametrize("asset", [EQUITY, CRYPTO])
async def test_zero_frozen_slippage_reconciles(tmp_path, asset: AssetIdentity) -> None:
    """A zero slippage allowance is a valid configuration: a record frozen at
    slippage_pct 0 that reconciles exactly is priced; at -0.01 it is not."""
    block = _approved_block("10", "2", asset, slippage=Decimal(0))
    zero = replace(LIMITS, slippage_limit_pct=Decimal(0))

    pending = await _priced_alone(tmp_path / "zero", block, asset=asset, price="2", limits=zero)

    assert block["dbae"]["slippage_pct"] == "0"
    assert pending == PendingFunding(Decimal(block["dbae"]["max_funding"]), (), ())
    block["dbae"]["slippage_pct"] = "-0.01"
    assert (await _priced_alone(tmp_path / "negative", block, asset=asset, price="2", limits=zero)) == PendingFunding(
        Decimal(0), (), ("ti-r",))


@pytest.mark.parametrize("delta", [Decimal("0.01"), Decimal("-0.01")])
@pytest.mark.parametrize("field", ["max_funding", "slippage_pct", "fee_pct"])
async def test_recompute_mismatch_is_unpriced(tmp_path, field: str, delta: Decimal) -> None:
    """Zero tolerance: max_funding, or a rate it was computed from, moved by
    0.01 no longer reconciles. Crypto, so the fee term is non-zero."""
    block = _approved_block("10", "1", CRYPTO)
    block["dbae"][field] = str(Decimal(block["dbae"][field]) + delta)

    assert (await _priced_alone(tmp_path, block, asset=CRYPTO)) == PendingFunding(Decimal(0), (), ("ti-r",))


async def test_recompute_mismatch_by_one_millionth_is_unpriced(tmp_path) -> None:
    block = _approved_block("10", "1")
    block["dbae"]["max_funding"] = str(Decimal(block["dbae"]["max_funding"]) + Decimal("0.000001"))

    assert (await _priced_alone(tmp_path, block)).unpriced == ("ti-r",)


@pytest.mark.parametrize("delta", [Decimal("0.01"), Decimal("-0.01")])
async def test_reference_price_mismatch_is_unpriced(tmp_path, delta: Decimal) -> None:
    """A frozen reference_price that is not the intent's, with a max_funding
    recomputed consistently from it: the record reconciles with itself but
    not with the intent, so it is unpriced."""
    block = _approved_block("10", str(Decimal(1) + delta))

    assert (await _priced_alone(tmp_path, block, price="1")) == PendingFunding(Decimal(0), (), ("ti-r",))


@pytest.mark.parametrize("frozen_asset", [OPTION_10, EQUITY], ids=["multiplier_10", "multiplier_1"])
async def test_contract_multiplier_mismatch_is_unpriced(tmp_path, frozen_asset: AssetIdentity) -> None:
    """A 100-multiplier option whose block was frozen, self-consistently, at
    another multiplier: unpriced."""
    block = _approved_block("2", "1.5", OPTION_100)
    consistent = _approved_block("2", "1.5", frozen_asset, slippage=Decimal(block["dbae"]["slippage_pct"]))
    block["dbae"].update({key: consistent["dbae"][key] for key in ("contract_multiplier", "max_funding")})

    assert (await _priced_alone(tmp_path, block, asset=OPTION_100, quantity="2", price="1.5")) == PendingFunding(
        Decimal(0), (), ("ti-r",))


async def test_option_with_options_slippage_reconciles(tmp_path) -> None:
    """An option, multiplier 100, frozen at the options slippage allowance."""
    options_slip = LIMITS.slippage_limit_for(AssetClass.OPTION)
    assert options_slip != EQ_SLIP
    block = _approved_block("2", "1.5", OPTION_100)

    pending = await _priced_alone(tmp_path, block, asset=OPTION_100, quantity="2", price="1.5")

    assert block["dbae"]["slippage_pct"] == str(options_slip)
    assert pending == PendingFunding(_reserve(Decimal(2), Decimal("1.5"), options_slip, cm=Decimal(100)), (), ())


async def test_crypto_with_fee_reconciles(tmp_path) -> None:
    block = _approved_block("0.37", "61234.57", CRYPTO)

    pending = await _priced_alone(tmp_path, block, asset=CRYPTO, quantity="0.37", price="61234.57")

    assert block["dbae"]["fee_pct"] == "0.25"
    assert pending == PendingFunding(
        _reserve(Decimal("0.37"), Decimal("61234.57"), CRYPTO_SLIP, Decimal("0.25")), (), ())


@pytest.mark.parametrize(("asset", "slippage"), [
    (OPTION_100, None),  # 101.25× quantity × price
    (EQUITY, Decimal(150)),  # a custom profile's 150% slippage: 2.5× the notional
], ids=["option_multiplier_100", "custom_slippage_150"])
async def test_legitimately_large_ratio_is_priced(tmp_path, asset: AssetIdentity, slippage: Decimal | None) -> None:
    """No fixed ratio bounds the record: a large max_funding that reconciles
    exactly with its frozen inputs is priced (the old 2× cutoff rejected the
    custom-slippage record outright)."""
    block = _approved_block("3", "4", asset, slippage=slippage)
    limits = LIMITS if slippage is None else replace(LIMITS, slippage_limit_pct=slippage)

    pending = await _priced_alone(tmp_path, block, asset=asset, quantity="3", price="4", limits=limits)

    assert pending == PendingFunding(Decimal(block["dbae"]["max_funding"]), (), ())
    assert pending.total > 2 * Decimal(3) * Decimal(4)


@pytest.mark.parametrize("risk_snapshot", [
    {"dbae": {"submitted_quantity": "abc"}},
    {"dbae": {"submitted_quantity": "NaN"}},
    {"dbae": {"submitted_quantity": "Infinity"}},
    {"dbae": {"submitted_quantity": "0"}},
    {"dbae": {"submitted_quantity": "-1"}},
    {"dbae": {"submitted_quantity": None}},
    {"dbae": {}},
    {"dbae": "7"},
])
async def test_unparsable_dbae_quantity_is_unpriced(tmp_path, risk_snapshot: dict[str, Any]) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-bad", quantity="7", price="2", risk_snapshot=risk_snapshot)
    await _seed_buy(repositories, "ti-ok", quantity="1", price="2")

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending.unpriced == ("ti-bad",)
    assert pending.total == _reserve(Decimal(1), Decimal(2), EQ_SLIP)


async def test_missing_reference_price_is_unpriced_not_skipped(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-np", quantity="7", price=None)

    pending = await pending_buy_funding(repositories, LIMITS)

    assert pending == PendingFunding(total=Decimal(0), mismatches=(), unpriced=("ti-np",))


async def test_fully_attributed_unpriced_buy_is_not_flagged(tmp_path) -> None:
    """Nothing left to fund means nothing left to price."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, "ti-done", quantity="5", price=None, status=TradeIntentStatus.PARTIALLY_FILLED)
    await _seed_settlement_event(repositories, settlement_event_id="se-d", trade_intent_id="ti-done",
                                 quantity=Decimal(5), price=Decimal(1), holding_projected=False)

    assert await pending_buy_funding(repositories, LIMITS) == PendingFunding(Decimal(0), (), ())


# --- §7.5 funding_available ----------------------------------------------------


def test_equity_and_option_use_buying_power() -> None:
    account = _account(cash=Decimal(100), buying_power=Decimal(60))
    assert funding_available(account, AssetClass.EQUITY, Decimal(10)) == Decimal(50)
    assert funding_available(account, AssetClass.OPTION, Decimal(10)) == Decimal(50)
    account = _account(cash=Decimal(40), buying_power=Decimal(160))
    assert funding_available(account, AssetClass.EQUITY, Decimal(10)) == Decimal(30)


def test_crypto_uses_non_marginable_buying_power() -> None:
    account = _account(cash=Decimal(100), buying_power=Decimal(400), non_marginable_buying_power=Decimal(70))
    assert funding_available(account, AssetClass.CRYPTO, Decimal(10)) == Decimal(60)
    assert funding_available(_account(non_marginable_buying_power=None), AssetClass.CRYPTO, Decimal(0)) is None
    # Equity ignores a missing non-marginable figure.
    assert funding_available(_account(non_marginable_buying_power=None), AssetClass.EQUITY, Decimal(0)) == Decimal(100)


def test_result_at_or_below_zero_is_returned() -> None:
    """Zero or negative availability is a number (INSUFFICIENT_CASH
    downstream), not an unverified input."""
    account = _account(cash=Decimal(10), buying_power=Decimal(40))
    assert funding_available(account, AssetClass.EQUITY, Decimal(10)) == Decimal(0)
    assert funding_available(account, AssetClass.EQUITY, Decimal(15)) == Decimal(-5)


@pytest.mark.parametrize(("overrides", "asset_class", "pending"), [
    ({"cash": None}, AssetClass.EQUITY, Decimal(0)),
    ({"buying_power": None}, AssetClass.EQUITY, Decimal(0)),
    ({"buying_power": None}, AssetClass.OPTION, Decimal(0)),
    ({"cash": None}, AssetClass.CRYPTO, Decimal(0)),
    ({"cash": Decimal("-0.01")}, AssetClass.EQUITY, Decimal(0)),
    ({"buying_power": Decimal(-1)}, AssetClass.EQUITY, Decimal(0)),
    ({"non_marginable_buying_power": Decimal(-1)}, AssetClass.CRYPTO, Decimal(0)),
    ({}, AssetClass.EQUITY, Decimal(-1)),
])
def test_missing_or_negative_input_is_none(overrides: dict[str, Any], asset_class: AssetClass, pending: Decimal) -> None:
    assert funding_available(_account(**overrides), asset_class, pending) is None


# --- multiplier discipline ------------------------------------------------------


def _account_multiplier_readers(path: Path) -> list[str]:
    """Names of the functions in ``path`` that read ``.account_multiplier``
    (``<module>`` for a read outside any function)."""
    tree = ast.parse(path.read_text())
    readers: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if (isinstance(child, ast.Attribute) and child.attr == "account_multiplier"
                    and isinstance(child.ctx, ast.Load)):
                readers.append(scope)
            visit(child, scope)

    visit(tree, "<module>")
    return readers


def test_account_multiplier_read_only_in_account_type_check() -> None:
    """§7.2: the account's buying-power multiplier is read only by the §5.3
    account-type check, never by funding, sizing or gateway arithmetic.
    (gateway.py has no reader until Task 7; it is scanned so one can never
    appear outside the check.)"""
    paths = sorted((REPO_ROOT / "tradepulse" / "dbae").glob("*.py")) + [
        REPO_ROOT / "tradepulse" / "execution" / "gateway.py",
    ]
    assert REPO_ROOT / "tradepulse" / "dbae" / "funding.py" in paths
    readers = {
        (path.relative_to(REPO_ROOT).as_posix(), name)
        for path in paths
        for name in _account_multiplier_readers(path)
    }
    assert readers == {("tradepulse/dbae/policy.py", "_core_account_reasons")}
