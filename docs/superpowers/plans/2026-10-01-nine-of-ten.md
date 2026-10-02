# Every Area to 9/10 Implementation Plan (v4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the fresh Rev.114 audit, and from two independent plan reviews, so each TradePulse area rates at least 9/10 once implementation is tested and a fresh soak confirms it.

**Architecture:** Three shippable revisions on the worktree branch:
- **Rev.115:** position protection and execution liveness.
- **Rev.116:** risk and control plane.
- **Rev.117:** verification, operations and consistency.

Broker-side protective stops (F3) change exit semantics and get their own design spec (Phase 4). This plan ends by writing that spec, not by implementing it.

**Tech Stack:** Python 3.12, asyncio, SQLite (WAL), FastAPI/uvicorn, httpx + respx for tests, pytest-asyncio (auto mode), React/Vite frontend (vitest).

**Spec:** `docs/superpowers/specs/2026-10-01-nine-of-ten-audit.md`

**Revision history:**
- **v2:** one lane-interval authority; account-bound, client-id-exact stranded recovery.
- **v3:**
  - lease-loss fence and atomic conditional commit for stranded recovery, with re-read under the lock
  - holds on the recovered intent
  - protective lanes survive a refused activation, and plain `run` reconciles
  - paper-enforced, resume-safe service
  - preflight built from the runner's effective paper configuration and bound to the opening account
  - per-day fee evidence with operator acknowledgement
  - barrier-coordinated concurrency test
  - unmanaged-position detection before exits, with a measured latency bound
  - corrected test file names and commands
- **v4:**
  - stranded commit verifies the asset reservation and the parent reconciliation lease (owner token and expiry) inside the same transaction, and compares the full re-read payload
  - scan lanes always start and idle until the session is active, so `tradepulse start` resumes scanning without a restart
  - `preflight.json` written as an object
  - acknowledgement dates validated and never able to bypass same-day, open-order or not-yet-closed refusals
  - ten-day preflight window documented

## Global Constraints

- Work in `/home/damien/tradepulse-rev113` (branch `rev113-options-liquidity`), never in `~/tradepulse-ai` while `pgrep -f run_accounting_soak` matches. A protected-source change there invalidates the running soak.
- Run tests with `PYTHONPATH=$PWD /home/damien/tradepulse-ai/.venv/bin/python -m pytest ...` from the worktree root. Below, `PYTEST` means exactly that prefix.
- Separate runtime resources while a soak runs. Never start `tradepulse run`, the dashboard, a soak, a systemd unit, or anything that calls the live Alpaca account from the worktree, since port 8766 and the paper account belong to the running soak. Tests use `tmp_path` databases and respx/AsyncMock brokers only.
- Money is `Decimal` only. Never construct a `Decimal` from a float.
- Fail closed: a broker lookup error is "unknown", never "not found" and never "rejected".
- Protective exits must never be blocked by a session state, the market clock, a kill switch, or a new guard. Protective lanes (monitor, settlement, reconciliation) must run whenever the process runs.
- **Stranded-intent finality.** A missing `broker_order_id` never proves an order was not submitted. A stranded intent may be closed or adopted only when all of these hold:
  1. The asset's execution reservation is held, renewed, and **not lost**. A lease-loss callback fences off further work, and the commit transaction itself re-verifies the reservation, and the parent reconciliation lease when one is held, by owner token and unexpired `expires_at`.
  2. The intent is **re-read under that reservation** and is still `risk_approved` or `submitted`, with no broker id, older than the grace period, and approved on the current broker account (`risk_snapshot.broker_account_number`).
  3. A **successful** lookup for the **exact** `client_order_id` (= `trade_intent_id`) either returns an order matching `client_order_id`, symbol and side (adopt it), or returns Alpaca's definitive 404 (close it).
  4. No integrity hold references the intent, or the order being adopted.
  5. The write is a single conditional transaction that compares the **full** stored payload with the re-read one, so no concurrent change (same status or not) is ever overwritten.

  The grace period is not a finality proof. It only keeps the sweep away from fresh intents; finality comes from 1-5.
- Commits are GPG-signed. Use `git commit -F -` with a `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer. The full suite must pass before each revision commit.
- Each revision adds `docs/revNNN-<slug>.md` in the existing style (finding, change, validation).
- Keep explanatory `# noqa: BLE001 - <reason>` comments. Keep the `Decimal("2")` house style.

## Review Focus

1. **A recovery racing a live submission, or losing its lease mid-lookup.** No write may happen (Task 2 tests `test_sweep_skips_asset_with_live_reservation`, `test_lease_lost_during_slow_lookup_writes_nothing`).
2. **A concurrent status change during the recovery lookup.** The competitor's state must survive (Task 2 test `test_concurrent_status_change_is_never_overwritten`).
3. **A restart while the session is latched (risk stop or integrity block) with open positions.** The monitor, settlement and reconciliation lanes must run; scan lanes must idle with no AI call and no scan record (Task 4 test `test_scan_lane_idles_without_any_work_while_session_inactive`).
4. **A restart after an operator stop under the service, followed by `tradepulse start`.** Trading must not re-activate by itself; after the operator's `start`, scanning must resume within one idle poll, with no restart (Task 4 test `test_operator_start_resumes_scanning_without_restart`).
5. **An unprotected position while other positions are exiting.** It must be detected before any exit work in that cycle (Task 3 test `test_unmanaged_detection_precedes_exit_work`).
6. **A soak opened against a different account than the one the preflight inspected.** It must be refused before the runtime starts (Task 8 test `test_runner_refuses_opening_on_a_different_account`).

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `tradepulse/execution/gateway.py` | Close the intent whenever execution stops after `RISK_APPROVED` without submitting; record `broker_account_number`; pass held notional to risk | 1, 6 |
| `tradepulse/reconciliation/coordinator.py` | `_recover_stranded_intents` with lease fence, re-read and atomic conditional commit | 2 |
| `tradepulse/monitor/coordinator.py` | Unmanaged-position first pass and alert; `MonitorCycleSummary.unmanaged_positions`; corrected comments | 3, 5 |
| `tradepulse/cli.py` | Every lane always starts; scan lanes idle until the session is active; reconcile lane in plain `run`; `run --resume`; reconcile lease passed to recovery; lane intervals from one authority | 2, 4, 5 |
| `tradepulse/config/lanes.py` | Lane interval and maximum-gap authority | 5 |
| `tradepulse/verification/soak.py` | Lane evidence reads `LANE_INTERVAL_SECONDS` / `LANE_MAX_GAP_SECONDS` | 5 |
| `tradepulse/risk/engine.py` | `RiskEvalOptions.held_notional`; whole-position cap | 6 |
| `tradepulse/web/app.py`, `frontend/src/api.ts` | Host/Origin/control-header guard | 7 |
| `scripts/run_accounting_soak.py` | Preflight from the effective paper configuration; `preflight.json`; opening-account binding | 8 |
| `python_tests/test_execution_gateway.py`, `python_tests/test_execution_idempotency.py` | Barrier-coordinated concurrency test; sequential backlog seeding | 9 |
| `deploy/tradepulse-run.service`, `docs/operations-runbook.md` | Paper-enforced, resume-safe supervised runtime; operator checklist | 10 |

---

## Phase 1: Rev.115, position protection and execution liveness

### Task 1: Never strand an intent after a pre-submission early return; record the approving account

**Files:**
- Modify: `tradepulse/execution/gateway.py`. Add the new `_reject_before_submission` method, change the crypto protective re-read returns (around lines 463-478), and add the `risk_snapshot` entry.
- Test: `python_tests/test_execution_gateway.py` (append)

**Interfaces:**
- Produces: `ExecutionGateway._reject_before_submission(intent: TradeIntent, reason: str) -> ExecutionResult`, which persists `REJECTED` and returns `ExecutionResult("rejected", ...)`.
- Produces: `TradeIntent.risk_snapshot["broker_account_number"]: str | None`, the account the intent was approved on.

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

Run: `PYTEST python_tests/test_execution_gateway.py -k reread_failure_closes -q`
Expected: FAIL, `assert 'skipped' == 'rejected'`

- [ ] **Step 3: Implement.** Add this method to `ExecutionGateway`, directly above `_recover_unknown_submission`:

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

Route the `AccountingEpochPending` rejection a few lines lower through the helper: `return await self._reject_before_submission(approved, str(exc))`. In the `risk_snapshot = {...}` dict, directly after the `"max_hold_days": ...` entry, add:

```python
                # The account this intent was approved on. Stranded-intent
                # recovery may only close the intent after a definitive
                # not-found from this same account.
                "broker_account_number": account.account_number,
```

- [ ] **Step 4: Run the gateway tests and verify they pass**

Run: `PYTEST python_tests/test_execution_gateway.py -q`
Expected: exit 0. If an existing test asserts `"skipped"` together with `BROKER_EXIT_QUANTITY_CHANGED`, change it to `"rejected"`: the old expectation encoded the defect.

- [ ] **Step 5: Commit** (message: `fix: close intents that stop before broker submission; record approving account (Rev.115 part 1)`)

### Task 2: Recover stranded pre-submission intents with a lease fence and an atomic conditional commit

**Files:**
- Modify: `tradepulse/reconciliation/coordinator.py`. Add `STRANDED_INTENT_GRACE_SECONDS`, `_recover_stranded_intents`, `_resolve_stranded` and `_commit_stranded`; call the sweep immediately before `await _recover_inflight_orders(...)` in `run_reconciliation`; add `TradeIntentStatus` to the `tradepulse.models` import list.
- Test: `python_tests/test_stranded_intents.py` (create)

