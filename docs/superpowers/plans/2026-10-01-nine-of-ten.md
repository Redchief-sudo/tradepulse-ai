# Every Area to 9/10 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the fresh Rev.114 audit so each TradePulse area rates at least 9/10.

**Architecture:** Three shippable revisions on the existing worktree branch:
- **Rev.115:** position protection and execution liveness (F1, F5, F6, monitor cadence).
- **Rev.116:** risk and control plane (F4, F2).
- **Rev.117:** verification, operations and consistency (F8, F7, service unit and runbook).

Broker-side protective stops (F3) change exit semantics and get their own design spec (Phase 4). This plan ends by writing that spec, not by implementing it.

**Tech Stack:** Python 3.12, asyncio, SQLite (WAL), FastAPI/uvicorn, httpx + respx for tests, pytest-asyncio (auto mode), React/Vite frontend (vitest).

**Spec:** `docs/superpowers/specs/2026-10-01-nine-of-ten-audit.md`

## Global Constraints

- Work in `/home/damien/tradepulse-rev113` (branch `rev113-options-liquidity`), never in `~/tradepulse-ai` while `pgrep -f run_accounting_soak` matches. A protected-source change there invalidates the running soak.
- Run tests with `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest ...` from the worktree root. The worktree has no `.venv`, and `PYTHONPATH` makes the worktree's `tradepulse` win the import.
- Money is `Decimal` only. Never construct a `Decimal` from a float.
- Fail closed: a broker lookup error is "unknown", never "not found" and never "rejected".
- Protective exits must never be blocked by a session state, the market clock, a kill switch, or a new guard.
- Commits are GPG-signed. Use `git commit -F -` with a `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer. The full suite must pass before each revision commit.
- Each revision adds `docs/revNNN-<slug>.md` in the existing style (finding, change, validation).
- Keep explanatory `# noqa: BLE001 - <reason>` comments. Keep the `Decimal("2")` house style.
- Separate runtime resources while a soak runs. Never start `tradepulse run`, the dashboard, a soak, or anything that calls the live Alpaca account from the worktree, since port 8766 and the paper account belong to the running soak. Tests use `tmp_path` databases and respx/AsyncMock brokers only.
- A missing `broker_order_id` never proves an order was not submitted. A stranded intent may be closed only when all of these hold:
  1. A **successful** lookup returns Alpaca's definitive 404 for the **exact** `client_order_id` (the gateway always sets it to `trade_intent_id`).
  2. The lookup is made against the **same broker account** the intent was approved on (`risk_snapshot.broker_account_number`, recorded by Task 1).
  3. No submission is still in progress. A submission is in progress exactly while the asset's execution reservation is held and renewed, so the sweep skips any held reservation, and the 120 s grace period exceeds the 45 s reservation TTL, so a crashed submitter's lease has expired.

  The sweep holds and renews that reservation for its whole decision. An adopted order must match on `client_order_id`, symbol and side.

## Review Focus

1. **The stranded-intent sweep racing a live submission.** If the gateway holds the asset's execution reservation, the sweep must skip that intent (Task 2 test `test_sweep_skips_asset_with_live_reservation`).
2. **An Alpaca client-order lookup that errors (5xx or timeout).** The intent must stay as it is, with a drift record, and must never be marked rejected (Task 2 test `test_sweep_lookup_error_leaves_intent_unchanged`).
3. **Opening-inventory positions in a bound generation.** These must not raise unmanaged-position alerts, while the same symbol at a different quantity must (Task 3 tests).
4. **Dashboard GET polling and curl usage.** GETs must keep working without the control header. A mutation with no `Origin` header (curl, tests) is allowed when the header is present (Task 6 tests).
5. **The cumulative cap on options.** Held notional must include the x100 multiplier, which it does because it comes from the broker `market_value` (Task 5 test `test_held_option_notional_counts_toward_position_cap`).
6. **A restart with integrity holds present.** The stranded-intent sweep must never alter or clear an integrity hold or the session latch (Task 2 test `test_sweep_preserves_integrity_holds_and_session_latch`).
7. **Soak evidence vs runtime cadence.** The soak's lane-continuity criterion must use the same interval as the runtime lane, and a monitor stall over 120 s must fail continuity (Task 4 tests `test_soak_lane_criteria_match_runtime_intervals`, `test_monitor_stall_over_two_minutes_breaks_continuity`).

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `tradepulse/execution/gateway.py` | Close the intent whenever execution stops after `RISK_APPROVED` without submitting; pass held notional to risk | 1, 5 |
| `tradepulse/reconciliation/coordinator.py` | New `_recover_stranded_intents` sweep, run before `_recover_inflight_orders` | 2 |
| `tradepulse/monitor/coordinator.py` | Unmanaged-position alert, `MonitorCycleSummary.unmanaged_positions`, corrected comments | 3, 4 |
| `tradepulse/cli.py` | `MONITOR_INTERVAL_SECONDS` 120 → 30 | 4 |
| `tradepulse/risk/engine.py` | `RiskEvalOptions.held_notional`; cumulative position cap | 5 |
| `tradepulse/web/app.py` | `local_control_guard` middleware (Host on every request; Origin and control header on mutations) | 6 |
| `frontend/src/api.ts` | Send `X-TradePulse-Control: 1` on POST | 6 |
| `scripts/run_accounting_soak.py` | `preflight()` and `_broker_preflight()`, run before freeze | 7 |
| `python_tests/test_execution_gateway.py`, `python_tests/test_execution_idempotency.py` | Deterministic rewrites of the two flaky tests | 8 |
| `deploy/tradepulse-run.service`, `docs/operations-runbook.md` | Always-on supervised runtime; operator checklist | 9 |
| `docs/superpowers/specs/<date>-broker-side-stops.md` | Phase 4 design spec | 10 |

---

## Phase 1: Rev.115, position protection and execution liveness

### Task 1: Never strand an intent after a pre-submission early return

**Files:**
- Modify: `tradepulse/execution/gateway.py:459-478` (crypto protective re-read) plus a new helper method on `ExecutionGateway`
- Test: `python_tests/test_execution_gateway.py` (append)

**Interfaces:**
- Produces: `ExecutionGateway._reject_before_submission(intent: TradeIntent, reason: str) -> ExecutionResult`. It persists `REJECTED` and returns `ExecutionResult("rejected", ...)`.
- Produces: `TradeIntent.risk_snapshot["broker_account_number"]`, the broker account the intent was approved on. Task 2 requires it to close a stranded intent.

- [ ] **Step 1: Write the failing test** (append to `python_tests/test_execution_gateway.py`)

