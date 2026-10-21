# Phase A0: Broker Port, Simulated Broker and Conformance Kit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every broker call behind a `BrokerPort` interface, without changing Alpaca behaviour, and add a deterministic simulated broker and a shared conformance kit that every future broker adapter (IBKR first) must pass.

**Architecture:** `BrokerPort` is a `typing.Protocol` that `AlpacaClient` already satisfies structurally (it gains only a `venue` attribute). Modules annotate against the port instead of `AlpacaClient`. `SimulatedBroker` implements the same protocol in memory, producing Alpaca-shaped orders and activities, so the existing gateway, settlement, reconciliation and evidence code run against it unchanged. The conformance kit is one parametrized test suite run against the simulator always, and against a real paper account only when explicitly enabled.

**Tech Stack:** Python 3.12, asyncio, `typing.Protocol`, pytest with pytest-asyncio (`asyncio_mode = "auto"`), respx for HTTP mocking, Decimal for all money.

**Spec:** `docs/multi-broker-ibkr-design.md`, sections 3.1, 3.2, 3.4, 3.5 and 7 (Phase A0).

## Global Constraints

- **No placeholder or simplified logic.** Every behaviour the simulator models follows Alpaca's documented contract and the shapes `AlpacaClient` really parses: statuses, IDs, pagination, fee batching, error classes. A behaviour that cannot be modelled faithfully is left out and listed in the task report, never approximated silently. No `TODO`, no stubs returning fixed values, no tests that assert only that a call ran.

- **Alpaca behaviour stays byte-identical.** No change to request parameters, persisted payload shapes, IDs, asset keys or alert text. The full existing suite must pass unchanged, with no edits to existing test assertions.
- Existing asset keys stay identical: `asset_key_from_broker_symbol(asset_class, symbol)` keeps returning `f"{asset_class}:default:alpaca:{SYMBOL}"`.
- `SimulatedBroker` can never be built from `Settings` or `build_broker`. Tests construct it directly.
- **No order reaches a real broker** from the conformance kit unless `TRADEPULSE_CONFORMANCE_ORDERS=1` is set **and** no soak runtime or service is running (AGENTS.md: out-of-band orders trip the integrity lock).
- Paper only: the kit refuses any configuration where `execution_mode != "paper"`, or where live trading is enabled.
- Money and quantities are `Decimal` end to end. The simulator never rounds a quantity Alpaca would accept.
- Every task ends with the full suite green, `ruff check` adding no new findings in changed files, and a signed commit to the `phase-a0` worktree branch.

## Review Focus