**Interfaces:**
- Consumes: `reserve_symbol_for_execution`, `release_symbol_reservation`, `execution_lock_key`, `SYMBOL_LOCK_TTL_SECONDS` (from `tradepulse.execution`); `run_with_lock_renewal(database, key, owner, ttl, work, *, on_renewal_failed)` (from `tradepulse.persistence`; it does **not** cancel `work` on lease loss); `broker.get_account()`; `broker.get_order_by_client_order_id(str) -> AlpacaOrderResponse | None`.
- Produces: `_recover_stranded_intents(repositories, broker, alerts, now, lease_lost=None, *, lock_ttl_seconds=None, reconcile_lease=None) -> int` (the number resolved). `reconcile_lease` is `(lock_key, owner_token)` of the caller's reconciliation lease, or None.
- Produces: `run_reconciliation(..., reconcile_lease: tuple[str, str] | None = None)`, which forwards it. `cli._run_reconcile` passes `(RECONCILE_LOCK_KEY, owner_token)`.

- [ ] **Step 1: Write the failing tests** (`python_tests/test_stranded_intents.py`)

```python
"""Rev.115 F1: resolve intents stranded between RISK_APPROVED and broker acceptance."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_settlement_engine import _no_op_alerter, _repositories
from tradepulse.execution import has_in_flight_intent, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    IntegrityHold,
    IntegrityHoldType,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import hydrate
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


async def _payload(repositories):
    return (await repositories.trade_intents.get("ti-1"))["payload"]


def _hold(order_id, intent_id):
    return IntegrityHold(broker_order_id=order_id, trade_intent_id=intent_id,
                         hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED, reason="INTEGRITY_VIOLATION: disputed",
                         created_at=NOW)


async def test_sweep_closes_intent_alpaca_never_received(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert (await _payload(repositories))["status"] == "rejected"
    assert await has_in_flight_intent(repositories, BTC) is False
    broker.get_order_by_client_order_id.assert_awaited_once_with("ti-1")


async def test_sweep_adopts_matching_order_alpaca_did_receive(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = _order()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    payload = await _payload(repositories)
    assert (payload["status"], payload["broker_order_id"]) == ("accepted", "order-9")


async def test_sweep_refuses_when_account_identity_is_unproven(tmp_path):
    for recorded, current in ((None, "PA1"), ("PA1", "PA2")):
        repositories = await _repositories(tmp_path / f"{recorded}-{current}")
        await _stranded(repositories, account=recorded)
        broker = _broker(account_number=current)
        broker.get_order_by_client_order_id.return_value = None
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "risk_approved"
        broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_refuses_to_adopt_an_order_that_does_not_match(tmp_path):
    for i, mismatch in enumerate(({"symbol": "ETH/USD"}, {"side": Side.BUY}, {"raw": {"client_order_id": "someone-else"}})):
        repositories = await _repositories(tmp_path / f"case-{i}")
        await _stranded(repositories)
        broker = _broker()
        broker.get_order_by_client_order_id.return_value = _order(**mismatch)
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "risk_approved"


async def test_sweep_lookup_error_leaves_intent_unchanged(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.side_effect = RuntimeError("503")
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"
    records = [r["payload"] for r in await repositories.reconciliation_records.list_all()]
    assert [r["outcome"] for r in records] == ["drift_detected"]


async def test_sweep_ignores_young_intent(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, age=timedelta(seconds=30))
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_skips_asset_with_live_reservation(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    assert await reserve_symbol_for_execution(repositories.trade_intents.database, BTC, "live-gateway")
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_hold_on_the_recovered_intent_or_adopted_order_blocks_recovery(tmp_path):
    for i, (order_id, intent_id) in enumerate((("unrelated-key", "ti-1"), ("order-9", "other"))):
        repositories = await _repositories(tmp_path / f"hold-{i}")
        await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
        hold = _hold(order_id, intent_id)
        await repositories.integrity_holds.create_once(order_id, hold, status=hold.hold_type.value)
        before = await repositories.integrity_holds.get(order_id)
        broker = _broker()
        broker.get_order_by_client_order_id.return_value = _order()
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "submitted"
        assert await repositories.integrity_holds.get(order_id) == before


async def test_sweep_preserves_unrelated_holds_and_session_latch(tmp_path):
    from tradepulse.risk import latch_financial_integrity_block, load_session

    repositories = await _repositories(tmp_path)
    await latch_financial_integrity_block(repositories, "pre-existing latch", clock=lambda: NOW)
    hold = _hold("order-1", "other")
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)
    hold_before, session_before = await repositories.integrity_holds.get("order-1"), await load_session(repositories)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert await repositories.integrity_holds.get("order-1") == hold_before
    assert await load_session(repositories) == session_before


async def test_lease_lost_during_slow_lookup_writes_nothing(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)

    async def renewal_fails(*args, **kwargs):
        return False

    monkeypatch.setattr("tradepulse.persistence.lock.renew_lock", renewal_fails)
    broker = _broker()

    async def slow_lookup(client_order_id):
        await asyncio.sleep(1.5)  # outlives one heartbeat: max(ttl/3, 1) = 1 s at ttl 3
        return None

    broker.get_order_by_client_order_id.side_effect = slow_lookup
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW, lock_ttl_seconds=3) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"
    assert await repositories.reconciliation_records.list_all() == []  # fenced: not even a drift record


async def test_same_status_payload_change_is_never_overwritten(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()

    async def lookup_while_payload_changes(client_order_id):
        current = hydrate("trade_intents", (await repositories.trade_intents.get("ti-1"))["payload"])
        changed = replace(current, requested_quantity=Decimal(2))  # same status, different content
        await repositories.trade_intents.update("ti-1", changed, status=changed.status.value)
        return None

    broker.get_order_by_client_order_id.side_effect = lookup_while_payload_changes
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    payload = await _payload(repositories)
    assert (payload["status"], payload["requested_quantity"]) == ("submitted", "2")


async def test_stolen_reservation_is_refused_inside_the_commit(tmp_path):
    from tradepulse.execution import execution_lock_key

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    database = repositories.trade_intents.database

    async def lookup_while_reservation_is_stolen(client_order_id):
        # The renewal callback has not fired yet; only the commit's own check can catch this.
        await database.run(lambda c: c.execute("UPDATE locks SET owner_token='thief' WHERE lock_key=?",
                                                (execution_lock_key(BTC),)), write=True)
        return None

    broker.get_order_by_client_order_id.side_effect = lookup_while_reservation_is_stolen
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_lost_parent_reconcile_lease_is_refused_inside_the_commit(tmp_path):
    from tradepulse.persistence import acquire_lock

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    database = repositories.trade_intents.database
    assert await acquire_lock(database, "reconcile", "someone-else", "reconcile", 600)  # not our lease
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW,
                                           reconcile_lease=("reconcile", "our-token")) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_concurrent_status_change_is_never_overwritten(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()

    async def lookup_while_gateway_advances(client_order_id):
        current = hydrate("trade_intents", (await repositories.trade_intents.get("ti-1"))["payload"])
        advanced = replace(current, status=TradeIntentStatus.ACCEPTED, broker_order_id="order-live")
        await repositories.trade_intents.update("ti-1", advanced, status=advanced.status.value)
        return None  # a "not found" that is stale relative to the competitor's write

    broker.get_order_by_client_order_id.side_effect = lookup_while_gateway_advances
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    payload = await _payload(repositories)
    assert (payload["status"], payload["broker_order_id"]) == ("accepted", "order-live")
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_stranded_intents.py -q`
Expected: FAIL, `ImportError: cannot import name '_recover_stranded_intents'`

- [ ] **Step 3: Implement** (in `tradepulse/reconciliation/coordinator.py`, directly above `_recover_inflight_orders`)