```python
@respx.mock
async def test_crypto_protective_exit_position_reread_failure_closes_the_intent(tmp_path) -> None:
    """F1: a failed pre-submission position re-read must not leave a
    RISK_APPROVED intent that blocks every later exit on the asset."""
    from tradepulse.execution import has_in_flight_intent

    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account(cash="50000", equity="100000", last_equity="100000")
    position = {"symbol": "BTCUSD", "asset_class": "crypto", "qty": "1", "avg_entry_price": "60000",
                "market_value": "60000", "current_price": "60000", "unrealized_pl": "0"}
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(side_effect=[
        httpx.Response(200, json=[position]),
        httpx.Response(503, json={"message": "unavailable"}),
    ])
    respx.get("https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes").mock(
        return_value=httpx.Response(200, json={"quotes": {"BTC/USD": {"bp": 60000.0, "ap": 60010.0, "t": QUOTE_TS}}}))
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    result = await gateway.execute_intent(ExecutionRequest(asset=_btc(), side=Side.SELL, requested_quantity=Decimal("1"),
                                                           strategy="position_monitor", decision_id="exit-1"))
    await broker.aclose()

    assert result.status == "rejected"
    assert any(r.startswith("BROKER_POSITIONS_UNAVAILABLE") for r in result.reasons)
    assert order_route.call_count == 0
    intent = (await repositories.trade_intents.list_all())[0]["payload"]
    assert intent["status"] == "rejected"
    assert "broker_account_number" in intent["risk_snapshot"]
    assert await has_in_flight_intent(repositories, _btc()) is False
```

- [ ] **Step 2: Run it and verify it fails**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_execution_gateway.py -k reread_failure_closes -q`
Expected: FAIL, `assert 'skipped' == 'rejected'`

- [ ] **Step 3: Implement**

Add this method to `ExecutionGateway`, directly above `_recover_unknown_submission`:

```python
    async def _reject_before_submission(self, intent: TradeIntent, reason: str) -> ExecutionResult:
        """No order was placed. Close the intent so has_in_flight_intent never
        treats it as in flight -- a stranded RISK_APPROVED intent would block
        every later order on the asset, protective exits included."""
        rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
        await self._repositories.trade_intents.update(intent.trade_intent_id, rejected, status=rejected.status.value)
        return ExecutionResult("rejected", intent.trade_intent_id, [reason], Decimal("0"), None)
```

Replace the two early returns in the crypto protective re-read:

```python
                    if len(matched) != 1 or risk.approved_quantity > abs(matched[0].qty):
                        return await self._reject_before_submission(approved, "BROKER_EXIT_QUANTITY_CHANGED")
                    held_quantity = matched[0].qty
                except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError) as exc:
                    return await self._reject_before_submission(approved, f"BROKER_POSITIONS_UNAVAILABLE: {exc}")
```

In the `risk_snapshot = {...}` dict, directly after the `"max_hold_days": ...` entry, add:

```python
                # The account this intent was approved on. Stranded-intent
                # recovery (reconciliation) may only close the intent after a
                # definitive not-found from this same account.
                "broker_account_number": account.account_number,
```

Also route the existing `AccountingEpochPending` rejection (the block that builds `rejected = replace(approved, ...)`, a few lines lower) through the helper: `return await self._reject_before_submission(approved, str(exc))`.

- [ ] **Step 4: Run the gateway tests and verify they pass**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_execution_gateway.py -q`
Expected: exit 0. If an existing test asserts `status == "skipped"` together with `BROKER_EXIT_QUANTITY_CHANGED`, update it to `"rejected"`: the old expectation encoded the defect.

- [ ] **Step 5: Commit** (message: `fix: close intents that stop before broker submission (Rev.115 part 1)`)

### Task 2: Sweep stranded pre-submission intents in reconciliation

**Files:**
- Modify: `tradepulse/reconciliation/coordinator.py`. Add the `STRANDED_INTENT_GRACE_SECONDS` constant and `_recover_stranded_intents`, call it immediately before `_recover_inflight_orders(...)` in `run_reconciliation`, and add `TradeIntentStatus` to the `tradepulse.models` import list.
- Test: `python_tests/test_stranded_intents.py` (create)

**Interfaces:**
- Consumes: `reserve_symbol_for_execution`, `release_symbol_reservation` (from `tradepulse.execution`); `broker.get_order_by_client_order_id(str) -> AlpacaOrderResponse | None` (None means a genuine 404; anything else raises).
- Produces: `_recover_stranded_intents(repositories, broker, alerts, now, lease_lost=None) -> int` (number resolved).

- [ ] **Step 1: Write the failing tests** (`python_tests/test_stranded_intents.py`)