1. **Running the order tests against Alpaca paper while a soak or the service is live** must refuse before any order is placed (Task 7).
2. **Activity IDs from the simulator** must be unique, and sort chronologically even for several fills in the same millisecond. Reconciliation pagination and generation membership depend on this (Task 5).
3. **A fill that `get_order` reports before its activity exists** (Alpaca's attribution lag) must be reproducible, so the gateway's lag path is exercised (Task 5).
4. **Fractional equity (thousandths) and crypto (8 dp) quantities** round-trip exactly through order, fill and position (Task 5).
5. **Existing databases keep their asset keys:** the venue parameter's default reproduces every current key (Task 2).

---

### Task 1: Broker port and neutral type names

**Files:**
- Create: `tradepulse/broker/port.py`
- Modify: `tradepulse/broker/alpaca_client.py` (add `venue = "alpaca"` class attribute), `tradepulse/broker/__init__.py` (export the port and the neutral names)
- Test: `python_tests/test_broker_port.py`

**Interfaces:**
- Produces, in `tradepulse/broker/port.py`:
  - Neutral aliases, as plain assignments: `BrokerAccount = AlpacaAccount`, `BrokerPosition = AlpacaPosition`, `BrokerClock = AlpacaClock`, `OrderRequest = AlpacaOrderRequest`, `OrderState = AlpacaOrderResponse`, `BrokerActivity = AlpacaActivity`, `OptionContract = AlpacaOptionContract`, `RateLimitSnapshot = AlpacaRateLimitSnapshot`.
  - `@runtime_checkable class BrokerPort(Protocol)` with attribute `venue: str` and exactly these members, with the signatures `AlpacaClient` has today:
    - account and positions: `get_account() -> BrokerAccount`, `get_positions() -> list[BrokerPosition]`, `get_clock() -> BrokerClock`;
    - orders: `place_order(order: OrderRequest) -> OrderState`, `get_order(broker_order_id: str) -> OrderState`, `get_order_by_client_order_id(client_order_id: str) -> OrderState | None`, `get_open_orders() -> list[OrderState]`, `cancel_order(broker_order_id: str) -> None`;
    - evidence: `get_activities(activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100, *, after_id: str | None = None, page_evidence: list | None = None) -> list[BrokerActivity]`;
    - market data: `get_latest_quote(symbol: str, asset_class: AssetClass, feed_override: str | None = None) -> RawQuote`, `get_latest_option_quotes(symbols: list[str]) -> dict[str, RawQuote]`, `get_options_chain(underlying_symbol: str, expiration_gte: str, expiration_lte: str) -> list[OptionContract]`, `get_bars(symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250) -> list[RawBar]`, `set_market_data_feeds(*, equity_feed: str, option_feed: str) -> None`;
    - other: `rate_limit_snapshot` (property, `RateLimitSnapshot | None`), `aclose() -> None`.

    All methods are async, except `set_market_data_feeds` and the property.

- [ ] **Step 1: Write the failing tests**
  - `test_alpaca_client_satisfies_the_port`: `isinstance(AlpacaClient("k", "s", "paper", 10), BrokerPort)` and `.venue == "alpaca"`.
  - `test_every_broker_call_in_the_code_is_on_the_port`: AST-scan every `.py` under `tradepulse/` except `tradepulse/broker/`.
    - Collect attribute names `X` from `broker.X` and `self._broker.X` everywhere, plus `self._client.X` in `tradepulse/providers/alpaca_market_data.py` only (other modules' `_client` is an HTTP client, not a broker).
    - Assert each collected name is a `BrokerPort` member.
- [ ] **Step 2:** Run `pytest python_tests/test_broker_port.py -v`. Expected: FAIL (`ImportError: BrokerPort`).
- [ ] **Step 3:** Implement `port.py` and the `venue` attribute; export `BrokerPort` and the aliases from `tradepulse.broker`.
- [ ] **Step 4:** Run the test file, then the full suite. Expected: PASS, with no other test changed.
- [ ] **Step 5:** Commit: `feat(broker): BrokerPort protocol and neutral broker type names (A0.1)`.

### Task 2: Venue-qualified asset keys

**Files:**
- Modify: `tradepulse/models/market.py` (`asset_key_from_broker_symbol`)
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Produces: `asset_key_from_broker_symbol(asset_class: AssetClass, symbol: str, venue: str = "alpaca") -> str`.
  - `venue="alpaca"` returns exactly today's key: venue unset, so the key reads `default`, with native id `alpaca:{SYMBOL.upper()}`.
  - Any other venue returns `asset_identity_key(AssetIdentity(symbol, asset_class, native_asset_id=f"{venue}:{symbol}", venue=venue))`. The symbol keeps its case: IBKR's native ids are case-sensitive contract ids.

- [ ] **Step 1: Write the failing tests**
  - `test_default_venue_reproduces_existing_keys`: for `("equity","AAPL")`, `("crypto","BTC/USD")` and `("option","AAPL261120C00200000")`, the result equals the pre-change literal, for example `"equity:default:alpaca:AAPL"`.
  - `test_other_venues_never_collide_with_alpaca`: `asset_key_from_broker_symbol(EQUITY, "AAPL", venue="ibkr") == "equity:ibkr:ibkr:AAPL"` and differs from the Alpaca key; `venue="simulated"` likewise.
- [ ] **Step 2:** Run them. Expected: FAIL (unexpected keyword `venue`).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(models): venue-qualified asset keys, Alpaca keys unchanged (A0.2)`.

### Task 3: Modules depend on the port, not on Alpaca

**Files:**
- Modify the type annotations only (no logic) in:
  - `cli.py`, `execution/gateway.py`, `execution/fill_attribution.py`, `monitor/coordinator.py`;
  - `providers/alpaca_market_data.py`, `providers/market_data_capability.py`;
  - `reconciliation/coordinator.py`, `risk/session.py`, `scanner/coordinator.py`;
  - `session_commands.py`, `valuation.py`, `web/app.py`.

  Each parameter or attribute typed `AlpacaClient` becomes `BrokerPort`, and `Alpaca*` data types become their Task 1 aliases.
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Consumes: Task 1 `BrokerPort` and aliases.
- Produces: `AlpacaClient` is constructed only in `session_commands.build_broker`. Feed probing (`market_data_capability.py`) uses only `get_latest_quote(..., feed_override=...)`, which is on the port.

- [ ] **Step 1: Write the failing test.** `test_only_construction_sites_import_alpaca_client`: AST-scan `tradepulse/` and assert the modules importing the name `AlpacaClient` are exactly `{tradepulse/broker/__init__.py, tradepulse/broker/alpaca_client.py, tradepulse/session_commands.py}`.
- [ ] **Step 2:** Run it. Expected: FAIL, listing the extra modules.
- [ ] **Step 3:** Change the annotations. Keep `from __future__ import annotations` where present; add `BrokerPort` imports.
- [ ] **Step 4:** Run the full suite and `ruff check tradepulse --select F,I001`. Expected: PASS, with no new findings.
- [ ] **Step 5:** Commit: `refactor: modules depend on BrokerPort, not AlpacaClient (A0.3)`.

### Task 4: Simulated broker — account, positions, sessions and market data

**Files:**
- Create: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py`

**Interfaces:**
- Produces: `class SimulatedBroker` (satisfies `BrokerPort`; `venue = "simulated"`):
  - Constructor: `__init__(*, clock: Callable[[], datetime], cash: Decimal = Decimal("100000"), session: SimSession | None = None, fee_schedule: FeeSchedule | None = None, account_number: str = "SIM0001")`.
  - Scripting:
    - `set_quote(symbol: str, asset_class: AssetClass, bid: Decimal, ask: Decimal, at: datetime | None = None) -> None` (`at` defaults to `clock()`);
    - `set_bars(symbol: str, asset_class: AssetClass, bars: list[RawBar]) -> None`;
    - `set_option_chain(underlying: str, contracts: list[OptionContract]) -> None`.
  - `@dataclass(frozen=True) class SimSession`: `is_open(at: datetime) -> bool` over regular US equity hours 13:30–20:00 UTC, Monday to Friday by default, or `SimSession.always_open()`. `next_open` and `next_close` are computed for `get_clock`.
  - Market data: `get_latest_quote` returns the last scripted quote with its timestamp. An unscripted symbol returns `RawQuote(bid=None, ask=None, timestamp=None)`, which the provider rejects exactly as it rejects Alpaca's empty quotes. `get_bars` and `get_options_chain` return the scripted series, filtered by date or expiry.
  - Account: equity = cash + Σ(qty × mid × multiplier); `last_equity` is equity at the last `end_of_day()`.
  - Positions: average entry from fills, mark at the quote mid; Alpaca symbols for crypto (`BTCUSD`) via the existing `normalize_alpaca_symbol` inverse.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_simulator_satisfies_the_port` (`isinstance(..., BrokerPort)`).
  - `test_clock_follows_the_session_calendar`: open at 15:00 UTC on a weekday, closed at 21:00 UTC and on Saturday, with correct next open and close.
  - `test_unscripted_quote_is_empty_not_invented`.
  - `test_bars_and_option_chain_are_filtered_by_date_and_expiry`.
- [ ] **Step 2:** Run. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement. State is plain dicts keyed by `(symbol, asset_class)`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulated broker market data, sessions, account (A0.4)`.

### Task 5: Simulated broker — orders, fills, fees and activities

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**
- Produces:
  - `place_order` validates like Alpaca (positive quantity, known side, time in force `day`, or `gtc` for crypto) and returns `OrderState(status="accepted", ...)` with a UUID `broker_order_id`. A duplicate `client_order_id` raises `AlpacaError(status_code=422)`, as Alpaca does.
  - Fill model:
    - a market order fills at the ask (buy) or bid (sell) of the quote current at submission;
    - a limit order fills only when marketable;
    - by default the whole quantity fills at once;
    - `script_fills(client_order_id: str, quantities: list[Decimal], then: str = "filled")` scripts partial fills (each call to `get_order` reveals the next) and the terminal status: `"filled"`, `"canceled"` or `"expired"`.
  - `activity_lag(fills: int)`: the next N fills appear in `get_order` before they appear in `get_activities` (Review Focus 3).
  - Activities, in Alpaca shape:
    - `FILL` with the raw keys `id`, `activity_type`, `symbol`, `side`, `qty`, `price`, `transaction_time`, `order_id`;
    - ids `"{YYYYMMDDHHMMSSmmm}{seq:03d}::{uuid}"`, where the 3-digit sequence keeps same-millisecond fills unique and in order (Review Focus 2);
    - pagination honours `after_id`, `page_size` and `page_evidence` with the same semantics as `AlpacaClient.get_activities`.
  - Fees (`FeeSchedule` dataclass):
    - `crypto_fee_pct` posts a `CFEE` activity per crypto fill, in the asset;
    - `reg_fee_per_dollar_sold` and `taf_per_share_sold` accumulate and post as `FEE` activities (`activity_sub_type` `REG`/`TAF`) only when `end_of_day()` is called, reproducing Alpaca's overnight batch.
  - Recovery: `get_order_by_client_order_id` returns the order, or `None` for an unknown id; `cancel_order` makes an open order `canceled`, keeping any partial fill.

- [ ] **Step 1: Write the failing tests.**
  - `test_market_buy_fills_at_the_ask_and_creates_a_position_and_fill_activity`.
  - `test_scripted_partial_fills_then_cancel_keep_the_filled_part` (5 = 3 + cancel; position 3; order `canceled`, `filled_qty` 3).
  - `test_same_millisecond_fills_have_unique_ordered_ids` (Review Focus 2).
  - `test_activity_lag_shows_fills_in_the_order_before_the_activity` (Review Focus 3).
  - `test_fractional_and_crypto_quantities_round_trip_exactly`: `9.877` shares and `0.12345678` BTC (Review Focus 4).
  - `test_equity_fees_post_only_at_end_of_day_and_crypto_fees_per_fill`.
  - `test_client_order_id_lookup_and_duplicate_rejection`.
  - `test_account_equity_is_cash_plus_marked_positions`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulated orders, fills, Alpaca-shaped activities and fees (A0.5)`.

### Task 6: Simulated broker — fault injection

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**
- Produces `class Fault(StrEnum)` and `SimulatedBroker.inject(fault: Fault, times: int = 1)`:
  - `SUBMIT_AMBIGUOUS_ACCEPTED`: the order is recorded and fills as normal, but `place_order` raises `httpx.ConnectError`, as a lost reply would;
  - `SUBMIT_AMBIGUOUS_NOT_ACCEPTED`: raises `httpx.ConnectError` and records nothing;
  - `SUBMIT_REJECTED`: raises `AlpacaError(status_code=422)`;
  - `LOOKUP_TIMEOUT`: the next `get_order` or `get_order_by_client_order_id` raises `httpx.ReadTimeout`;
  - `RATE_LIMITED`: the next call raises `AlpacaError(status_code=429)`;
  - `DISCONNECTED`: every call raises `httpx.ConnectError` until `reconnect()`.

  Each is consumed after `times` uses, except `DISCONNECTED`. `is_definitive_rejection` must classify `SUBMIT_REJECTED` as definitive and the others as ambiguous, unchanged.

- [ ] **Step 1: Write the failing tests.** One per fault, asserting the raised type, its `is_definitive_rejection` classification, and the order state afterwards; for example, after `SUBMIT_AMBIGUOUS_ACCEPTED`, `get_order_by_client_order_id` finds the order.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulated broker fault injection (A0.6)`.

### Task 7: Broker conformance kit

**Files:**
- Create: `python_tests/broker_conformance/__init__.py`, `python_tests/broker_conformance/conftest.py`, `python_tests/broker_conformance/test_conformance.py`
- Modify: `docs/operations-runbook.md` (new section: running the kit)

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces a `conformant_broker` fixture, parametrized over:
  - `simulated`: always runs; scripted quotes and calendar;
  - `alpaca_paper`: `@pytest.mark.integration`; runs only with `pytest -m integration` and Alpaca paper credentials in `.env`. Read-only tests by default. Order tests additionally require `TRADEPULSE_CONFORMANCE_ORDERS=1`, and the fixture first refuses (`pytest.fail`) if `systemctl --user is-active` reports `tradepulse-run` or any `tradepulse-soak*` unit active, or a `tradepulse.cli run` process exists (Review Focus 1). Order tests use 1 share of a liquid ETF (SPY) and are skipped when the market is closed.
- One test per area of design 3.4, written against `BrokerPort` only:
  - identity round-trip;
  - order accept and reject classification;
  - full and partial fill (simulator scripts; paper: full only);
  - recovery by client order id, plus not-found for an unknown id;
  - each fill's fees captured once (simulator fee schedule; paper: fee activities read, never asserted to exist same-day);
  - history pagination complete with no duplicates;
  - account and position totals consistent;
  - session calendar matches `get_clock`;
  - outages (simulator only).

- [ ] **Step 1: Write the kit tests** (all areas) and the fixture with its guards. Also `test_order_tests_refuse_while_a_soak_or_service_runs`: monkeypatch the activity probe to report an active unit, and assert the fixture fails before any order call.
- [ ] **Step 2:** Run `pytest python_tests/broker_conformance -v`. Expected: the simulator variants PASS, and the `alpaca_paper` variants are deselected without `-m integration`.
- [ ] **Step 3:** Run it read-only once against Alpaca paper, with the operator's agreement: `pytest -m integration python_tests/broker_conformance -k "alpaca_paper and not order"`. Expected: PASS.
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5:** Commit: `test(broker): conformance kit for BrokerPort adapters (A0.7)`.

### Task 8: The whole runtime against the simulator

**Files:**
- Test: `python_tests/test_runtime_on_simulator.py`

**Interfaces:**
- Consumes: `SimulatedBroker`, the existing `ExecutionGateway`, `run_position_monitor`, `SettlementProcessor` and `run_reconciliation`, with `AlpacaMarketDataProvider(SimulatedBroker)` as the quote source.

- [ ] **Step 1: Write the tests.**
  - `test_buy_settles_then_monitor_stop_exits_and_reconciliation_is_clean`:
    1. A gateway BUY of AAPL on the simulator fills.
    2. Settlement creates the lot and holding.
    3. The quote drops below the stop; the monitor exits.
    4. `end_of_day()` posts fees.
    5. `run_reconciliation` returns `ok`, with no drift and no missed fills.
  - `test_ambiguous_submission_is_recovered_without_a_second_order`: with `SUBMIT_AMBIGUOUS_ACCEPTED`, the intent ends `filled` and the simulator holds exactly one order.
  - `test_lagged_fill_activity_is_attributed_after_it_appears` (`activity_lag(1)`).
- [ ] **Step 2:** Run. Expected: PASS. Any failure is a real incompatibility between the simulator and Alpaca's contract: fix the simulator, never the production code, unless the production code is wrong for every broker. If so, stop and report.
- [ ] **Step 3:** Full suite green.
- [ ] **Step 4:** Commit: `test: end-to-end runtime on the simulated broker (A0.8)`.

### Task 9: Merge and the 24-hour verification run

**Files:**
- Modify: `docs/multi-broker-ibkr-design.md` (Phase A0 status), `docs/operations-runbook.md` (the verification run)
- Create: `docs/rev130-phase-a0.md` (what changed, how it was validated)

- [ ] **Step 1:** Run the whole-branch review (subagent-driven development's final review) and address its findings.
- [ ] **Step 2:** Merge `phase-a0` into main as Rev.130: full suite and frontend tests pass, signed, pushed, zip cut.
- [ ] **Step 3: The 24-hour verification run** (operator approved; account flat, or its positions accepted as opening inventory; no other runtime):
  ```
  .venv/bin/python scripts/run_accounting_soak.py --run-number 1 --hours 24 \
    --database data/soaks/<UTC stamp>-a0-check/soak-accounting-1.db \
    --report data/soaks/<UTC stamp>-a0-check/soak-accounting-1.json --port 8766
  ```
  Started and ended with the market closed. Expected: no errors, all lanes continuous, reconciliation clean, and Alpaca behaviour indistinguishable from the Rev.129 runs. Coverage invariants such as round trips may legitimately fail in 24 hours; they are recorded, not gating.
- [ ] **Step 4:** Record the run's outcome in `docs/rev130-phase-a0.md`. Phase A starts only after this run is clean.