```python
STRANDED_INTENT_GRACE_SECONDS = 120
_STRANDED_STATUSES = (TradeIntentStatus.RISK_APPROVED, TradeIntentStatus.SUBMITTED)


async def _recover_stranded_intents(repositories, broker, alerts, now, lease_lost=None, *, lock_ttl_seconds=None,
                                    reconcile_lease=None) -> int:
    """Intents approved or marked submitted but never given a broker order id.

    A crash or early return between RISK_APPROVED and broker acceptance leaves
    an intent has_in_flight_intent treats as in flight forever, blocking every
    later order on its asset -- protective exits included. A missing broker id
    proves nothing about submission. Each candidate is re-read and resolved
    under the asset's execution reservation, held and renewed throughout. A
    lost lease stops further work, and the commit transaction re-verifies both
    the reservation and the parent reconciliation lease (owner token and
    expiry) before a conditional, full-payload-compared write. The grace
    period only keeps the sweep away from fresh intents -- it is not a
    finality proof.
    """
    from tradepulse.execution import (
        SYMBOL_LOCK_TTL_SECONDS, execution_lock_key, release_symbol_reservation, reserve_symbol_for_execution,
    )
    from tradepulse.persistence import run_with_lock_renewal

    ttl = lock_ttl_seconds or SYMBOL_LOCK_TTL_SECONDS
    database = repositories.trade_intents.database
    rows = await list_all_by_statuses(repositories.trade_intents, [status.value for status in _STRANDED_STATUSES])
    resolved = 0
    for row in rows:
        if lease_lost is not None and lease_lost.is_set():
            break
        candidate = hydrate("trade_intents", row["payload"])
        if candidate.broker_order_id or (now - candidate.created_at).total_seconds() < STRANDED_INTENT_GRACE_SECONDS:
            continue
        token = str(uuid4())
        if not await reserve_symbol_for_execution(database, candidate.asset, token):
            continue  # a live execution owns this asset -- never race it
        fence = asyncio.Event()

        async def on_lost(fence=fence):
            fence.set()

        leases = [(execution_lock_key(candidate.asset), token), *([reconcile_lease] if reconcile_lease else [])]
        try:
            if await run_with_lock_renewal(
                database, execution_lock_key(candidate.asset), token, ttl,
                _resolve_stranded(repositories, broker, alerts, candidate.trade_intent_id, now, fence, leases),
                on_renewal_failed=on_lost,
            ):
                resolved += 1
        finally:
            await release_symbol_reservation(database, candidate.asset, token)
    return resolved


async def _resolve_stranded(repositories, broker, alerts, trade_intent_id, now, fence, leases) -> bool:
    """Re-read under the reservation, prove account and order identity, then commit conditionally."""
    async def unresolved(intent, reason: str) -> bool:
        if not fence.is_set():  # a lost lease writes nothing at all, not even evidence
            await _record(repositories, reconciliation_type="order", subject_id=trade_intent_id,
                          outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={"stranded_intent_resolved": True},
                          actual={"error": reason, "status": intent.status.value}, occurred_at=now)
        return False

    row = await repositories.trade_intents.get(trade_intent_id)
    if row is None:
        return False
    original_payload = row["payload"]
    intent = hydrate("trade_intents", original_payload)
    if (intent.status not in _STRANDED_STATUSES or intent.broker_order_id
            or (now - intent.created_at).total_seconds() < STRANDED_INTENT_GRACE_SECONDS):
        return False  # resolved or advanced by someone else since the scan -- nothing to do
    approved_on = intent.risk_snapshot.get("broker_account_number")
    try:
        account = await broker.get_account()
        if approved_on is None or account.account_number != approved_on:
            return await unresolved(intent, "ACCOUNT_IDENTITY_UNPROVEN")
        order = await broker.get_order_by_client_order_id(intent.trade_intent_id)
    except Exception as exc:  # noqa: BLE001 - an unavailable lookup proves nothing; retry next pass
        return await unresolved(intent, str(exc))
    if order is not None and (order.raw.get("client_order_id") != intent.trade_intent_id
                              or order.symbol != intent.asset.symbol or order.side != intent.side):
        return await unresolved(intent, "STRANDED_ORDER_IDENTITY_MISMATCH")
    if fence.is_set():
        return False  # lease lost while waiting on the broker: ownership is unproven, write nothing
    if order is not None:
        updated = replace(intent, status=TradeIntentStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                          client_order_id=intent.trade_intent_id)
        action = "adopted the broker order Alpaca holds for this client_order_id"
    else:
        updated = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="STRANDED_BEFORE_SUBMISSION")
        action = "closed: Alpaca returned a definitive not-found for this client_order_id"
    record = ReconciliationRecord(
        str(uuid4()), "order", intent.trade_intent_id, ReconciliationOutcome.CORRECTED,
        expected={"stranded_intent_resolved": True},
        actual={"previous_status": intent.status.value, "status": updated.status.value,
                "broker_order_id": updated.broker_order_id, "account_number": approved_on},
        occurred_at=now, corrective_action=action,
    )
    outcome = await repositories.trade_intents.database.run(
        lambda connection: _commit_stranded(connection, original_payload, intent, updated, record, now, leases),
        write=True)
    if outcome != "committed":
        return await unresolved(intent, outcome)
    await alerts.send("warning", f"Stranded {intent.status.value} intent for {intent.asset.symbol} resolved: {action}",
                      {"trade_intent_id": intent.trade_intent_id})
    return True


def _commit_stranded(connection, original_payload, intent, updated, record, now, leases) -> str:
    """One BEGIN IMMEDIATE transaction. Every lease this decision relies on must
    still be ours and unexpired; the stored payload must equal the re-read one
    in full; and no integrity hold may reference the intent or the adopted
    order."""
    from datetime import UTC, datetime as _datetime

    from tradepulse.persistence.codec import decode_payload, encode_payload

    wall_clock = _datetime.now(UTC).isoformat()
    for lock_key, owner_token in leases:
        lock = connection.execute("SELECT owner_token, expires_at FROM locks WHERE lock_key=?", (lock_key,)).fetchone()
        if lock is None or lock["owner_token"] != owner_token or lock["expires_at"] <= wall_clock:
            return "STRANDED_RESERVATION_LOST"
    row = connection.execute("SELECT status, payload FROM trade_intents WHERE record_id=?",
                             (intent.trade_intent_id,)).fetchone()
    if row is None or row["status"] != intent.status.value or decode_payload(row["payload"]) != original_payload:
        return "STRANDED_INTENT_CHANGED_CONCURRENTLY"
    held = connection.execute(
        "SELECT 1 FROM integrity_holds WHERE json_extract(payload,'$.trade_intent_id')=? OR record_id=?",
        (intent.trade_intent_id, updated.broker_order_id or ""),
    ).fetchone()
    if held:
        return "STRANDED_INTENT_UNDER_INTEGRITY_HOLD"
    connection.execute("UPDATE trade_intents SET status=?, payload=?, updated_at=? WHERE record_id=?",
                       (updated.status.value, encode_payload(updated), now.isoformat(), intent.trade_intent_id))
    connection.execute("INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)",
                       (record.record_id, encode_payload(record), now.isoformat()))
    return "committed"
```

The lease check uses wall-clock time, not the reconciliation's `now`, because lock expiry is wall-clock. `repositories.trade_intents.get` returns the decoded payload, so `original_payload` and `decode_payload(row["payload"])` are directly comparable. Confirm this against `persistence/repositories.py::get` before implementing.

In `run_reconciliation`, add the keyword parameter `reconcile_lease: tuple[str, str] | None = None` and insert this immediately before `await _recover_inflight_orders(repositories, broker, settlement, alerts, now, lease_lost)`:

```python
    await _recover_stranded_intents(repositories, broker, alerts, now, lease_lost, reconcile_lease=reconcile_lease)
```

In `cli._run_reconcile`, pass `reconcile_lease=(RECONCILE_LOCK_KEY, owner_token)` to its `run_reconciliation(...)` call. Other callers (`reset-integrity`, the verification reconcile action, the repair tool) pass nothing. They either hold their own exclusivity or run against a stopped runtime; record which in the Rev.115 note.

- [ ] **Step 4: Run the new tests and the reconciliation tests and verify they pass**

Run: `PYTEST python_tests/test_stranded_intents.py python_tests/test_reconciliation*.py -q`
Expected: exit 0. If the connection `database.run(..., write=True)` hands to the callable has no `sqlite3.Row` factory (check `persistence/database.py:195-220`), index the row by position instead of by column name.

- [ ] **Step 5: Commit** (message: `fix: lease-fenced, conditional recovery of stranded pre-submission intents (Rev.115 part 2)`)

### Task 3: Detect unprotected positions first, alert immediately

**Files:**
- Modify: `tradepulse/monitor/coordinator.py`. Add `unmanaged_positions: int = 0` as the **last** field of `MonitorCycleSummary` (after `error`, so existing positional constructors are unaffected), add `_report_unmanaged_position`, and add a first detection pass in `run_position_monitor` before any per-position exit work.
- Test: `python_tests/test_monitor_unmanaged.py` (create)

**Interfaces:**
- Produces: `MonitorCycleSummary.unmanaged_positions: int`. Audit event id format: `unmanaged_position:<asset_key>:<UTC date>`.

**Detection latency (documented in the Rev.115 note).** The scheduler waits 30 s *after* the previous cycle completes, and exits are processed sequentially. Detection therefore runs in a dedicated first pass, before any quote fetch, ratchet or exit. Worst-case latency is one previous cycle's duration plus 30 s, which the soak bounds at 120 s through the monitor gap limit (Task 5). Measured cycles: p99 19.7 s, max 22.5 s. The first sighting alerts immediately; later sightings that day are counted, not re-alerted.

- [ ] **Step 1: Write the failing tests** (`python_tests/test_monitor_unmanaged.py`)

```python
"""Rev.115 F5: a broker position with no local holding has no stop -- find it first, say so once a day."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_settlement_engine import _repositories
from tradepulse.broker import AlpacaPosition
from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Holding, asset_identity_key
from tradepulse.monitor.coordinator import run_position_monitor

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)


def _position(symbol="AAPL", qty="5", price="200"):
    q, p = Decimal(qty), Decimal(price)
    return AlpacaPosition(symbol, AssetClass.EQUITY, q, p, q * p, p, Decimal(0))


async def _run(repositories, broker, alerts, gateway=None):
    return await run_position_monitor(repositories, broker, AsyncMock(), gateway or AsyncMock(), alerts,
                                      risk_limits_for_profile("balanced"), clock=lambda: NOW)


async def test_unmanaged_position_alerts_once_per_day(tmp_path):
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position()]
    first, second = await _run(repositories, broker, alerts), await _run(repositories, broker, alerts)
    assert (first.unmanaged_positions, second.unmanaged_positions) == (1, 1)
    assert alerts.send.await_count == 1 and alerts.send.await_args.args[0] == "critical"
    assert [r["payload"]["event_type"] for r in await repositories.audit_events.list_all()] == ["unmanaged_broker_position"]


async def test_opening_inventory_at_opening_quantity_is_not_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position(qty="5")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 0
    alerts.send.assert_not_awaited()


async def test_opening_inventory_at_a_different_quantity_is_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position(qty="8")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 1


async def test_unmanaged_detection_precedes_exit_work(tmp_path):
    """MSFT is held locally and breached (exit work runs); AAPL is unmanaged.
    The AAPL alert must already be sent when the first exit call starts."""
    repositories = await _repositories(tmp_path)
    msft = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    await repositories.holdings.create_once(asset_identity_key(msft), Holding(
        msft, Decimal(5), Decimal(300), NOW, stop_loss=Decimal(250)))
    broker, alerts, gateway = AsyncMock(), AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position("MSFT", "5", "240"), _position()]
    alerts_sent_at_first_exit = []

    async def execute(request):
        alerts_sent_at_first_exit.append(alerts.send.await_count)
        return SimpleNamespace(status="rejected")

    gateway.execute_intent.side_effect = execute
    await _run(repositories, broker, alerts, gateway)
    assert alerts_sent_at_first_exit == [1]
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_monitor_unmanaged.py -q`
Expected: FAIL, `AttributeError: 'MonitorCycleSummary' object has no attribute 'unmanaged_positions'`