```python
"""Rev.115 F1: resolve intents stranded between RISK_APPROVED and broker acceptance."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_settlement_engine import _no_op_alerter, _repositories
from tradepulse.execution import has_in_flight_intent, reserve_symbol_for_execution
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Side, TradeIntent, TradeIntentStatus
from tradepulse.reconciliation.coordinator import _recover_stranded_intents

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)
BTC = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")


def _broker(account_number="PA1"):
    broker = AsyncMock()
    broker.get_account.return_value = SimpleNamespace(account_number=account_number)
    return broker


def _order(**overrides):
    fields = {"broker_order_id": "order-9", "symbol": "BTC/USD", "side": Side.SELL, "raw": {"client_order_id": "ti-1"}}
    return SimpleNamespace(**{**fields, **overrides})


async def _stranded(repositories, status=TradeIntentStatus.RISK_APPROVED, age=timedelta(minutes=10), account="PA1"):
    snapshot = {} if account is None else {"broker_account_number": account}
    intent = TradeIntent("ti-1", "idem-1", "corr-1", BTC, Side.SELL, ExecutionMode.PAPER, "position_monitor",
                         NOW - age, requested_quantity=Decimal(1), status=status, risk_snapshot=snapshot)
    await repositories.trade_intents.create_once("ti-1", intent, status=status.value, unique_value="idem-1")


async def _status(repositories):
    return (await repositories.trade_intents.get("ti-1"))["payload"]["status"]


async def test_sweep_closes_intent_alpaca_never_received(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert await _status(repositories) == "rejected"
    assert await has_in_flight_intent(repositories, BTC) is False
    broker.get_order_by_client_order_id.assert_awaited_once_with("ti-1")


async def test_sweep_adopts_order_alpaca_did_receive(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = _order()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    payload = (await repositories.trade_intents.get("ti-1"))["payload"]
    assert (payload["status"], payload["broker_order_id"]) == ("accepted", "order-9")


async def test_sweep_refuses_close_when_account_identity_is_unproven(tmp_path):
    for recorded, current in ((None, "PA1"), ("PA1", "PA2")):
        repositories = await _repositories(tmp_path / f"{recorded}-{current}")
        await _stranded(repositories, account=recorded)
        broker = _broker(account_number=current)
        broker.get_order_by_client_order_id.return_value = None
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert await _status(repositories) == "risk_approved"
        broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_refuses_to_adopt_an_order_that_does_not_match(tmp_path):
    for i, mismatch in enumerate(({"symbol": "ETH/USD"}, {"side": Side.BUY}, {"raw": {"client_order_id": "someone-else"}})):
        repositories = await _repositories(tmp_path / f"case-{i}")
        await _stranded(repositories)
        broker = _broker()
        broker.get_order_by_client_order_id.return_value = _order(**mismatch)
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert await _status(repositories) == "risk_approved"


async def test_sweep_lookup_error_leaves_intent_unchanged(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.side_effect = RuntimeError("503")
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    assert await _status(repositories) == "risk_approved"
    records = [r["payload"] for r in await repositories.reconciliation_records.list_all()]
    assert [r["outcome"] for r in records] == ["drift_detected"]


async def test_sweep_ignores_young_intent(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, age=timedelta(seconds=30))
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_preserves_integrity_holds_and_session_latch(tmp_path):
    """Restart scenario: a latched session and a disputed-order hold survive the sweep untouched."""
    from tradepulse.models import IntegrityHold, IntegrityHoldType
    from tradepulse.risk import latch_financial_integrity_block, load_session

    repositories = await _repositories(tmp_path)
    await latch_financial_integrity_block(repositories, "pre-existing latch", clock=lambda: NOW)
    hold = IntegrityHold(broker_order_id="order-1", trade_intent_id="other", hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED,
                         reason="INTEGRITY_VIOLATION: disputed", created_at=NOW)
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)
    hold_before = await repositories.integrity_holds.get("order-1")
    session_before = await load_session(repositories)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert await repositories.integrity_holds.get("order-1") == hold_before
    assert await load_session(repositories) == session_before


async def test_sweep_skips_asset_with_live_reservation(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    assert await reserve_symbol_for_execution(repositories.trade_intents.database, BTC, "live-gateway")
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()
    assert await _status(repositories) == "risk_approved"
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_stranded_intents.py -q`
Expected: FAIL, `ImportError: cannot import name '_recover_stranded_intents'`

- [ ] **Step 3: Implement** (in `tradepulse/reconciliation/coordinator.py`, directly above `_recover_inflight_orders`)

```python
STRANDED_INTENT_GRACE_SECONDS = 120


async def _recover_stranded_intents(repositories, broker, alerts, now, lease_lost=None) -> int:
    """Intents approved or marked submitted but never given a broker order id.

    A crash or early return between RISK_APPROVED and broker acceptance leaves
    an intent has_in_flight_intent treats as in flight forever, blocking every
    later order on its asset -- protective exits included. A missing broker
    id proves nothing about submission, so each intent is resolved only by
    Alpaca's answer for its client_order_id, under the asset's execution
    reservation, held and renewed for the whole resolution so a live
    submission is never raced.
    """
    from tradepulse.execution import (
        SYMBOL_LOCK_TTL_SECONDS, execution_lock_key, release_symbol_reservation, reserve_symbol_for_execution,
    )
    from tradepulse.persistence import run_with_lock_renewal

    database = repositories.trade_intents.database
    rows = await list_all_by_statuses(repositories.trade_intents,
                                     [TradeIntentStatus.RISK_APPROVED.value, TradeIntentStatus.SUBMITTED.value])
    resolved = 0
    for row in rows:
        if lease_lost is not None and lease_lost.is_set():
            break
        intent = hydrate("trade_intents", row["payload"])
        if intent.broker_order_id or (now - intent.created_at).total_seconds() < STRANDED_INTENT_GRACE_SECONDS:
            continue
        token = str(uuid4())
        if not await reserve_symbol_for_execution(database, intent.asset, token):
            continue  # a live execution owns this asset -- never race it
        try:
            if await run_with_lock_renewal(database, execution_lock_key(intent.asset), token, SYMBOL_LOCK_TTL_SECONDS,
                                           _resolve_stranded(repositories, broker, alerts, intent, now)):
                resolved += 1
        finally:
            await release_symbol_reservation(database, intent.asset, token)
    return resolved


async def _resolve_stranded(repositories, broker, alerts, intent, now) -> bool:
    """Adopt Alpaca's matching order for this client_order_id, or close the
    intent on a definitive 404 from the account it was approved on."""
    async def unresolved(reason: str) -> bool:
        await _record(repositories, reconciliation_type="order", subject_id=intent.trade_intent_id,
                      outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={"stranded_intent_resolved": True},
                      actual={"error": reason, "status": intent.status.value}, occurred_at=now)
        return False

    try:
        account = await broker.get_account()
        approved_on = intent.risk_snapshot.get("broker_account_number")
        if approved_on is None or account.account_number != approved_on:
            return await unresolved("ACCOUNT_IDENTITY_UNPROVEN")
        order = await broker.get_order_by_client_order_id(intent.trade_intent_id)
    except Exception as exc:  # noqa: BLE001 - an unavailable lookup proves nothing; retry next pass
        return await unresolved(str(exc))
    if order is not None and (order.raw.get("client_order_id") != intent.trade_intent_id
                              or order.symbol != intent.asset.symbol or order.side != intent.side):
        return await unresolved("STRANDED_ORDER_IDENTITY_MISMATCH")
    if order is not None:
        updated = replace(intent, status=TradeIntentStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                          client_order_id=intent.trade_intent_id)
        action = "adopted the broker order Alpaca holds for this client_order_id"
    else:
        updated = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="STRANDED_BEFORE_SUBMISSION")
        action = "closed: Alpaca has no order for this client_order_id"
    await repositories.trade_intents.update(intent.trade_intent_id, updated, status=updated.status.value)
    await _record(repositories, reconciliation_type="order", subject_id=intent.trade_intent_id,
                  outcome=ReconciliationOutcome.CORRECTED, expected={"stranded_intent_resolved": True},
                  actual={"previous_status": intent.status.value, "status": updated.status.value,
                          "broker_order_id": updated.broker_order_id},
                  occurred_at=now, corrective_action=action)
    await alerts.send("warning", f"Stranded {intent.status.value} intent for {intent.asset.symbol} resolved: {action}",
                      {"trade_intent_id": intent.trade_intent_id})
    return True
```

In `run_reconciliation`, insert this immediately before `await _recover_inflight_orders(repositories, broker, settlement, alerts, now, lease_lost)`:

```python
    await _recover_stranded_intents(repositories, broker, alerts, now, lease_lost)
```

- [ ] **Step 4: Run the new tests and the reconciliation tests and verify they pass**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_stranded_intents.py python_tests/test_reconciliation*.py -q`
Expected: exit 0. If an existing reconciliation test's mock broker raises on the unexpected `get_order_by_client_order_id`, that is fine: its intents have broker ids or are young, so the sweep never calls it. Fix the test only if it seeds an old id-less intent.

- [ ] **Step 5: Commit** (message: `fix: resolve stranded pre-submission intents in reconciliation (Rev.115 part 2)`)

### Task 3: Alert on broker positions that nothing protects

**Files:**
- Modify: `tradepulse/monitor/coordinator.py`. Add `unmanaged_positions: int = 0` as the **last** field of `MonitorCycleSummary` (after `error`, so existing positional constructors are unaffected) and a `_report_unmanaged_position` helper; compute the opening inventory once per cycle; replace the `if holding_row is None: continue` branch.
- Test: `python_tests/test_monitor_unmanaged.py` (create)

**Interfaces:**
- Produces: `MonitorCycleSummary.unmanaged_positions: int`. Audit event id format: `unmanaged_position:<asset_key>:<UTC date>`.
- Alerting is immediate: the first cycle that sees an unprotected position (at most 30 s after it appears, per Task 4) sends a critical alert. Later cycles that day only count it, so Telegram isn't flooded every cycle.

- [ ] **Step 1: Write the failing tests** (`python_tests/test_monitor_unmanaged.py`)

```python
"""Rev.115 F5: a broker position with no local holding has no stop -- say so, once a day."""
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from test_settlement_engine import _repositories
from tradepulse.broker import AlpacaPosition
from tradepulse.models import AssetClass
from tradepulse.monitor.coordinator import run_position_monitor
from tradepulse.config import risk_limits_for_profile

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)


def _position(qty="5"):
    return AlpacaPosition("AAPL", AssetClass.EQUITY, Decimal(qty), Decimal(200), Decimal(qty) * 200, Decimal(200), Decimal(0))


async def _run(repositories, broker, alerts):
    return await run_position_monitor(repositories, broker, AsyncMock(), AsyncMock(), alerts,
                                      risk_limits_for_profile("balanced"), clock=lambda: NOW)


async def test_unmanaged_position_alerts_once_per_day(tmp_path):
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position()]
    first = await _run(repositories, broker, alerts)
    second = await _run(repositories, broker, alerts)
    assert (first.unmanaged_positions, second.unmanaged_positions) == (1, 1)
    assert alerts.send.await_count == 1
    assert alerts.send.await_args.args[0] == "critical"
    events = [r["payload"] for r in await repositories.audit_events.list_all()]
    assert [e["event_type"] for e in events] == ["unmanaged_broker_position"]


async def test_opening_inventory_at_opening_quantity_is_not_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position("5")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 0
    alerts.send.assert_not_awaited()


async def test_opening_inventory_at_a_different_quantity_is_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position("8")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 1
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_monitor_unmanaged.py -q`
Expected: FAIL, `AttributeError: 'MonitorCycleSummary' object has no attribute 'unmanaged_positions'`

- [ ] **Step 3: Implement**

Add `AuditEvent` to the `tradepulse.models` import, then add the helper above `run_position_monitor`:

```python
async def _report_unmanaged_position(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                                     position: AlpacaPosition, now: datetime) -> None:
    """A broker position with no local holding has no stop, target or time
    stop. Alert once per asset per UTC day: a deterministic audit event id
    makes create_once the deduplication."""
    key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
    event_id = f"unmanaged_position:{key}:{now.date().isoformat()}"
    event = AuditEvent(
        event_id=event_id, event_type="unmanaged_broker_position", severity="critical",
        message=(f"UNMANAGED_POSITION: {position.symbol} qty {position.qty} has no local holding -- "
                 "no stop, target or time stop protects it."),
        occurred_at=now, entity_type="broker_position", entity_id=key,
        details={"symbol": position.symbol, "asset_class": position.asset_class.value, "qty": str(position.qty)},
    )
    if await repositories.audit_events.create_once(event_id, event):
        await alerts.send("critical", event.message, dict(event.details))
```

In `run_position_monitor`, after the lots pre-fetch block, add:

```python
    # Opening inventory of a bound generation is excluded by design and never
    # monitored; any other broker position without a holding is unprotected.
    from tradepulse.reconciliation.membership import opening_quantities
    from tradepulse.verification.opening import load_bound_opening_checkpoint
    opening = opening_quantities(await repositories.trade_intents.database.run(load_bound_opening_checkpoint))
    unmanaged = 0
```

Replace the branch:

```python
        if holding_row is None:
            key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
            if opening.get(key) != position.qty:
                unmanaged += 1
                await _report_unmanaged_position(repositories, alerts, position, clock())
            continue
```

Change the final return to `return MonitorCycleSummary("ok", len(positions), exits_triggered, execution_results, unmanaged_positions=unmanaged)`. The degraded return needs no change (the field defaults to 0).

- [ ] **Step 4: Run the new tests and the monitor tests and verify they pass**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_monitor_unmanaged.py python_tests/test_position_monitor*.py -q`
Expected: exit 0. If `create_once` returns `None` instead of `True` on insert, the first test fails with zero alerts; then compare the audit event count before and after instead.

- [ ] **Step 5: Commit** (message: `feat: alert once a day on broker positions with no protection (Rev.115 part 3)`)

### Task 4: Correct stale lane comments, tighten monitor cadence with one interval authority, ship Rev.115

**Files:**
- Create: `tradepulse/config/lanes.py` (the single lane-interval authority)
- Modify: `tradepulse/cli.py:153-157` (the interval constants come from it); `tradepulse/verification/soak.py:26` (`REQUIRED_LANES` comes from it); `tradepulse/monitor/coordinator.py:150-157` and `:171-175` (comments)
- Test: `python_tests/test_accounting_soak.py` (append)
- Create: `docs/rev115-position-protection-liveness.md`

The soak's lane-continuity criterion (`REQUIRED_LANES`, maximum gap `2 x interval + 120`) used to duplicate the runtime intervals. Changing only the runtime would leave the evidence check six times looser than the lane, so both must read one authority.

- [ ] **Step 0: Write the failing tests** (append to `python_tests/test_accounting_soak.py`; add `from datetime import UTC, datetime` to its imports if absent)