- [ ] **Step 3: Implement.** Add `AuditEvent` to the `tradepulse.models` import, then add the helper above `run_position_monitor`:

```python
async def _report_unmanaged_position(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                                     position: AlpacaPosition, now: datetime) -> None:
    """A broker position with no local holding has no stop, target or time
    stop. Alert on first sighting, then once per asset per UTC day: a
    deterministic audit event id makes create_once the deduplication."""
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

In `run_position_monitor`, directly after the lots pre-fetch block and **before** the `for position in positions:` exit loop, add the detection pass:

```python
    # Detection pass first: an unprotected position is reported before any
    # quote fetch, ratchet or (sequential, possibly slow) exit in this cycle.
    from tradepulse.reconciliation.membership import opening_quantities
    from tradepulse.verification.opening import load_bound_opening_checkpoint
    opening = opening_quantities(await repositories.trade_intents.database.run(load_bound_opening_checkpoint))
    unmanaged = 0
    for position in positions:
        key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
        if await repositories.holdings.get(key) is None and opening.get(key) != position.qty:
            unmanaged += 1
            await _report_unmanaged_position(repositories, alerts, position, clock())
```

Leave the exit loop's existing `if holding_row is None: continue` unchanged. Change the final return to `return MonitorCycleSummary("ok", len(positions), exits_triggered, execution_results, unmanaged_positions=unmanaged)`. The degraded return needs no change.

- [ ] **Step 4: Run the new tests and the existing monitor tests and verify they pass**

Run: `PYTEST python_tests/test_monitor_unmanaged.py python_tests/test_monitor_coordinator.py -q`
Expected: exit 0. If `create_once` returns `None` rather than `True` on insert, assert on the audit-event count delta instead.

- [ ] **Step 5: Commit** (message: `feat: detect and alert unprotected positions before exit work (Rev.115 part 3)`)

### Task 4: Every lane always starts; scan lanes idle until the session is active; plain `run` reconciles; `run --resume`

**Files:**
- Modify: `tradepulse/cli.py`:
  - add `SCAN_IDLE_POLL_SECONDS`, `_scan_lane_enabled`, `_should_activate` and `_reconcile_action`
  - gate `_scan_action` on the session
  - always add a reconcile lane in `_run_trading_supervisor`
  - start the full supervisor even when activation is refused (outside verification)
  - add `--resume`
- Test: `python_tests/test_run_protection.py` (create)

**Interfaces:**
- Produces: `SCAN_IDLE_POLL_SECONDS = 30`; `_scan_lane_enabled(session: TradingSession) -> bool`; `_should_activate(state: SessionState, *, resume: bool) -> bool`; `_run_application(settings, port, open_browser, *, verification=None, resume=False)`.

**Why.** Today a refused activation (risk stop, integrity block, broker unreachable at start) starts **no** lanes (the `start_result != 0` branch of `_run_application`), so a restart during a latch leaves open positions unprotected. Starting only the protective lanes would fix that but strand the scanners: a later `tradepulse start` could not create them without a restart. And starting the scanners unconditionally is not acceptable as-is either, because the scan cycle refuses only hard-blocked states (`scanner/coordinator.py:571`). In `MANUALLY_STOPPED` or `DISABLED` it would still call the AI and record failed scan runs.

So every lane always starts, and each scan lane checks the session at the top of every tick. Until the session is `ACTIVE` or `MARKET_CLOSED` with `trading_active`, it idles: no AI call, no market-data call, no scan record. It re-checks every `SCAN_IDLE_POLL_SECONDS`. `tradepulse start` therefore resumes scanning within 30 s, with no restart. Plain `run` also gains a reconcile lane, so recovery (including Task 2) runs outside a soak.

- [ ] **Step 1: Write the failing tests** (`python_tests/test_run_protection.py`)

```python
"""Rev.115: protection runs whenever the process runs; scanning follows the session; restarts never override an operator stop."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from test_settlement_engine import _repositories
from tradepulse import cli
from tradepulse.models import AssetClass, SessionState, TradingSession
from tradepulse.risk import save_session

NOW = datetime(2026, 10, 2, 14, tzinfo=UTC)


@pytest.mark.parametrize(("state", "resume", "expected"), [
    (SessionState.DISABLED, False, True), (SessionState.MANUALLY_STOPPED, False, True),
    (SessionState.ACTIVE, True, True), (SessionState.MARKET_CLOSED, True, True),
    (SessionState.MANUALLY_STOPPED, True, False), (SessionState.DISABLED, True, False),
    (SessionState.RISK_STOPPED, True, False), (SessionState.FINANCIAL_INTEGRITY_BLOCKED, True, False),
])
def test_resume_never_overrides_an_operator_or_safety_stop(state, resume, expected):
    assert cli._should_activate(state, resume=resume) is expected


async def _tick(repositories, monkeypatch):
    leg = AsyncMock()
    monkeypatch.setattr(cli, "_run_scan_leg", leg)
    ai = AsyncMock()
    wait = await cli._scan_action(AssetClass.CRYPTO, 600, repositories.trade_intents.database, repositories, ai,
                                  AsyncMock(), AsyncMock(), AsyncMock(), None, AsyncMock(), None)
    return wait, leg, ai


@pytest.mark.parametrize("state", [SessionState.MANUALLY_STOPPED, SessionState.DISABLED,
                                   SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED])
async def test_scan_lane_idles_without_any_work_while_session_inactive(tmp_path, monkeypatch, state):
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", state, False, NOW))
    wait, leg, ai = await _tick(repositories, monkeypatch)
    assert wait == cli.SCAN_IDLE_POLL_SECONDS
    leg.assert_not_awaited()
    ai.scan_candidates.assert_not_awaited()
    assert await repositories.scan_runs.list_all() == []


async def test_operator_start_resumes_scanning_without_restart(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.MANUALLY_STOPPED, False, NOW))
    _, idle_leg, _ = await _tick(repositories, monkeypatch)
    idle_leg.assert_not_awaited()
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))  # what `start` persists
    wait, leg, _ = await _tick(repositories, monkeypatch)
    leg.assert_awaited_once()
    assert wait == 600


async def test_supervisor_starts_every_lane_including_plain_reconcile(monkeypatch):
    started = []

    async def fake_lane(name, factory, repositories, alerts, shutdown, sleep):
        started.append(name)

    monkeypatch.setattr(cli, "_supervised_lane", fake_lane)
    await cli._run_trading_supervisor(None, None, None, None, None, None, None, None, None, asyncio.Event(), None)
    assert sorted(started) == ["crypto", "equity", "monitor", "option", "reconcile", "settle"]
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_run_protection.py -q`
Expected: FAIL, `AttributeError: module 'tradepulse.cli' has no attribute '_should_activate'`

- [ ] **Step 3: Implement.** Near the lane constants in `cli.py` (`RECONCILE_INTERVAL_SECONDS = 60` is temporary until Task 5 re-points it at the authority):

```python
RECONCILE_INTERVAL_SECONDS = 60
SCAN_IDLE_POLL_SECONDS = 30
_RESUMABLE_STATES = frozenset({SessionState.ACTIVE, SessionState.MARKET_CLOSED})


def _scan_lane_enabled(session: TradingSession) -> bool:
    """Scanning (AI calls, market data, new exposure) follows the session.
    Every other lane only protects or accounts for existing exposure and runs
    regardless -- protective exits are allowed in every halted state
    (risk/session.py::execution_session_decision)."""
    return session.state in _RESUMABLE_STATES and session.trading_active


def _should_activate(state: SessionState, *, resume: bool) -> bool:
    """`run --resume` (the supervised service) never activates a session an
    operator stopped or a safety latch halted -- it only continues trading
    that was already on. Interactive `run` keeps its existing behavior; the
    hard-blocked states are still refused by _run_start itself."""
    return state in _RESUMABLE_STATES if resume else True


async def _reconcile_action(settings: Settings) -> float:
    await _run_reconcile(settings)
    return RECONCILE_INTERVAL_SECONDS
```

Add `TradingSession` to the `tradepulse.models` import if absent. Make this the first statement of `_scan_action`'s body, before the market-clock check:

```python
    if not _scan_lane_enabled(await load_session(repositories)):
        return SCAN_IDLE_POLL_SECONDS  # idle: no AI call, no market data, no scan record
```

In `_run_trading_supervisor`, replace the `if verification_enabled:` reconcile block with:

```python
    lanes["reconcile"] = (
        (lambda: _periodic_loop(lambda: _verification_reconcile_action(settings, repositories, broker),
                                shutdown, sleep, **heartbeat('reconcile')))
        if verification_enabled else
        (lambda: _periodic_loop(lambda: _reconcile_action(settings), shutdown, sleep))
    )
```

In `_run_application`, add the keyword parameter `resume: bool = False` and replace the activation `if/else` with:

```python
        if current_session.state == SessionState.MARKET_CLOSED:
            logger.info("run_session_already_active_market_closed", extra={"event": "run_session_already_active_market_closed"})
            start_result = 0
        elif not _should_activate(current_session.state, resume=resume):
            logger.warning("run_resume_session_not_active", extra={"event": "run_resume_session_not_active",
                                                                    "state": current_session.state.value})
            start_result = 1
        else:
            start_result = await _run_start(settings)
```

Change the supervisor launch so that **outside a verification generation it starts regardless of `start_result`**: protection runs, and scan lanes idle until the session is active. Inside a verification generation, keep today's behavior (a refused start shuts the run down; the soak runner owns retries). Concretely:
- move the existing `else:` branch's `trading_task = asyncio.create_task(_run_trading_supervisor(...))` so it runs when `start_result == 0 or verification is None`
- keep the `logger.error("run_session_activation_failed", ...)` line when `start_result != 0`
- replace the old comment with one stating the new rule

Add `run_parser.add_argument("--resume", action="store_true", help="supervised restart: continue only an already-active session; never re-activate a stopped or latched one")` and pass `resume=args.resume` into both `_run_application` call sites.

- [ ] **Step 4: Add the wiring test** (append to `python_tests/test_run_protection.py`). It proves a refused activation still starts the supervisor and keeps the process up. Drive the real `_run_application`, monkeypatching only collaborators:
  - `cli._run_start` returns 1
  - `cli._run_trading_supervisor` records that it was called and then sets the shutdown event (its 10th positional argument)
  - the dashboard-server builder returns a stub whose `serve()` returns once `should_exit` is set

  Read `_run_application` (`cli.py:865-990`) for the exact collaborators; never stub its branches. Assert that the supervisor was started and that `_run_application` returns 0.

- [ ] **Step 5: Run the new tests and the CLI tests and verify they pass**

Run: `PYTEST python_tests/test_run_protection.py python_tests/test_cli.py -q`
Expected: exit 0. If an existing CLI test asserts that a refused activation starts no supervisor task, change it to assert that the supervisor starts and that scan lanes idle: the old expectation encoded the protection gap.

- [ ] **Step 6: Commit** (message: `fix: lanes always start, scans follow the session, plain run reconciles, run --resume (Rev.115 part 4)`)

### Task 5: One lane-interval authority, explicit gap limits, corrected comments, ship Rev.115

**Files:**
- Create: `tradepulse/config/lanes.py`
- Modify: `tradepulse/cli.py` (interval constants from the authority); `tradepulse/verification/soak.py` (`REQUIRED_LANES` and the `_lane_evidence` gap limits); `tradepulse/monitor/coordinator.py:150-157` and `:171-175` (comments)
- Test: `python_tests/test_accounting_soak.py` (append)
- Create: `docs/rev115-position-protection-liveness.md`

- [ ] **Step 1: Write the failing tests** (append to `python_tests/test_accounting_soak.py`; add `from datetime import UTC, datetime, timedelta` to its imports if absent)

```python
def test_soak_lane_criteria_match_runtime_intervals():
    from tradepulse import cli
    from tradepulse.config.lanes import LANE_INTERVAL_SECONDS
    from tradepulse.verification.soak import REQUIRED_LANES

    assert REQUIRED_LANES == LANE_INTERVAL_SECONDS
    assert (cli.EQUITY_SCAN_INTERVAL_SECONDS, cli.CRYPTO_SCAN_INTERVAL_SECONDS, cli.OPTION_SCAN_INTERVAL_SECONDS,
            cli.MONITOR_INTERVAL_SECONDS, cli.SETTLE_INTERVAL_SECONDS, cli.RECONCILE_INTERVAL_SECONDS) == tuple(
        LANE_INTERVAL_SECONDS[k] for k in ("equity", "crypto", "option", "monitor", "settle", "reconcile"))
    assert LANE_INTERVAL_SECONDS["monitor"] == 30


def test_monitor_stall_over_two_minutes_breaks_continuity():
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

- [ ] **Step 2: Run both and verify they fail**

Run: `PYTEST python_tests/test_accounting_soak.py -k "lane_criteria or monitor_stall" -q`
Expected: 2 FAIL, `ModuleNotFoundError: No module named 'tradepulse.config.lanes'`

- [ ] **Step 3: Implement.** Create `tradepulse/config/lanes.py`:

```python
"""The single authority for supervised lane cadence and soak continuity.