```python
def test_soak_lane_criteria_match_runtime_intervals():
    from tradepulse import cli
    from tradepulse.config.lanes import LANE_INTERVAL_SECONDS
    from tradepulse.verification.soak import REQUIRED_LANES

    assert REQUIRED_LANES == LANE_INTERVAL_SECONDS
    assert (cli.EQUITY_SCAN_INTERVAL_SECONDS, cli.CRYPTO_SCAN_INTERVAL_SECONDS, cli.OPTION_SCAN_INTERVAL_SECONDS,
            cli.MONITOR_INTERVAL_SECONDS, cli.SETTLE_INTERVAL_SECONDS) == tuple(
        LANE_INTERVAL_SECONDS[k] for k in ("equity", "crypto", "option", "monitor", "settle"))
    assert LANE_INTERVAL_SECONDS["monitor"] == 30


def test_monitor_stall_over_two_minutes_breaks_continuity():
    from datetime import timedelta

    from tradepulse.config.lanes import LANE_MAX_GAP_SECONDS
    from tradepulse.verification.soak import _lane_evidence

    assert LANE_MAX_GAP_SECONDS["monitor"] == 120
    assert LANE_MAX_GAP_SECONDS["reconcile"] == 2 * 60 + 120  # unchanged for other lanes
    start = datetime(2026, 10, 2, 14, tzinfo=UTC)

    def cycles(offsets):
        return [{"event_type": "verification_lane_cycle", "details": {"lane": "monitor"},
                 "occurred_at": (start + timedelta(seconds=s)).isoformat()} for s in offsets]

    segment = [{"started_at": start.isoformat(), "ended_at": (start + timedelta(seconds=600)).isoformat()}]
    healthy = _lane_evidence(cycles(range(30, 600, 30)), segment)["monitor"]
    stalled = _lane_evidence(cycles([30, 60, 210, *range(240, 600, 30)]), segment)["monitor"]
    assert healthy["continuous"] and healthy["maximum_permitted_gap_seconds"] == 120
    assert not stalled["continuous"]  # a 150 s silence
```

Run it: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_accounting_soak.py -k lane_criteria -q`. Expected: FAIL, `ModuleNotFoundError: No module named 'tradepulse.config.lanes'`.

Create `tradepulse/config/lanes.py`:

```python
"""The single authority for supervised lane intervals (seconds).

The runtime schedules each lane at this cadence and the soak's lane-continuity
evidence (verification/soak.py) bounds gaps from the same values, so the two
can never drift apart. The monitor runs every 30 s because stops are evaluated
in software (see the Phase 4 broker-side stop spec).
"""
LANE_INTERVAL_SECONDS = {"equity": 900, "crypto": 600, "option": 1200, "monitor": 30, "settle": 60, "reconcile": 60}

# Longest silence between completed cycles the soak accepts. A gap is one
# cycle's duration plus the interval (the heartbeat fires at cycle end).
# Scan/settle/reconcile keep the historical 2 x interval + 120 s. The monitor
# gets an explicit 30 s + 90 s cycle budget: 4x the longest cycle observed
# (22.5 s across 676 cycles in two soaks) and several 20 s fill waits, while
# any protection stall over two minutes fails continuity.
LANE_MAX_GAP_SECONDS = {lane: 2 * interval + 120 for lane, interval in LANE_INTERVAL_SECONDS.items()}
LANE_MAX_GAP_SECONDS["monitor"] = 120
```

In `tradepulse/cli.py`, replace the five interval constants with:

```python
from tradepulse.config.lanes import LANE_INTERVAL_SECONDS

EQUITY_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["equity"]
CRYPTO_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["crypto"]
OPTION_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["option"]
MONITOR_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["monitor"]
SETTLE_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["settle"]
```

In `tradepulse/verification/soak.py`, replace the `REQUIRED_LANES = {...}` literal with `REQUIRED_LANES = LANE_INTERVAL_SECONDS`, imported from `tradepulse.config.lanes` together with `LANE_MAX_GAP_SECONDS`. In `_lane_evidence`, replace both occurrences of `2 * interval + 120` (the `maximum = ...` line and the `"maximum_permitted_gap_seconds"` value) with `LANE_MAX_GAP_SECONDS[lane]`. Leave the separate market-session `maximum_gap = 2 * REQUIRED_LANES["reconcile"] + 120` unchanged: it bounds broker-clock receipts, not lane silence. Check that the verification-generation reconcile lane in `cli.py` still uses a 60 s cadence; if it has its own constant, route it through `LANE_INTERVAL_SECONDS["reconcile"]` too.

- [ ] **Step 1: Replace the stale comments.** In `_fetch_atr`'s docstring, replace the sentence beginning "an unguarded fetch here would propagate out of this module" through "for the rest of the run." with:

```text
an unguarded fetch here would propagate out of this module and out of
    _periodic_loop, failing the whole monitor lane; cli.py::_supervised_lane
    restarts it with capped backoff, but every position would go unchecked
    until then.
```

In the defense-in-depth comment, replace "since _supervised_lane never restarts a lane after an unhandled exception, this one matters enough" with "since a lane failure leaves every position unchecked until _supervised_lane's backoff restart, this one matters enough".

- [ ] **Step 2: Confirm the cadence.** `MONITOR_INTERVAL_SECONDS` is now 30 through `LANE_INTERVAL_SECONDS` (Step 0). Re-run the Step 0 test and expect PASS.

- [ ] **Step 3: Write `docs/rev115-position-protection-liveness.md`** using the established revision-note format. Its "Finding" section covers F1, F5 and F6. "Changes" describes Tasks 1-4 with their exact reason strings (`BROKER_POSITIONS_UNAVAILABLE`, `BROKER_EXIT_QUANTITY_CHANGED`, `STRANDED_BEFORE_SUBMISSION`, `UNMANAGED_POSITION`) and the 120 → 30 s cadence. "Validation" lists the new test files.

- [ ] **Step 4: Run the full suite**

Run: `PYTHONPATH=$PWD timeout 900 /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests -q -p no:cacheprovider; echo exit=$?`
Expected: `exit=0`

- [ ] **Step 5: Commit** (message: `fix: position protection and execution liveness (Rev.115)`)

---

## Phase 2: Rev.116, risk and control plane

### Task 5: Cap the whole position, not each order

**Files:**
- Modify: `tradepulse/risk/engine.py`. Add `held_notional: Decimal = Decimal("0")` to `RiskEvalOptions` after `held_quantity`, and change the max-position cap.
- Modify: `tradepulse/execution/gateway.py`. Pass `held_notional` in `RiskEvalOptions(...)`.
- Test: `python_tests/test_risk_engine.py` (append)

**Interfaces:**
- Produces: `RiskEvalOptions.held_notional`, the absolute broker market value already held in this asset (multiplier included).

- [ ] **Step 1: Write the failing tests** (append to `python_tests/test_risk_engine.py`)

```python
def _quoted(**extra) -> RiskEvalOptions:
    return RiskEvalOptions(bid=Decimal("99.95"), ask=Decimal("100.05"), estimated_slippage_pct=Decimal("0.05"),
                           available_cash=Decimal("100000"), **extra)