The runtime schedules each lane at LANE_INTERVAL_SECONDS and the soak's
lane-continuity evidence (verification/soak.py) bounds silence with
LANE_MAX_GAP_SECONDS, so the two can never drift apart. A gap is one cycle's
duration plus the interval: the heartbeat fires at cycle end, and the next
cycle starts one interval after the previous one completes.
"""
LANE_INTERVAL_SECONDS = {"equity": 900, "crypto": 600, "option": 1200, "monitor": 30, "settle": 60, "reconcile": 60}

# Scan, settlement and reconciliation keep the historical 2 x interval + 120 s.
# The monitor (software-evaluated stops, see the Phase 4 broker-side stop spec)
# gets 30 s + a 90 s cycle budget: 4x the longest cycle observed (22.5 s across
# 676 cycles in two soaks) and several 20 s fill waits. Any protection stall
# over two minutes fails continuity. A cycle with four or more simultaneous slow
# exits could exceed it; that fails closed, and the soak is re-run.
LANE_MAX_GAP_SECONDS = {lane: 2 * interval + 120 for lane, interval in LANE_INTERVAL_SECONDS.items()}
LANE_MAX_GAP_SECONDS["monitor"] = 120
```

In `tradepulse/cli.py`, replace the five interval constants and Task 4's temporary `RECONCILE_INTERVAL_SECONDS` with:

```python
from tradepulse.config.lanes import LANE_INTERVAL_SECONDS

EQUITY_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["equity"]
CRYPTO_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["crypto"]
OPTION_SCAN_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["option"]
MONITOR_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["monitor"]
SETTLE_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["settle"]
RECONCILE_INTERVAL_SECONDS = LANE_INTERVAL_SECONDS["reconcile"]
```

If `_verification_reconcile_action` returns a literal 60, return `RECONCILE_INTERVAL_SECONDS` instead. In `tradepulse/verification/soak.py`:
- Import `LANE_INTERVAL_SECONDS, LANE_MAX_GAP_SECONDS` from `tradepulse.config.lanes`.
- Set `REQUIRED_LANES = LANE_INTERVAL_SECONDS`.
- In `_lane_evidence`, replace both occurrences of `2 * interval + 120` with `LANE_MAX_GAP_SECONDS[lane]`.
- Leave `maximum_gap = 2 * REQUIRED_LANES["reconcile"] + 120` in the market-session check unchanged. It bounds broker-clock receipts, not lane silence.

- [ ] **Step 4: Correct the stale comments.** In `_fetch_atr`'s docstring, replace the sentence beginning "an unguarded fetch here would propagate out of this module" through "for the rest of the run." with:

```text
an unguarded fetch here would propagate out of this module and out of
    _periodic_loop, failing the whole monitor lane; cli.py::_supervised_lane
    restarts it with capped backoff, but every position would go unchecked
    until then.
```

In the defense-in-depth comment, replace "since _supervised_lane never restarts a lane after an unhandled exception, this one matters enough" with "since a lane failure leaves every position unchecked until _supervised_lane's backoff restart, this one matters enough".

- [ ] **Step 5: Run both Step 1 tests and verify they pass**

Run: `PYTEST python_tests/test_accounting_soak.py -k "lane_criteria or monitor_stall" -q`
Expected: 2 passed

- [ ] **Step 6: Write `docs/rev115-position-protection-liveness.md`.** Cover:
  - findings F1, F5 and F6, plus two findings from the plan reviews: no lanes at all after a refused activation, and no reconciliation in plain `run`; also the scan-lane idle rule
  - the exact reason strings: `BROKER_POSITIONS_UNAVAILABLE`, `BROKER_EXIT_QUANTITY_CHANGED`, `STRANDED_BEFORE_SUBMISSION`, `ACCOUNT_IDENTITY_UNPROVEN`, `STRANDED_ORDER_IDENTITY_MISMATCH`, `STRANDED_INTENT_CHANGED_CONCURRENTLY`, `STRANDED_INTENT_UNDER_INTEGRITY_HOLD`, `STRANDED_RESERVATION_LOST`, `UNMANAGED_POSITION`
  - the cadence and gap-limit table
  - the detection-latency bound from Task 3
  - the validation list

- [ ] **Step 7: Run the full suite**

Run: `PYTEST python_tests -q -p no:cacheprovider; echo exit=$?`
Expected: `exit=0`

- [ ] **Step 8: Commit** (message: `fix: position protection and execution liveness (Rev.115)`)

---

## Phase 2: Rev.116, risk and control plane

### Task 6: Cap the whole position, not each order

**Files:**
- Modify: `tradepulse/risk/engine.py`. Add `RiskEvalOptions.held_notional`, and add it to the max-position cap.
- Modify: `tradepulse/execution/gateway.py`. Pass `held_notional`.
- Test: `python_tests/test_risk_engine.py` (append)

**Interfaces:**
- Produces: `RiskEvalOptions.held_notional: Decimal = Decimal("0")`, the absolute broker `market_value` already held in this asset (multiplier included).

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
    assert evaluate_risk(option, _snapshot(), LIMITS, opts).approved_quantity == Decimal("2")  # (7000-6400)/(3x100)
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_risk_engine.py -k held -q`
Expected: FAIL, `TypeError: ... unexpected keyword argument 'held_notional'`

- [ ] **Step 3: Implement.** In `RiskEvalOptions`, add `held_notional: Decimal = Decimal("0")` directly after `held_quantity`. Replace the max-position block in `evaluate_risk` with:

```python
        # The cap bounds the whole position: notional already held in this
        # asset (broker market value, multiplier included) consumes it.
        max_position_notional = (limits.max_position_pct / 100) * total_equity - opts.held_notional
        if approved_qty * notional_per_unit > max_position_notional:
            approved_qty = _round_qty(max(max_position_notional, Decimal(0)) / notional_per_unit, intent.asset_class)
            reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_POSITION_PCT")
```

In `gateway.py`'s `RiskEvalOptions(...)` call, add:

```python
                held_notional=abs(held_position.market_value) if held_position is not None and request.side == Side.BUY else Decimal("0"),
```

- [ ] **Step 4: Run the risk, gateway and scanner tests and verify they pass**

Run: `PYTEST python_tests/test_risk_engine.py python_tests/test_execution_gateway.py python_tests/test_scanner_coordinator.py -q`
Expected: exit 0

- [ ] **Step 5: Commit** (message: `fix: max_position_pct caps the whole position (Rev.116 part 1)`)

### Task 7: Refuse cross-site and rebinding requests to the dashboard

**Files:**
- Modify: `tradepulse/web/app.py`. Add the `local_control_guard` middleware inside `create_app` after `app.state.tp = state`, the `_LOCAL_HOSTS` and `_CONTROL_HEADER` constants, and the imports `from urllib.parse import urlsplit` and `from fastapi.responses import JSONResponse`.
- Modify: `frontend/src/api.ts`, the `post()` headers.
- Modify: `python_tests/test_web_app.py:52`, the client fixture.
- Test: `python_tests/test_web_app.py` (append)

**Interfaces:**
- Produces: header contract `X-TradePulse-Control: 1` on every non-GET request; allowed hosts `127.0.0.1` and `localhost`.

- [ ] **Step 1: Write the failing tests** (append to `python_tests/test_web_app.py`)

```python
async def _raw_client(tmp_path, host="127.0.0.1"):
    state = await build_app_state(_settings(f"sqlite:///{tmp_path}/test.db"))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)), base_url=f"http://{host}:8766")


async def test_mutation_without_control_header_is_refused(tmp_path):
    response = await (await _raw_client(tmp_path)).post("/api/session/stop")
    assert response.status_code == 403 and response.json()["detail"] == "CONTROL_HEADER_REQUIRED"


async def test_cross_origin_mutation_is_refused_even_with_header(tmp_path):
    response = await (await _raw_client(tmp_path)).post(
        "/api/session/stop", headers={"X-TradePulse-Control": "1", "Origin": "https://evil.example"})
    assert response.status_code == 403 and response.json()["detail"] == "ORIGIN_NOT_ALLOWED"


async def test_rebound_host_is_refused_for_reads(tmp_path):
    assert (await (await _raw_client(tmp_path, host="attacker.example")).get("/api/session")).status_code == 403


async def test_local_reads_need_no_header_and_local_mutations_work(tmp_path):
    client = await _raw_client(tmp_path)
    assert (await client.get("/api/session")).status_code == 200
    response = await client.post("/api/session/stop", headers={"X-TradePulse-Control": "1", "Origin": "http://127.0.0.1:8766"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_web_app.py -k "control_header or cross_origin or rebound or local_reads" -q`
Expected: 3 FAIL (status 200 instead of 403), 1 PASS

- [ ] **Step 3: Implement the middleware** inside `create_app`, directly after `app.state.tp = state`:

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

Run: `PYTEST python_tests/test_web_app.py -q && (cd frontend && npm test -- --run)`
Expected: both exit 0. If a frontend test asserts the exact fetch init for a POST, add the header to its expectation.

- [ ] **Step 7: Write `docs/rev116-risk-and-control-plane.md`** (F4 and F2 finding, change, validation), run the full suite (expect `exit=0`), and commit: `fix: whole-position cap and dashboard request guard (Rev.116)`

---

## Phase 3: Rev.117, verification, operations and consistency

### Task 8: Soak preflight from the effective paper configuration, bound to the opening account

**Scope (stated verbatim in the Rev.117 note).** Alpaca exposes no fee-finality signal. The accounting policy (`reconciliation/activity_cursor.py` docstring) holds that "neither a wall-clock delay nor an ID cutoff establishes fee finality." The preflight is therefore a **preliminary screen**: it checks necessary conditions using explicit per-day fee evidence, and refuses when any is missing. Generation membership's fail-closed `unresolved_generation_membership` latch remains the **sufficient** backstop for any fee the preflight cannot see.

The screen examines the last **ten** ET days of fills. Fees for older trades have, in every observed case, posted the same evening; a residual older late fee is caught by the membership latch, never silently accepted. Every check and acknowledgement is written to an immutable `preflight.json`.

**Acknowledgements are narrow.** An acknowledged date must:
- be a valid `YYYYMMDD`
- fall strictly before today (ET) and inside the ten-day window
- be one of the fee-expected trade days the screen actually found

Anything else is refused as `ACKNOWLEDGEMENT_INVALID:<value>`. An acknowledgement only waives a missing fee **subtype**. It never waives `OPEN_BROKER_ORDERS`, `PRE_GENERATION_TRADE_TODAY`, `FEE_DAY_NOT_CLOSED`, or any integrity check.

**Files:**
- Modify: `scripts/run_accounting_soak.py`:
  - add `preflight()`, `_broker_preflight()` and `_verify_opening_account()`
  - add the `--acknowledge-fee-day` argument
  - add the calls in `run()`
- Modify: `python_tests/test_accounting_soak.py`. The `runner` fixture replaces `module._broker_preflight` and `module._verify_opening_account` with `AsyncMock()` before returning, so the existing runner tests make no broker calls.
- Test: `python_tests/test_accounting_soak.py` (append)

**Interfaces:**
- Produces: `preflight(broker, *, now=None, lookback_days=10, acknowledged=frozenset()) -> dict`, returning `{"problems": list[str], "fee_days": dict, "account": {"account_id", "account_number"}}`. The problems are `OPEN_BROKER_ORDERS`, `PRE_GENERATION_TRADE_TODAY`, `FEE_DAY_NOT_CLOSED:<YYYYMMDD>`, `FEE_EVIDENCE_MISSING:<YYYYMMDD>:<SUBTYPE>`, and `ACKNOWLEDGEMENT_INVALID:<value>`.

**Per-day fee evidence rule.** For each ET trade date in the last `lookback_days`:
- equity sells require a `FEE` row of subtype `REG` with that date's id prefix
- option fills require `OCC`

Any such date must also be **strictly before** today (ET), so at least one end-of-day batch has run since it. The operator may acknowledge a specific date with `--acknowledge-fee-day YYYYMMDD` after checking the account statement by hand (for example, a sell too small to produce a REG row). The acknowledgement is recorded in `preflight.json`.

- [ ] **Step 1: Write the failing tests** (append; add `from unittest.mock import AsyncMock`, `from types import SimpleNamespace` and `from datetime import UTC, datetime` to the imports if absent)

```python
def _preflight_broker(activities, open_orders=(), account_number="PA1"):
    broker = AsyncMock()
    broker.get_open_orders.return_value = list(open_orders)
    broker.get_activities.return_value = [SimpleNamespace(raw=a) for a in activities]
    broker.get_account.return_value = SimpleNamespace(account_id="acct-1", account_number=account_number)
    return broker


PREFLIGHT_NOW = datetime(2026, 10, 2, 14, tzinfo=UTC)  # 10:00 ET, 2026-10-02
OPTION_BUY_0929 = {"id": "20260929135313600::a", "activity_type": "FILL", "symbol": "IWM261030C00286000", "side": "buy"}
OCC_0929 = {"id": "20260929000000000::b", "activity_type": "FEE", "activity_sub_type": "OCC"}
SELL_0929 = {"id": "20260929094227714::c", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
REG_0929 = {"id": "20260929000000000::d", "activity_type": "FEE", "activity_sub_type": "REG"}


async def test_preflight_clear_with_complete_fee_evidence(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW)
    assert result["problems"] == []
    assert result["account"] == {"account_id": "acct-1", "account_number": "PA1"}


async def test_preflight_refuses_partial_fee_batch(runner):
    # OCC posted, but REG for the same day's sell not yet: a partial batch
    result = await runner.preflight(_preflight_broker([OCC_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["FEE_EVIDENCE_MISSING:20260929:REG"]


async def test_preflight_refuses_older_unresolved_fee_day(runner):
    old_sell = {"id": "20260924093005873::e", "activity_type": "FILL", "symbol": "GOOGL", "side": "sell"}
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929, old_sell]),
                                    now=PREFLIGHT_NOW)
    assert result["problems"] == ["FEE_EVIDENCE_MISSING:20260924:REG"]


async def test_preflight_refuses_a_day_whose_batch_cannot_have_run(runner):
    # a late receipt for today's fills cannot exist yet: today is refused even with a REG row present
    today_sell = {"id": "20261002094100000::f", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
    today_reg = {"id": "20261002000000000::g", "activity_type": "FEE", "activity_sub_type": "REG"}
    result = await runner.preflight(_preflight_broker([today_reg, today_sell]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["PRE_GENERATION_TRADE_TODAY", "FEE_DAY_NOT_CLOSED:20261002"]


async def test_preflight_acknowledged_day_is_recorded_not_refused(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW,
                                    acknowledged=frozenset({"20260929"}))
    assert result["problems"] == []
    assert result["fee_days"]["20260929"]["acknowledged"] is True


async def test_acknowledgement_never_waives_today_or_same_day_trading(runner):
    today_sell = {"id": "20261002094100000::f", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
    result = await runner.preflight(_preflight_broker([today_sell]), now=PREFLIGHT_NOW, acknowledged=frozenset({"20261002"}))
    assert result["problems"] == ["PRE_GENERATION_TRADE_TODAY", "FEE_DAY_NOT_CLOSED:20261002",
                                  "ACKNOWLEDGEMENT_INVALID:20261002"]


async def test_acknowledgement_of_an_unknown_or_malformed_day_is_refused(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW,
                                    acknowledged=frozenset({"20260930", "2026-09-29"}))
    assert result["problems"] == ["ACKNOWLEDGEMENT_INVALID:2026-09-29", "ACKNOWLEDGEMENT_INVALID:20260930"]


async def test_preflight_json_is_the_result_object(runner, tmp_path, monkeypatch):
    import json

    broker = _preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929])
    monkeypatch.setattr("tradepulse.session_commands.build_broker", lambda settings: broker)
    environment = {"TRADEPULSE_EXECUTION_MODE": "paper", "TRADEPULSE_LIVE_TRADING_ENABLED": "false",
                   "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}
    account = await runner._real_broker_preflight(environment, tmp_path, frozenset())
    stored = json.loads((tmp_path / "preflight.json").read_text())
    assert isinstance(stored, dict) and stored["problems"] == [] and stored["account"] == account


async def test_preflight_refuses_open_orders(runner):
    result = await runner.preflight(_preflight_broker([], open_orders=[object()]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["OPEN_BROKER_ORDERS"]


async def test_runner_refuses_opening_on_a_different_account(runner, tmp_path, monkeypatch):
    import pytest as _pytest

    from tradepulse.verification.integrity import canonical, digest

    checkpoint = {"account_identity_digest": digest(canonical({"account_id": "acct-2", "account_number": "PA2"}))}
    monkeypatch.setattr(runner, "load_opening_checkpoint", lambda database: checkpoint)
    with _pytest.raises(runner.VerificationError, match="soak_opening_account_mismatch"):
        await runner._verify_opening_account(tmp_path / "soak.db", {"account_id": "acct-1", "account_number": "PA1"})
```

The fixture stubs `_verify_opening_account` and `_broker_preflight`, so tests of those two must call the **real** functions. In the fixture, before stubbing, save `module._real_verify_opening_account = module._verify_opening_account` and `module._real_broker_preflight = module._broker_preflight`. Change the account-mismatch test above to call `runner._real_verify_opening_account(...)`.

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_accounting_soak.py -k "preflight or opening_on_a_different" -q`
Expected: FAIL, `AttributeError: module 'accounting_soak_runner' has no attribute 'preflight'`

- [ ] **Step 3: Implement** (in `scripts/run_accounting_soak.py`; add `from datetime import UTC, datetime` if absent, `from tradepulse.verification.opening import load_opening_checkpoint` at module level, and `write_once` to the existing `tradepulse.verification.integrity` import)

```python
_FEE_EVIDENCE = (("equity_sell", "REG"), ("option", "OCC"))


async def preflight(broker, *, now: datetime | None = None, lookback_days: int = 10,
                    acknowledged: frozenset[str] = frozenset()) -> dict:
    """Necessary opening conditions; the membership latch stays the sufficient backstop.

    Alpaca posts fee rows in an end-of-day batch with a midnight-of-trade-date
    id that sorts before the day's fills. A pre-generation fee that posts after
    the opening classifies as unresolved membership and latches the integrity
    block. Each recent trade day therefore needs explicit, expected fee
    evidence and a completed end-of-day boundary, unless the operator
    acknowledges that day after checking the account statement.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    now = now or datetime.now(UTC)
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    today = eastern.strftime("%Y%m%d")
    earliest = (eastern - timedelta(days=lookback_days)).strftime("%Y%m%d")
    account = await broker.get_account()
    problems = []
    if await broker.get_open_orders():
        problems.append("OPEN_BROKER_ORDERS")
    activities = [dict(a.raw) for a in await broker.get_activities(activity_type=None)]
    fills = [r for r in activities if r.get("activity_type") == "FILL" and r["id"][:8] >= earliest]
    if any(r["id"][:8] == today for r in fills):
        problems.append("PRE_GENERATION_TRADE_TODAY")
    fees = {(r["id"][:8], r.get("activity_sub_type")) for r in activities if r.get("activity_type") == "FEE"}
    fee_days: dict[str, dict] = {}
    for fill in fills:
        symbol, day = str(fill.get("symbol", "")), fill["id"][:8]
        kind = ("option" if len(symbol) > 12
                else "equity_sell" if fill.get("side") == "sell" and "/" not in symbol else None)
        if kind is not None:
            fee_days.setdefault(day, {"kinds": set(), "acknowledged": day in acknowledged})["kinds"].add(kind)
    for day in sorted(fee_days):
        entry = fee_days[day]
        if day >= today:  # never waivable: no end-of-day batch can have run yet
            problems.append("FEE_DAY_NOT_CLOSED:" + day)
            continue
        if entry["acknowledged"]:
            continue  # waives a missing fee subtype only, for a validated past day
        problems.extend(f"FEE_EVIDENCE_MISSING:{day}:{subtype}" for kind, subtype in _FEE_EVIDENCE
                        if kind in entry["kinds"] and (day, subtype) not in fees)
    valid_ack = {day for day in fee_days if earliest <= day < today}
    problems.extend(f"ACKNOWLEDGEMENT_INVALID:{value}" for value in sorted(acknowledged)
                    if not (len(value) == 8 and value.isdigit() and value in valid_ack))
    return {"problems": problems,
            "fee_days": {day: {"kinds": sorted(e["kinds"]), "acknowledged": e["acknowledged"]} for day, e in fee_days.items()},
            "account": {"account_id": account.account_id, "account_number": account.account_number}}