def test_held_notional_counts_toward_position_cap() -> None:
    # balanced: max_position_pct 7% of 100k = 7,000; already holding 6,000 leaves 1,000 = 10 shares at $100
    decision = evaluate_risk(_buy(requested_quantity=Decimal("50")), _snapshot(), LIMITS, _quoted(held_notional=Decimal("6000")))
    assert decision.approved and decision.approved_quantity == Decimal("10")
    assert "POSITION_CAPPED_TO_10_BY_MAX_POSITION_PCT" in decision.reasons


def test_position_already_at_cap_cannot_grow() -> None:
    decision = evaluate_risk(_buy(requested_quantity=Decimal("5")), _snapshot(), LIMITS, _quoted(held_notional=Decimal("7000")))
    assert not decision.approved
    assert "INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in decision.reasons


def test_held_option_notional_counts_toward_position_cap() -> None:
    option = _buy(symbol="IWM261106C00287000", asset_class=AssetClass.OPTION, requested_quantity=Decimal("5"),
                  price=Decimal("3"), contract_multiplier=Decimal("100"))
    opts = RiskEvalOptions(bid=Decimal("2.98"), ask=Decimal("3.02"), estimated_slippage_pct=Decimal("0.6"),
                           available_cash=Decimal("100000"), held_notional=Decimal("6400"))
    decision = evaluate_risk(option, _snapshot(), LIMITS, opts)
    assert decision.approved_quantity == Decimal("2")  # (7000 - 6400) / (3 x 100) = 2 contracts
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_risk_engine.py -k "held" -q`
Expected: FAIL, `TypeError: ... unexpected keyword argument 'held_notional'`

- [ ] **Step 3: Implement.** In `RiskEvalOptions`, add `held_notional: Decimal = Decimal("0")` directly after `held_quantity`. Replace the max-position block in `evaluate_risk`:

```python
        # The cap bounds the whole position: notional already held in this
        # asset (broker market value, multiplier included) consumes it.
        max_position_notional = (limits.max_position_pct / 100) * total_equity - opts.held_notional
        if approved_qty * notional_per_unit > max_position_notional:
            approved_qty = _round_qty(max(max_position_notional, Decimal(0)) / notional_per_unit, intent.asset_class)
            reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_POSITION_PCT")
```

In `gateway.py`, in the `RiskEvalOptions(...)` call, add:

```python
                held_notional=abs(held_position.market_value) if held_position is not None and request.side == Side.BUY else Decimal("0"),
```

- [ ] **Step 4: Run the risk, gateway and scanner tests and verify they pass**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_risk_engine.py python_tests/test_execution_gateway.py python_tests/test_scanner_coordinator.py -q`
Expected: exit 0

- [ ] **Step 5: Commit** (message: `fix: max_position_pct caps the whole position (Rev.116 part 1)`)

### Task 6: Refuse cross-site and rebinding requests to the dashboard

**Files:**
- Modify: `tradepulse/web/app.py`. Add the `local_control_guard` middleware inside `create_app`, directly after `app.state.tp = state`, plus the imports `from urllib.parse import urlsplit` and `from fastapi.responses import JSONResponse`.
- Modify: `frontend/src/api.ts`, the `post()` headers.
- Modify: `python_tests/test_web_app.py:52`, the client fixture.
- Test: `python_tests/test_web_app.py` (append)

**Interfaces:**
- Produces: header contract `X-TradePulse-Control: 1` on every non-GET `/api` request; allowed hosts `127.0.0.1` and `localhost`.

- [ ] **Step 1: Write the failing tests** (append to `python_tests/test_web_app.py`)

```python
async def _raw_client(tmp_path, host="127.0.0.1"):
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    state = await build_app_state(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)), base_url=f"http://{host}:8766"), state


async def test_mutation_without_control_header_is_refused(tmp_path):
    client, _ = await _raw_client(tmp_path)
    response = await client.post("/api/session/stop")
    assert response.status_code == 403 and response.json()["detail"] == "CONTROL_HEADER_REQUIRED"


async def test_cross_origin_mutation_is_refused_even_with_header(tmp_path):
    client, _ = await _raw_client(tmp_path)
    response = await client.post("/api/session/stop", headers={"X-TradePulse-Control": "1", "Origin": "https://evil.example"})
    assert response.status_code == 403 and response.json()["detail"] == "ORIGIN_NOT_ALLOWED"


async def test_rebound_host_is_refused_for_reads(tmp_path):
    client, _ = await _raw_client(tmp_path, host="attacker.example")
    assert (await client.get("/api/session")).status_code == 403


async def test_local_reads_need_no_header_and_local_mutations_work(tmp_path):
    client, _ = await _raw_client(tmp_path)
    assert (await client.get("/api/session")).status_code == 200
    response = await client.post("/api/session/stop", headers={"X-TradePulse-Control": "1", "Origin": "http://127.0.0.1:8766"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_web_app.py -k "control_header or cross_origin or rebound or local_reads" -q`
Expected: 3 FAIL (status 200 instead of 403), 1 PASS

- [ ] **Step 3: Implement the middleware** in `create_app`, directly after `app.state.tp = state`:

```python
    @app.middleware("http")
    async def local_control_guard(request: Request, call_next):
        """Localhost binding alone does not stop a page in the operator's own
        browser. Host must be local on every request (defeats DNS rebinding).
        Mutations also need a local or absent Origin plus a custom header,
        which a cross-site page cannot send without a CORS preflight this app
        never grants."""
        if request.url.hostname not in _LOCAL_HOSTS:
            return JSONResponse({"detail": "HOST_NOT_ALLOWED"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).hostname not in _LOCAL_HOSTS:
                return JSONResponse({"detail": "ORIGIN_NOT_ALLOWED"}, status_code=403)
            if request.headers.get(_CONTROL_HEADER) != "1":
                return JSONResponse({"detail": "CONTROL_HEADER_REQUIRED"}, status_code=403)
        return await call_next(request)
```

At module level, near `_CONFIRMATION_PHRASE`:

```python
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_CONTROL_HEADER = "X-TradePulse-Control"
```

- [ ] **Step 4: Update the existing client fixture** at `python_tests/test_web_app.py:52`:

```python
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766",
                               headers={"X-TradePulse-Control": "1"})
```