async def _broker_preflight(environment: dict, report_dir: Path, acknowledged: frozenset[str]) -> dict:
    """Build the broker from exactly the effective paper configuration the runtime receives."""
    from tradepulse.config import Settings
    from tradepulse.session_commands import build_broker

    settings = Settings.from_env(environment)
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise VerificationError("soak_preflight_requires_paper_configuration")
    broker = build_broker(settings)
    try:
        result = await preflight(broker, acknowledged=acknowledged)
    finally:
        await broker.aclose()
    await asyncio.to_thread(write_once, report_dir / "preflight.json", result)  # write_once serializes canonical JSON itself
    if result["problems"]:
        raise VerificationError("soak_preflight_refused:" + ",".join(result["problems"]))
    return result["account"]


async def _verify_opening_account(database: Path, account: dict) -> None:
    """The frozen opening checkpoint must be bound to the account the preflight inspected."""
    from tradepulse.verification.integrity import canonical, digest

    checkpoint = await asyncio.to_thread(load_opening_checkpoint, database)
    if checkpoint is None or checkpoint["account_identity_digest"] != digest(canonical(account)):
        raise VerificationError("soak_opening_account_mismatch")
```

`write_once(path, value)` serializes `value` as canonical JSON (`verification/integrity.py:83`), so pass the dict, never a pre-serialized string. In `run()`:
1. Call `_load_dotenv()` (imported from `tradepulse.cli`) **before** `environment = dict(os.environ)`, so the effective configuration includes `.env`.
2. As the first statement inside `try:`, call `account = await _broker_preflight(environment, report.parent, frozenset(args.acknowledge_fee_day or ()))`.
3. Immediately after the freeze command succeeds, and before `_session(...)`, call `await _verify_opening_account(database, account)`.

In `parser()`, add `result.add_argument("--acknowledge-fee-day", action="append", metavar="YYYYMMDD", help="accept a trade day's fees as complete after checking the statement by hand; recorded in preflight.json")`.

- [ ] **Step 4: Run all soak tests and verify they pass**

Run: `PYTEST python_tests/test_accounting_soak.py -q`
Expected: exit 0

- [ ] **Step 5: Commit** (message: `feat: soak preflight from the effective paper configuration, bound to the opening account (Rev.117 part 1)`)

### Task 9: Make the two load-sensitive tests deterministic without losing concurrency coverage

**Files:**
- Modify: `python_tests/test_execution_gateway.py`, `test_concurrent_buys_for_different_symbols_serialize_through_portfolio_risk_lock`
- Modify: `python_tests/test_execution_idempotency.py`, `test_in_flight_detection_is_correct_behind_a_large_non_blocking_backlog`

- [ ] **Step 1: Coordinate two real, concurrent executions with a barrier.** Keep the test's quote, order, status and activity mocks. Replace its `_mock_account(...)` call and everything from `results = await asyncio.gather(...)` to the end with the block below. Build `account_json` by copying the exact JSON the file's `_mock_account` helper serves, with `cash="150000", equity="100000", last_equity="100000"`.

```python
    inside_lock, release = asyncio.Event(), asyncio.Event()

    async def account_route(request: httpx.Request) -> httpx.Response:
        # The first execution parks here -- inside the portfolio-risk lock -- until released.
        if not inside_lock.is_set():
            inside_lock.set()
            await release.wait()
        return httpx.Response(200, json=account_json)

    respx.get("https://paper-api.alpaca.markets/v2/account").mock(side_effect=account_route)

    first = asyncio.create_task(gateway.execute_intent(aapl_request))
    await asyncio.wait_for(inside_lock.wait(), timeout=10)
    second = await gateway.execute_intent(btc_request)  # genuinely concurrent: the first holds the lock now
    release.set()
    winner = await asyncio.wait_for(first, timeout=30)
    await broker.aclose()

    assert second.status == "skipped" and second.reasons == ["PORTFOLIO_RISK_EVALUATION_LOCKED"]
    assert winner.status == "filled"
    orders = [c.request for c in respx.calls if c.request.method == "POST" and c.request.url.path == "/v2/orders"]
    assert len(orders) == 1 and b'"AAPL"' in orders[0].content  # one broker submission
    intents = [row["payload"] for row in await repositories.trade_intents.list_all()]
    assert [(i["asset"]["symbol"], i["status"]) for i in intents] == [("AAPL", "filled")]  # one approval
```

If respx in this environment does not await coroutine `side_effect`s, monkeypatch `broker.get_account` with an async wrapper that applies the same barrier around the real call. Do not drop the barrier.

- [ ] **Step 2: Seed the backlog sequentially.** Replace `await asyncio.gather(*(_seed(i) for i in range(1100)))` with:

```python
    for i in range(1100):  # sequential: the property under test is backlog size, not write concurrency
        await _seed(i)
```

- [ ] **Step 3: Run both tests three times**

Run: `for i in 1 2 3; do PYTEST "python_tests/test_execution_gateway.py::test_concurrent_buys_for_different_symbols_serialize_through_portfolio_risk_lock" "python_tests/test_execution_idempotency.py::test_in_flight_detection_is_correct_behind_a_large_non_blocking_backlog" -q -p no:cacheprovider; echo exit=$?; done`
Expected: `exit=0` three times

- [ ] **Step 4: Commit** (message: `test: barrier-coordinated portfolio-lock concurrency; sequential backlog seeding (Rev.117 part 2)`)

### Task 10: Paper-enforced, resume-safe supervised runtime and runbook, then ship Rev.117

**Files:**
- Create: `deploy/tradepulse-run.service`
- Create: `docs/operations-runbook.md`
- Create: `docs/rev117-verification-operations-consistency.md`
- Test: `python_tests/test_service_unit.py` (create)

- [ ] **Step 1: Write the failing tests** (`python_tests/test_service_unit.py`)

```python
"""The supervised unit must enforce paper mode and never re-activate a stopped or latched session."""
from configparser import ConfigParser
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "tradepulse-run.service"