- [ ] **Step 5: Update the frontend** `post()` in `frontend/src/api.ts`:

```ts
    headers: body === undefined
      ? { 'X-TradePulse-Control': '1' }
      : { 'Content-Type': 'application/json', 'X-TradePulse-Control': '1' },
```

- [ ] **Step 6: Run the Python web tests and the frontend tests**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_web_app.py -q && (cd frontend && npm test -- --run)`
Expected: both exit 0. If a frontend test asserts the exact fetch init for a POST, add the header to its expectation.

- [ ] **Step 7: Write `docs/rev116-risk-and-control-plane.md`** (F4 and F2 finding, change, validation), run the full suite (expect `exit=0`), and commit: `fix: whole-position cap and dashboard request guard (Rev.116)`

---

## Phase 3: Rev.117, verification, operations and consistency

### Task 7: Soak preflight refuses unsafe opening conditions

**Files:**
- Modify: `scripts/run_accounting_soak.py`. Add `preflight()` and `_broker_preflight()`, and call `await _broker_preflight()` as the first statement inside `run()`'s `try:` block (before the freeze command).
- Modify: `python_tests/test_accounting_soak.py`. The `runner` fixture sets `module._broker_preflight = AsyncMock()` before returning, so existing runner tests make no broker calls.
- Test: `python_tests/test_accounting_soak.py` (append)

**Interfaces:**
- Produces: `preflight(broker, *, now: datetime | None = None) -> list[str]`, returning any of `OPEN_BROKER_ORDERS`, `PRE_GENERATION_TRADE_TODAY` and `FEE_BATCH_PENDING:<YYYYMMDD>`. An empty list means clear.

- [ ] **Step 1: Write the failing tests** (append; add `from unittest.mock import AsyncMock`, `from types import SimpleNamespace` and `from datetime import UTC, datetime` to the imports if they are absent)

```python
def _broker(activities, open_orders=()):
    broker = _broker()
    broker.get_open_orders.return_value = list(open_orders)
    broker.get_activities.return_value = [SimpleNamespace(raw=a) for a in activities]
    return broker


PREFLIGHT_NOW = datetime(2026, 10, 2, 14, tzinfo=UTC)  # 10:00 ET on 2026-10-02
FILL_0929 = {"id": "20260929135313600::a", "activity_type": "FILL", "symbol": "IWM261030C00286000", "side": "buy"}
FEE_0929 = {"id": "20260929000000000::b", "activity_type": "FEE", "activity_sub_type": "OCC"}


async def test_preflight_clear_when_last_trade_day_fees_have_posted(runner):
    assert await runner.preflight(_broker([FEE_0929, FILL_0929]), now=PREFLIGHT_NOW) == []


async def test_preflight_refuses_pending_fee_batch(runner):
    assert await runner.preflight(_broker([FILL_0929]), now=PREFLIGHT_NOW) == ["FEE_BATCH_PENDING:20260929"]


async def test_preflight_refuses_trade_today_and_open_orders(runner):
    today = {"id": "20261002094100000::c", "activity_type": "FILL", "symbol": "BTC/USD", "side": "buy"}
    problems = await runner.preflight(_broker([FEE_0929, FILL_0929, today], open_orders=[object()]), now=PREFLIGHT_NOW)
    assert problems == ["OPEN_BROKER_ORDERS", "PRE_GENERATION_TRADE_TODAY"]


async def test_preflight_ignores_crypto_only_and_equity_buy_only_days(runner):
    crypto = {"id": "20260930101500000::d", "activity_type": "FILL", "symbol": "SOL/USD", "side": "buy"}
    equity_buy = {"id": "20260930101600000::e", "activity_type": "FILL", "symbol": "AAPL", "side": "buy"}
    assert await runner.preflight(_broker([FEE_0929, FILL_0929, crypto, equity_buy]), now=PREFLIGHT_NOW) == []
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_accounting_soak.py -k preflight -q`
Expected: FAIL, `AttributeError: module 'accounting_soak_runner' has no attribute 'preflight'`

- [ ] **Step 3: Implement** (in `scripts/run_accounting_soak.py`; add `from datetime import UTC, datetime` to the imports)

```python
async def preflight(broker, *, now: datetime | None = None) -> list[str]:
    """Reasons a soak must not open on this account now; empty means clear.

    Alpaca posts fee rows in an end-of-day batch with a midnight-of-trade-date
    id that sorts before the day's fills. A fee from a pre-generation trade
    posting after the opening classifies as unresolved membership and latches
    the integrity block, so the account must have no trades today and the
    last sell/option trade day's fee batch must already be posted. Equity buys
    and crypto may legitimately post no FEE row, so they don't gate.
    """
    from zoneinfo import ZoneInfo

    now = now or datetime.now(UTC)
    today = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    problems = []
    if await broker.get_open_orders():
        problems.append("OPEN_BROKER_ORDERS")
    activities = [dict(a.raw) for a in await broker.get_activities(activity_type=None)]
    fills = [r for r in activities if r.get("activity_type") == "FILL"]
    if any(r["id"][:8] == today for r in fills):
        problems.append("PRE_GENERATION_TRADE_TODAY")
    fee_days = sorted({r["id"][:8] for r in fills if "/" not in str(r.get("symbol", ""))
                       and (r.get("side") == "sell" or len(str(r.get("symbol", ""))) > 12)})
    if fee_days and not any(r.get("activity_type") == "FEE" and r["id"][:8] == fee_days[-1] for r in activities):
        problems.append("FEE_BATCH_PENDING:" + fee_days[-1])
    return problems


async def _broker_preflight() -> None:
    from tradepulse.cli import _load_dotenv
    from tradepulse.config import Settings
    from tradepulse.session_commands import build_broker

    _load_dotenv()
    broker = build_broker(Settings.from_env())
    try:
        problems = await preflight(broker)
    finally:
        await broker.aclose()
    if problems:
        raise VerificationError("soak_preflight_refused:" + ",".join(problems))
```

(The option-symbol test `len(symbol) > 12` relies on OCC symbols always being longer than 12 characters, while equity tickers never are.)

- [ ] **Step 4: Run all soak tests and verify they pass**

Run: `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests/test_accounting_soak.py -q`
Expected: exit 0

- [ ] **Step 5: Commit** (message: `feat: soak preflight refuses same-day trades, open orders and pending fee batches (Rev.117 part 1)`)

### Task 8: Make the two load-sensitive tests deterministic

**Files:**
- Modify: `python_tests/test_execution_gateway.py`, `test_concurrent_buys_for_different_symbols_serialize_through_portfolio_risk_lock`
- Modify: `python_tests/test_execution_idempotency.py`, `test_in_flight_detection_is_correct_behind_a_large_non_blocking_backlog`

- [ ] **Step 1: Hold the portfolio lock explicitly** instead of relying on two tasks interleaving. Replace the `results = await asyncio.gather(...)` line and the assertions that follow it with:

```python
    from tradepulse.execution import PORTFOLIO_RISK_LOCK_KEY, PORTFOLIO_RISK_LOCK_TTL_SECONDS
    from tradepulse.persistence import acquire_lock, release_lock

    database = repositories.trade_intents.database
    assert await acquire_lock(database, PORTFOLIO_RISK_LOCK_KEY, "concurrent-evaluator", "test", PORTFOLIO_RISK_LOCK_TTL_SECONDS)
    blocked = await gateway.execute_intent(btc_request)
    assert blocked.status == "skipped" and blocked.reasons == ["PORTFOLIO_RISK_EVALUATION_LOCKED"]
    await release_lock(database, PORTFOLIO_RISK_LOCK_KEY, "concurrent-evaluator")

    winner = await gateway.execute_intent(aapl_request)
    await broker.aclose()
    assert winner.status in ("filled", "pending", "rejected")  # reached a real decision once the lock was free
```

Keep the docstring and add one sentence: "The lock is held explicitly so the contended path is exercised deterministically, independent of scheduler timing."

- [ ] **Step 2: Seed the backlog sequentially.** Replace `await asyncio.gather(*(_seed(i) for i in range(1100)))` with:

```python
    for i in range(1100):  # sequential: the property under test is backlog size, not write concurrency
        await _seed(i)
```

- [ ] **Step 3: Run both tests three times**

Run: `for i in 1 2 3; do PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest "python_tests/test_execution_gateway.py::test_concurrent_buys_for_different_symbols_serialize_through_portfolio_risk_lock" "python_tests/test_execution_idempotency.py::test_in_flight_detection_is_correct_behind_a_large_non_blocking_backlog" -q -p no:cacheprovider; echo exit=$?; done`
Expected: `exit=0` three times

- [ ] **Step 4: Commit** (message: `test: make the portfolio-lock and backlog tests deterministic (Rev.117 part 2)`)

### Task 9: Supervised always-on runtime and operator runbook, then ship Rev.117

**Files:**
- Create: `deploy/tradepulse-run.service`
- Create: `docs/operations-runbook.md`
- Create: `docs/rev117-verification-operations-consistency.md`

- [ ] **Step 1: Write the systemd user unit** `deploy/tradepulse-run.service`:

```ini
[Unit]
Description=TradePulse trading runtime (paper)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/tradepulse-ai
ExecStart=%h/tradepulse-ai/.venv/bin/tradepulse run --no-browser
Restart=on-failure
RestartSec=30
# Positions are only protected while this process runs (see the Phase 4 spec).
KillSignal=SIGINT
TimeoutStopSec=900

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Write `docs/operations-runbook.md`** with these sections, written out in full:
  1. **Host.** The runtime protects positions only while it runs. Use an always-on, mains-powered host. Install the unit with `mkdir -p ~/.config/systemd/user && cp deploy/tradepulse-run.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now tradepulse-run && loginctl enable-linger $USER`.
  2. **Starting a soak.** Run the exact `setsid nohup systemd-inhibit ... scripts/run_accounting_soak.py ...` command, and confirm the preflight passes. Never trade the account, close opening inventory or edit `tradepulse/` during a run. The final reconciliation must land after the ~17:45 PT fee batch.
  3. **Stopping cleanly.** Send `kill -INT <runtime pid>`. The runner then writes the report.
  4. **Secrets.** Rotate the Telegram bot token (earlier logs contained it). Never paste keys into agent sessions.
  5. **Repository hygiene.** Unlink the Base44 app from `Redchief-sudo/tradepulse-ai`, fetch before every push, and delete `backup/base44-bot-push-20260928` once it is no longer wanted.

- [ ] **Step 3: Write `docs/rev117-verification-operations-consistency.md`** (F8, F7 and operations finding, change, validation).

- [ ] **Step 4: Run the full suite**

Run: `PYTHONPATH=$PWD timeout 900 /home/damien/tradepulse-ai/.venv/bin/python -m pytest python_tests -q -p no:cacheprovider; echo exit=$?`
Expected: `exit=0`

- [ ] **Step 5: Commit** (message: `feat: soak preflight, deterministic tests, supervised runtime and runbook (Rev.117)`)

---

## Phase 4: Broker-side protective stops (design spec, not implemented here)

### Task 10: Write the broker-side stop design spec

**Files:**
- Create: `docs/superpowers/specs/<date>-broker-side-stops.md` (via `superpowers:brainstorming`)

- [ ] **Step 1: Run `superpowers:brainstorming` with the operator** to decide the following. Each decision must be recorded in the spec:
  1. **Which asset classes get a resting broker stop?** Alpaca accepts equity `stop` orders and crypto `stop_limit` orders. Option stop support must be verified against the live paper API before it is assumed.
  2. **How it is placed.** As a trade intent through `ExecutionGateway` (`client_order_id` = intent id), so a broker-triggered fill is a known order recovered by `_recover_inflight_orders`, never a "missed fill" that latches the integrity block.
  3. **Quantity reservation.** A resting stop holds shares, so a monitor exit must cancel it first. That needs a cancel-then-exit sequence and a rule for when the cancel is ambiguous.
  4. **Level.** A catastrophe level, e.g. the entry `stop_loss`, never ratcheted at the broker. Or ratcheted with replace orders, and what that costs in API calls.
  5. **Generation interaction.** Resting stop orders vs the soak's "no pending orders" opening rule and the preflight.
- [ ] **Step 2: Commit the spec**, then write its own implementation plan with `superpowers:writing-plans`.

## Expected ratings after Rev.115-117

| Area | Now | After | Remaining gap |
|---|---|---|---|
| Order execution | 7.5 | 9 | (none) |
| Position monitor and closing | 6 | 9 on an always-on host | No protection while down; Phase 4 removes the host dependency |
| Risk engine | 8 | 9 | (none) |
| Session control | 8.5 | 9 | (none) |
| Dashboard security | 5 | 9 | (none) |
| Verification and soak | 8 | 9 | (none) |
| Broker and data | 8 | 9 | (none) |
| Scanner and strategy | 7 | 9 for engineering | Trade frequency is strategy calibration, not code |
| Code and docs consistency | 7 | 9 | (none) |
| Operations | 6 | 9 once the runbook steps are done | Host choice and token rotation are operator actions |
| Accounting, reconciliation, persistence | 9 | 9 | (none) |