def test_unit_enforces_paper_and_resume():
    parser = ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNIT)
    service = parser["Service"]
    environment = service["Environment"].split()
    assert "TRADEPULSE_EXECUTION_MODE=paper" in environment
    assert "TRADEPULSE_LIVE_TRADING_ENABLED=false" in environment
    assert service["ExecStart"].split()[-3:] == ["run", "--no-browser", "--resume"]
    assert service["Restart"] == "on-failure"


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    from tradepulse.cli import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("TRADEPULSE_EXECUTION_MODE=live\nTRADEPULSE_LIVE_TRADING_ENABLED=true\n")
    monkeypatch.setenv("TRADEPULSE_EXECUTION_MODE", "paper")
    monkeypatch.setenv("TRADEPULSE_LIVE_TRADING_ENABLED", "false")
    _load_dotenv(env_file)
    import os
    assert (os.environ["TRADEPULSE_EXECUTION_MODE"], os.environ["TRADEPULSE_LIVE_TRADING_ENABLED"]) == ("paper", "false")
```

Restart behaviour after an operator stop, a risk stop and an integrity hold is covered by Task 4's `_should_activate`, scan-idle and resume-after-`start` tests, because the unit runs `run --resume`.

- [ ] **Step 2: Run them and verify they fail**

Run: `PYTEST python_tests/test_service_unit.py -q`
Expected: FAIL, `KeyError: 'Service'` (the unit does not exist yet). The dotenv test passes or fails depending on `_load_dotenv`'s current override behavior. If it fails, change `_load_dotenv` (`cli.py:1066`) to skip keys already present in `os.environ`, because the unit's paper enforcement depends on it.

- [ ] **Step 3: Write the unit** `deploy/tradepulse-run.service`:

```ini
[Unit]
Description=TradePulse trading runtime (paper only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/tradepulse-ai
# Process environment overrides .env: this unit can never trade live.
Environment=TRADEPULSE_EXECUTION_MODE=paper TRADEPULSE_LIVE_TRADING_ENABLED=false
# --resume: continue only an already-active session. An operator stop or a
# safety latch stays in force across restarts, while monitor, settlement and
# reconciliation keep protecting existing positions.
ExecStart=%h/tradepulse-ai/.venv/bin/tradepulse run --no-browser --resume
Restart=on-failure
RestartSec=30
KillSignal=SIGINT
TimeoutStopSec=900

[Install]
WantedBy=default.target
```

- [ ] **Step 4: Run them and verify they pass**

Run: `PYTEST python_tests/test_service_unit.py -q`
Expected: 2 passed

- [ ] **Step 5: Write `docs/operations-runbook.md`** with these sections, in full:
  1. **Host.** Protection runs only while the process runs. Use an always-on, mains-powered host. Install with `mkdir -p ~/.config/systemd/user && cp deploy/tradepulse-run.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now tradepulse-run && loginctl enable-linger $USER`. The first activation is always deliberate: run `tradepulse start`, since `--resume` never activates a stopped session. Scanning begins within 30 s of `start` (`SCAN_IDLE_POLL_SECONDS`). No service restart is needed.
  2. **Restart semantics.** After an operator stop, a risk stop or an integrity hold, a restart keeps trading off: scan lanes idle while monitor, settlement and reconciliation keep running. Clear the condition with the existing reset commands, then run `tradepulse start`; scanning resumes within 30 s, with no restart.
  3. **Starting a soak.** Give the exact `setsid nohup systemd-inhibit ... scripts/run_accounting_soak.py ...` command. Stop the service first (`systemctl --user stop tradepulse-run`), because a soak needs exclusive use of the account. Read `preflight.json`. Use `--acknowledge-fee-day` only after checking the statement by hand. Never trade the account, close opening inventory or edit `tradepulse/` during a run. The final reconciliation must land after the ~17:45 PT fee batch.
  4. **Stopping cleanly.** `kill -INT <runtime pid>` makes the soak runner write its report. For the service: `systemctl --user stop tradepulse-run`.
  5. **Secrets.** Rotate the Telegram bot token (earlier logs contained it). Never paste keys into agent sessions.
  6. **Repository hygiene.** Unlink the Base44 app from `Redchief-sudo/tradepulse-ai`, fetch before every push, and delete `backup/base44-bot-push-20260928` when it is no longer wanted.

- [ ] **Step 6: Write `docs/rev117-verification-operations-consistency.md`.** Cover F8, F7 and the operations finding: change and validation, plus Task 8's scope statement verbatim.

- [ ] **Step 7: Run the full suite**

Run: `PYTEST python_tests -q -p no:cacheprovider; echo exit=$?`
Expected: `exit=0`

- [ ] **Step 8: Commit** (message: `feat: account-bound soak preflight, deterministic tests, paper-only resume-safe service and runbook (Rev.117)`)

---

## Phase 4: Broker-side protective stops (design spec, not implemented here)

### Task 11: Write the broker-side stop design spec

**Files:**
- Create: `docs/superpowers/specs/<date>-broker-side-stops.md` (via `superpowers:brainstorming`)

- [ ] **Step 1: Run `superpowers:brainstorming` with the operator** to decide the following. Each decision must be recorded in the spec:
  1. **Which asset classes get a resting broker stop?** Alpaca accepts equity `stop` orders and crypto `stop_limit` orders. Option stop support must be verified against the live paper API before it is assumed.
  2. **How it is placed.** As a trade intent through `ExecutionGateway` (`client_order_id` = intent id), so a broker-triggered fill is a known order recovered by `_recover_inflight_orders`, never a "missed fill" that latches the integrity block.
  3. **Quantity reservation.** A resting stop holds shares, so a monitor exit must cancel it first. That needs a cancel-then-exit sequence and a rule for when the cancel is ambiguous.
  4. **Level.** A catastrophe level (e.g. the entry `stop_loss`, never ratcheted at the broker), or ratcheted with replace orders and the API-call cost of doing so.
  5. **Generation interaction.** Resting stop orders vs the soak's "no open orders" preflight rule and the opening checkpoint.
- [ ] **Step 2: Commit the spec**, then write its own implementation plan with `superpowers:writing-plans`.

## Expected ratings after Rev.115-117 (targets; they hold only after tests pass and a fresh soak confirms them)

| Area | Now | Target | Remaining dependency |
|---|---|---|---|
| Order execution | 7.5 | 9 | (none) |
| Position monitor and closing | 6 | 9 on an always-on host | Phase 4 removes the host dependency |
| Risk engine | 8 | 9 | (none) |
| Session control | 8.5 | 9 | (none) |
| Dashboard security | 5 | 9 | (none) |
| Verification and soak | 8 | 9 | (none) |
| Broker and data | 8 | 9 | (none) |
| Scanner and strategy | 7 | 9 for engineering | Trade frequency is strategy calibration, not code |
| Code and docs consistency | 7 | 9 | (none) |
| Operations | 6 | 9 once the runbook steps are done | Host choice and token rotation are operator actions |
| Accounting, reconciliation, persistence | 9 | 9 | (none) |
