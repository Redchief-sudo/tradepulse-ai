# Phase A0: Broker Port, Simulated Broker and Conformance Kit — Implementation Plan (rev. 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every broker call behind a `BrokerPort` interface without changing Alpaca behaviour, guarantee one runtime per broker account, and add a deterministic simulated broker and a shared conformance kit that every future adapter (IBKR first) must pass.

**Architecture:**
- `BrokerPort` is a `typing.Protocol` that `AlpacaClient` satisfies structurally (it gains only a `venue` attribute), and that a static type check verifies.
- Modules annotate against the port.
- A machine-wide **account runtime lease** (a file lock keyed by venue and account) is held by every trading runtime and checked before any conformance order.
- `SimulatedBroker` models Alpaca's contract in memory, with explicit identity semantics, a real exchange calendar, an order state machine and controllable activity visibility, so the existing gateway, settlement, reconciliation and evidence code run against it unchanged.

**Tech Stack:** Python 3.12, asyncio, `typing.Protocol`, pytest with pytest-asyncio (`asyncio_mode = "auto"`), respx, Decimal for all money. New dev-only dependencies: `mypy`, `exchange_calendars`.

**Spec:** `docs/multi-broker-ibkr-design.md`, sections 3.1–3.5 and 7 (Phase A0).

**Revision 2** applies the external plan review of 2026-10-20: account-lease runtime guard, venue versus implementation identity, an exchange calendar, explicit activity visibility, Alpaca-exact activity IDs, an order state machine, out-of-session orders, fee behaviour labelled as scenario, static typing, enforced integration deselection, 24-hour acceptance criteria, and account-scoped identity groundwork.

## Global Constraints

- **No placeholder or simplified logic.**
  - Every simulator behaviour is either (a) documented Alpaca behaviour, cited in the task report with the source URL and date, or (b) observed in TradePulse's own soak evidence, cited by soak and date.
  - Anything else is a configurable **scenario**, named as such and never presented as Alpaca's guaranteed behaviour.
  - A behaviour that cannot be modelled faithfully is left out and listed in the task report.
  - No `TODO`, no stubs returning fixed values, no tests that assert only that a call ran.
- **Alpaca behaviour stays byte-identical** except for the one deliberate addition in Task 7 (the account lease, which only refuses a second runtime on an account already in use). No change to request parameters, persisted payload shapes, IDs, asset keys or alert text. The existing suite passes with no edits to existing assertions.
- Existing asset keys stay identical: `asset_key_from_broker_symbol(asset_class, symbol)` keeps returning `f"{asset_class}:default:alpaca:{SYMBOL}"`.
- **Three identities are kept distinct:**
  - **venue:** which broker's contract, used in asset keys;
  - **account:** which account at that venue, via `AccountRef`, used for leases and later for account-scoped state;
  - **implementation:** real or simulated, provenance only, never in keys.
- `SimulatedBroker` can never be built from `Settings` or `build_broker`, and refuses construction when `TRADEPULSE_EXECUTION_MODE` is `live`.
- **No order reaches a real broker** from the conformance kit unless all of these hold:
  1. `pytest -m integration` was given explicitly;
  2. `TRADEPULSE_CONFORMANCE_ORDERS=1` is set;
  3. the kit acquires the account lease exclusively;
  4. the defence-in-depth process and unit checks find nothing running.

  Paper only: any `execution_mode != "paper"` or live trading enabled is refused.
- Money and quantities are `Decimal` end to end. The simulator never rounds a quantity Alpaca would accept.
- Every task ends with the full suite green, `ruff check` adding no new findings in changed files, `mypy` clean on `tradepulse/broker/`, and a signed commit to the `phase-a0` worktree branch.

## Acceptance Gates (from the review)

1. **Protocol compatibility:** Alpaca satisfies the port, statically and at runtime; the existing tests pass without assertion changes; legacy keys and payloads are unchanged (Tasks 1–3).
2. **Simulator correctness:** fills, partial fills, cancellation, pagination, fractional quantities, fee events, recovery and injected faults pass invariant-based tests (Tasks 4–6).
3. **Operational isolation:** no real order without explicit authorization; the lease and every guard are verified; live configurations are categorically refused (Tasks 7–8).
4. **End-to-end accounting:** gateway → settlement → monitor → exit → reconciliation, including ambiguous submissions and delayed activities (Task 9).
5. **Rev.130 verification:** full backend and frontend suites, signed reviewed merge, and a clean 24-hour paper run against the criteria in Task 10, with its evidence preserved.

## Review Focus

1. **A conformance order run while any TradePulse runtime trades the same account,** from any database, as a service or as a soak, must refuse before placing an order (Tasks 7–8).
2. **Simulator activity IDs keep Alpaca's exact format** (`YYYYMMDDHHMMSSmmm::<uuid4>`). The soak preflight reads the trade date from `id[:8]`, and evidence lists sort by ID. Pagination follows insertion (chronological) order, as Alpaca's does (Task 5).
3. **A fill visible in `get_order` before its activity is** (Alpaca's attribution lag) is reproducible at an exact clock time (Task 5).
4. **Fractional equity (thousandths) and crypto (8 dp) quantities** round-trip exactly (Task 5).
5. **A plain `pytest`** (no `-m integration`) never constructs a real broker client and never reads credentials for one (Task 8).

---

### Task 1: Broker port, neutral names, account reference and static typing

**Files:**
- Create: `tradepulse/broker/port.py`, `python_tests/typing/port_conformance.py` (checked by mypy, never executed)
- Modify: `tradepulse/broker/alpaca_client.py` (class attribute `venue = "alpaca"`), `tradepulse/broker/__init__.py` (exports), `pyproject.toml` (dev extras gain `mypy>=1.11` and `exchange_calendars>=4.5`; a `[tool.mypy]` section enabling `strict` for `tradepulse.broker.port`, `tradepulse.broker.simulated` and `python_tests.typing`)
- Test: `python_tests/test_broker_port.py`

**Interfaces:**
- Produces, in `tradepulse/broker/port.py`:
  - Neutral aliases: `BrokerAccount = AlpacaAccount`, `BrokerPosition = AlpacaPosition`, `BrokerClock = AlpacaClock`, `OrderRequest = AlpacaOrderRequest`, `OrderState = AlpacaOrderResponse`, `BrokerActivity = AlpacaActivity`, `OptionContract = AlpacaOptionContract`, `RateLimitSnapshot = AlpacaRateLimitSnapshot`.
  - `@runtime_checkable class BrokerPort(Protocol)`: attribute `venue: str` plus these members, all async except `set_market_data_feeds` and the property:
    - account and positions: `get_account() -> BrokerAccount`, `get_positions() -> list[BrokerPosition]`, `get_clock() -> BrokerClock`;
    - orders: `place_order(order: OrderRequest) -> OrderState`, `get_order(broker_order_id: str) -> OrderState`, `get_order_by_client_order_id(client_order_id: str) -> OrderState | None`, `get_open_orders() -> list[OrderState]`, `cancel_order(broker_order_id: str) -> None`;
    - evidence: `get_activities(activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100, *, after_id: str | None = None, page_evidence: list | None = None) -> list[BrokerActivity]`;
    - market data: `get_latest_quote(symbol: str, asset_class: AssetClass, feed_override: str | None = None) -> RawQuote`, `get_latest_option_quotes(symbols: list[str]) -> dict[str, RawQuote]`, `get_options_chain(underlying_symbol: str, expiration_gte: str, expiration_lte: str) -> list[OptionContract]`, `get_bars(symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250) -> list[RawBar]`, `set_market_data_feeds(*, equity_feed: str, option_feed: str) -> None`;
    - other: `rate_limit_snapshot` (property, `RateLimitSnapshot | None`), `aclose() -> None`.
  - `@dataclass(frozen=True) class AccountRef`: `venue: str`, `account_number: str`, with `key` property `f"{venue}:{account_number}"`.
  - `async def account_ref(broker: BrokerPort) -> AccountRef`: resolved from `get_account().account_number`. It raises `ValueError` if the account number is missing or blank: a lease must never key on an unknown account.

- [ ] **Step 1: Write the failing tests.**
  - `test_alpaca_client_satisfies_the_port`: `isinstance(AlpacaClient("k", "s", "paper", 10), BrokerPort)` and `venue == "alpaca"`.
  - `test_every_broker_call_in_the_code_is_on_the_port`: AST-scan `tradepulse/` except `tradepulse/broker/`. Collect `X` from `broker.X` and `self._broker.X` everywhere, plus `self._client.X` in `providers/alpaca_market_data.py` only. Every name must be a port member.
  - `test_account_ref_requires_a_real_account_number` (blank, `None` → `ValueError`).
  - `test_mypy_accepts_the_port_assignments`: run `mypy` on `python_tests/typing/port_conformance.py`, which contains `_a: BrokerPort = AlpacaClient("k", "s", "paper", 10)`. Expect exit code 0. Skip with a reason if `mypy` is not installed.
- [ ] **Step 2:** `pytest python_tests/test_broker_port.py -v`. Expected: FAIL (`ImportError: BrokerPort`).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the test file, then the full suite. Expected: PASS, with no other test changed.
- [ ] **Step 5:** Commit: `feat(broker): BrokerPort, neutral names, AccountRef, static typing (A0.1)`.

### Task 2: Identity semantics — venue, account, implementation

**Files:**
- Modify: `tradepulse/models/market.py` (`asset_key_from_broker_symbol`), `docs/multi-broker-ibkr-design.md` (new section 3.7, account-scoped identity)
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Produces: `asset_key_from_broker_symbol(asset_class: AssetClass, symbol: str, venue: str = "alpaca") -> str`.
  - `venue="alpaca"` returns exactly today's key.
  - Other venues return `asset_identity_key(AssetIdentity(symbol, asset_class, native_asset_id=f"{venue}:{symbol}", venue=venue))`, with the symbol's case preserved.
- Rule, written into docstrings and design section 3.7:
  - the **venue** in a key is the broker whose contract the instrument follows. A simulator modelling Alpaca uses venue `alpaca`;
  - **account** scoping of positions, reservations, reconciliation and risk is a later-phase migration. A0 only provides `AccountRef` and the lease;
  - **implementation** (`"alpaca_http"`, `"simulated"`) is provenance and never appears in a key.

- [ ] **Step 1: Write the failing tests.**
  - `test_default_venue_reproduces_existing_keys` for `("equity","AAPL")`, `("crypto","BTC/USD")` and `("option","AAPL261120C00200000")`, each equal to its pre-change literal.
  - `test_other_venues_never_collide_with_alpaca` (`venue="ibkr"` → `"equity:ibkr:ibkr:AAPL"`).
- [ ] **Step 2:** Run. Expected: FAIL (unexpected keyword `venue`).
- [ ] **Step 3:** Implement, and write design section 3.7.
- [ ] **Step 4:** Run the tests and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(models): venue-qualified asset keys; identity semantics documented (A0.2)`.

### Task 3: Modules depend on the port, not on Alpaca

**Files:**
- Modify the type annotations only (no logic) in:
  - `cli.py`, `execution/gateway.py`, `execution/fill_attribution.py`, `monitor/coordinator.py`;
  - `providers/alpaca_market_data.py`, `providers/market_data_capability.py`;
  - `reconciliation/coordinator.py`, `risk/session.py`, `scanner/coordinator.py`;
  - `session_commands.py`, `valuation.py`, `web/app.py`.

  `AlpacaClient` becomes `BrokerPort`, and `Alpaca*` data types become the Task 1 aliases.
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Produces: `AlpacaClient` is constructed only in `session_commands.build_broker`. Feed probing uses only `get_latest_quote(..., feed_override=...)`, which is on the port.

- [ ] **Step 1: Write the failing test.** `test_only_construction_sites_import_alpaca_client`: the modules importing the name `AlpacaClient` are exactly `{tradepulse/broker/__init__.py, tradepulse/broker/alpaca_client.py, tradepulse/session_commands.py}`.
- [ ] **Step 2:** Run. Expected: FAIL, listing the extra modules.
- [ ] **Step 3:** Change the annotations.
- [ ] **Step 4:** Run the full suite, `ruff check tradepulse --select F,I001` and mypy. Expected: PASS, with no new findings.
- [ ] **Step 5:** Commit: `refactor: modules depend on BrokerPort, not AlpacaClient (A0.3)`.

### Task 4: Simulator foundation — clock, exchange calendar, market data, account

**Files:**
- Create: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py`

**Interfaces:**
- Produces:
  - **`class ManualClock`**: `__init__(start: datetime)` (aware); `__call__() -> datetime`; `advance(delta: timedelta) -> None`; `set(at: datetime) -> None`. It never moves backwards (`ValueError`). Every simulator test uses it; there is no hidden wall-clock time anywhere in the simulator.
  - **`@dataclass(frozen=True) class SimSession`**: an ordered tuple of `(open, close)` aware UTC datetimes.
    - `is_open(at) -> bool`, `next_open(at) -> datetime | None`, `next_close(at) -> datetime | None`.
    - Constructors: `SimSession.from_exchange_calendar(code: str, start: date, end: date)` uses `exchange_calendars` (`"XNYS"`) for real US sessions, including DST, holidays and early closes; `SimSession.always_open()` for crypto-only scenarios; `SimSession.explicit(sessions)`.
    - The time zone is `America/New_York` throughout.
  - **`class SimulatedBroker`** (satisfies `BrokerPort`):
    - `__init__(*, clock: ManualClock, session: SimSession, venue: str = "alpaca", implementation: str = "simulated", cash: Decimal = Decimal("100000"), account_number: str = "SIM0001", fee_schedule: FeeSchedule | None = None)`. The constructor raises `RuntimeError` if `os.environ.get("TRADEPULSE_EXECUTION_MODE") == "live"`.
    - Scripting: `set_quote(symbol, asset_class, bid, ask, at=None)`, `set_bars(symbol, asset_class, bars)`, `set_option_chain(underlying, contracts)`.
    - `get_clock` reports the session for equity and option trading. Crypto is always tradable.
    - An unscripted quote returns `RawQuote(bid=None, ask=None, timestamp=None)`, which the provider rejects as it does Alpaca's empty quotes.
    - Account: equity = cash + Σ(qty × mid × multiplier); `last_equity` is equity at the last `end_of_day()`.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_simulator_satisfies_the_port`, statically: add `_s: BrokerPort = SimulatedBroker(...)` to `python_tests/typing/port_conformance.py`.
  - `test_xnys_calendar_handles_dst_holidays_and_early_closes`:
    - 2026-10-30 opens at 13:30 UTC (EDT);
    - 2026-11-02 opens at 14:30 UTC (EST);
    - 2026-11-26 (Thanksgiving) is closed;
    - 2026-11-27 closes at 18:00 UTC (13:00 ET early close).
  - `test_manual_clock_never_goes_backwards`.
  - `test_unscripted_quote_is_empty_not_invented`.
  - `test_bars_and_option_chain_are_filtered_by_date_and_expiry`.
  - `test_live_mode_refuses_construction`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulator clock, XNYS calendar, market data, account (A0.4)`.

### Task 5: Simulator orders — state machine, fills, activities, fees

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**
- **Order state machine.** Alpaca status names, from Alpaca's order lifecycle documentation, cited in the report.

  | From | Event | To |
  |---|---|---|
  | (none) | `place_order`, venue open or crypto | `accepted` |
  | (none) | `place_order`, equity or option, session closed, `time_in_force="day"`, market | `accepted`, queued to the next session open |
  | `accepted` | fill reveal, partial | `partially_filled` |
  | `accepted` / `partially_filled` | final fill reveal | `filled` |
  | `accepted` / `partially_filled` | `cancel_order` | `canceled`, filled part kept |
  | `accepted` / `partially_filled` | session close passes with a day order unfilled | `expired`, filled part kept |

  - Terminal states are `filled`, `canceled`, `expired` and `rejected`. `cancel_order` on a terminal order raises `AlpacaError(status_code=422)`.
  - **Invariants, checked after every transition by a private `_check_invariants()` that raises:**
    - `0 <= filled_qty <= qty`;
    - `filled_qty` never decreases;
    - `filled_avg_price` equals the VWAP of the revealed fills exactly (`Decimal`);
    - terminal orders never change;
    - every revealed fill has exactly one `FILL` activity (once visible).
- **Fill pricing.**
  - A market buy fills at the ask, and a market sell at the bid, of the quote current at the reveal time. A queued order reveals at the first session open, at the quote then current.
  - A limit order fills only when marketable, at its limit or better.
  - `script_fills(client_order_id: str, fills: list[tuple[Decimal, Decimal | None]], terminal: Literal["filled", "canceled", "expired"] = "filled")` scripts partial fills as `(qty, explicit price or None for quote-based)`. Each `get_order` call reveals the next scripted fill.
  - Unscripted orders fill fully at the first reveal.
- **Activities.**
  - `FILL` raw keys: `id`, `activity_type`, `symbol`, `side`, `qty`, `price`, `transaction_time`, `order_id`, `cum_qty`, `leaves_qty`, `type` (`"fill"` / `"partial_fill"`).
  - IDs are exactly `f"{ts:%Y%m%d%H%M%S}{ms:03d}::{uuid4()}"`.
  - Ordering and pagination follow insertion order. `after_id` continues after that ID's position; an unknown `after_id` raises `AlpacaError(status_code=422)`. `page_evidence` is filled exactly as in `AlpacaClient`.
- **Visibility.**
  - Each activity has `visible_at = fill_time + lag`, where `lag` comes from `set_activity_lag(delay: timedelta)` (default zero). `get_activities` returns only activities with `visible_at <= clock()`.
  - `release_activities()` sets every pending `visible_at` to now.
  - Order fill state (`get_order`) is visible immediately, which is Alpaca's observed lag direction.
- **Fees (`FeeSchedule`): scenarios, labelled as such.**
  - `observed_alpaca_2026_09()` reproduces fee behaviour observed in TradePulse soak evidence (2026-09-29/30):
    - equity sells get `FEE` activities with sub-types `REG`, `TAF` and `CAT`, dated the trade date and created the next day at 00:43–00:52 UTC, posted when `end_of_day()` is called;
    - option fills get an immediate `OCC` fee, and option sells an `ORF` fee;
    - crypto fills get a `CFEE` per fill, in the asset.
  - Rates are parameters with no defaults pretending to be Alpaca's. `none()` is the no-fee scenario.

- [ ] **Step 1: Write the failing tests.**
  - `test_market_buy_in_session_fills_at_the_ask_with_one_fill_activity`.
  - `test_market_order_out_of_session_queues_until_the_open_then_fills_at_the_open_quote`.
  - `test_day_order_unfilled_at_close_expires_keeping_partial_fill`.
  - `test_scripted_partials_vwap_and_cancel_keep_the_filled_part` (5 = 3 @100 + cancel; `filled_avg_price` 100; status `canceled`).
  - `test_cancel_on_a_terminal_order_is_422`.
  - `test_invariant_violation_raises` (a private hook forcing an over-fill raises).
  - `test_activity_ids_are_alpaca_format_and_paginate_in_insertion_order` (three fills in the same millisecond, pages of 2, `id[:8]` equal to the trade date).
  - `test_unknown_after_id_is_rejected`.
  - `test_activity_lag_hides_fills_until_the_clock_reaches_visible_at` (Review Focus 3).
  - `test_fractional_and_crypto_quantities_round_trip_exactly` (`9.877` shares, `0.12345678` BTC).
  - `test_observed_fee_scenario_posts_equity_fees_only_at_end_of_day`.
  - `test_client_order_id_lookup_and_duplicate_is_422`.
  - `test_account_equity_is_cash_plus_marked_positions`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The report cites the Alpaca documentation (statuses, out-of-session queuing, 422 on a duplicate `client_order_id` and on cancelling a terminal order) with URLs and dates. Anything the docs do not confirm becomes a scenario flag.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulator order state machine, Alpaca-exact activities, fee scenarios (A0.5)`.

### Task 6: Simulator fault injection

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**
- Produces `class Fault(StrEnum)` and `SimulatedBroker.inject(fault: Fault, times: int = 1)`:
  - `SUBMIT_AMBIGUOUS_ACCEPTED`: the order is recorded and proceeds, but `place_order` raises `httpx.ConnectError`;
  - `SUBMIT_AMBIGUOUS_NOT_ACCEPTED`: raises `httpx.ConnectError` and records nothing;
  - `SUBMIT_REJECTED`: raises `AlpacaError(status_code=422)`;
  - `LOOKUP_TIMEOUT`: the next `get_order` or `get_order_by_client_order_id` raises `httpx.ReadTimeout`;
  - `RATE_LIMITED`: the next call raises `AlpacaError(status_code=429)`;
  - `DISCONNECTED`: every call raises `httpx.ConnectError` until `reconnect()`.

  Each fault is consumed after `times` uses, except `DISCONNECTED`. `is_definitive_rejection` (unchanged) classifies `SUBMIT_REJECTED` as definitive and every other fault as ambiguous.

- [ ] **Step 1:** One failing test per fault, asserting the raised type, its classification and the order state afterwards.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulated broker fault injection (A0.6)`.

### Task 7: Account runtime lease

**Files:**
- Create: `tradepulse/runtime_lease.py`
- Modify: `tradepulse/cli.py` (`_run_application` holds the lease for the runtime's lifetime)
- Test: `python_tests/test_runtime_lease.py`

**Interfaces:**
- Produces:
  - `class AccountLeaseHeld(RuntimeError)`;
  - `@contextmanager def account_lease(ref: AccountRef, *, holder: str, directory: Path | None = None) -> Iterator[Path]`. It takes an exclusive non-blocking `fcntl.flock` on `directory / f"{ref.venue}-{ref.account_number}.lock"`. The default directory is `$XDG_STATE_HOME/tradepulse/leases`, else `~/.local/state/tradepulse/leases`, mode 0700.
    - It writes `{"holder", "pid", "acquired_at"}` into the file for diagnosis. The lock, not the content, is authoritative, so a crashed holder's lease frees itself.
    - Raises `AccountLeaseHeld` naming the recorded holder.
  - `def account_lease_in_use(ref: AccountRef, *, directory: Path | None = None) -> str | None`: a probe returning the holder description or `None`. It never holds the lock afterwards.
- Behaviour:
  - `tradepulse run`, the supervised service and the soak runtime all use `_run_application`. After the broker account is known, it acquires the lease (holder `"tradepulse run (<database path>)"`) and refuses to start with a clear error and exit code 1 if the lease is held.
  - Reconcile, status and other one-shot commands do not take the lease; they never place orders.
  - The soak runner's restart exercise stops one runtime before starting the next, so it is unaffected.

- [ ] **Step 1: Write the failing tests.**
  - `test_second_holder_is_refused_and_named`.
  - `test_lease_frees_when_the_holder_process_dies`: a subprocess acquires the lease and is killed; the lease is then acquirable.
  - `test_probe_does_not_hold_the_lease`.
  - `test_a_second_runtime_on_the_same_account_refuses_to_start`: drive `_run_application` with a stub broker whose account number matches an already-held lease; exit 1, no lanes started.
  - `test_different_accounts_do_not_conflict`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests and the full suite. Expected: PASS. Existing runtime tests use temporary lease directories via a fixture in `python_tests/conftest.py`, so they never touch the user's real lease directory.
- [ ] **Step 5:** Commit: `feat: one runtime per broker account via a machine-wide lease (A0.7)`.

### Task 8: Broker conformance kit

**Files:**
- Create: `python_tests/broker_conformance/__init__.py`, `python_tests/broker_conformance/conftest.py`, `python_tests/broker_conformance/test_conformance.py`
- Modify: `python_tests/conftest.py` (add `pytest_collection_modifyitems`: unless the `-m` expression explicitly selects `integration`, every `integration` item is skipped with reason `"integration tests run only with -m integration"`), `docs/operations-runbook.md` (new section: running the kit)

**Interfaces:**
- Produces a `conformant_broker` fixture, parametrized over:
  - `simulated`: always runs; calendar from `SimSession.from_exchange_calendar("XNYS", ...)` plus `always_open` for crypto;
  - `alpaca_paper`: marked `integration`. Its fixture resolves settings and refuses unless `execution_mode == "paper"` and live trading is disabled. Read-only tests by default.
- Order tests additionally need `TRADEPULSE_CONFORMANCE_ORDERS=1`. They then:
  1. resolve `AccountRef`;
  2. **acquire `account_lease` exclusively**, holder `"conformance kit"`, which fails if any runtime trades the account;
  3. run the defence-in-depth checks: no active `tradepulse-*` user units, no `tradepulse.cli run` process;
  4. confirm the market is open (otherwise skip);
  5. trade 1 share of SPY, round trip, under the lease.
- One test per design 3.4 area, written against `BrokerPort` only:
  - identity round-trip;
  - accept and reject classification;
  - full and partial fill (simulator scripts; paper: full only);
  - recovery by client order id, and not-found;
  - each fill's fees captured once (simulator fee scenario; paper: fee activities read, never assumed same-day);
  - history pagination complete with no duplicates;
  - account and position totals consistent;
  - session calendar agrees with `get_clock`;
  - outages (simulator only).

- [ ] **Step 1: Write the kit, plus these guard tests:**
  - `test_plain_pytest_never_builds_a_real_broker`: run `pytest python_tests/broker_conformance` in a subprocess without `-m integration`, with `build_broker` patched to raise. Expect a pass, with `alpaca_paper` items skipped (Review Focus 5).
  - `test_order_tests_refuse_while_the_account_lease_is_held`: hold the lease in the test; the order fixture fails before any order call (Review Focus 1).
  - `test_order_tests_refuse_live_configuration`.
  - `test_order_tests_refuse_when_a_runtime_process_or_unit_is_active`: monkeypatch the probes.
- [ ] **Step 2:** `pytest python_tests/broker_conformance -v`. Expected: the simulator variants PASS, and `alpaca_paper` is skipped.
- [ ] **Step 3:** With the operator's agreement, run it once read-only on paper: `pytest -m integration python_tests/broker_conformance -k "alpaca_paper and not order"`. Expected: PASS.
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5:** Commit: `test(broker): conformance kit with lease-guarded paper orders (A0.8)`.

### Task 9: The whole runtime against the simulator

**Files:**
- Test: `python_tests/test_runtime_on_simulator.py`

**Interfaces:**
- Consumes: `SimulatedBroker`, `ManualClock`, the existing `ExecutionGateway`, `run_position_monitor`, `SettlementProcessor` and `run_reconciliation`, and `AlpacaMarketDataProvider(SimulatedBroker)`.

- [ ] **Step 1: Write the tests.**
  - `test_buy_settles_then_stop_exit_and_reconciliation_is_clean`:
    1. In session, the gateway BUYs AAPL and it fills.
    2. Settlement creates the lot and holding.
    3. The quote drops below the stop; the monitor exits.
    4. `end_of_day()` posts the observed fee scenario.
    5. `run_reconciliation` returns `ok`: no drift, no missed fills, fees attributed once.
  - `test_ambiguous_submission_is_recovered_without_a_second_order` (`SUBMIT_AMBIGUOUS_ACCEPTED`: the intent ends `filled`; the simulator holds exactly one order).
  - `test_lagged_fill_activity_is_attributed_once_visible` (`set_activity_lag`, then advance the clock).
  - `test_out_of_session_exit_is_deferred_and_taken_at_the_open` (the Rev.128 monitor rule meets the simulator's queuing).
- [ ] **Step 2:** Run. Expected: PASS. A failure is a real incompatibility. Fix the simulator, never production code, unless production code is wrong for every broker; in that case stop and report.
- [ ] **Step 3:** Full suite green.
- [ ] **Step 4:** Commit: `test: end-to-end runtime on the simulated broker (A0.9)`.

### Task 10: Merge and the 24-hour verification run

**Files:**
- Modify: `docs/multi-broker-ibkr-design.md` (A0 status), `docs/operations-runbook.md` (the verification run and the account lease)
- Create: `docs/rev130-phase-a0.md`

- [ ] **Step 1:** Whole-branch review (the final review of subagent-driven development); address its findings.
- [ ] **Step 2:** Merge `phase-a0` into main as Rev.130: backend and frontend suites pass, mypy is clean, the commit is signed, pushed, and a zip is cut.
- [ ] **Step 3: The 24-hour verification run.** It needs operator approval and no other runtime on the account (the lease now enforces this). It starts and ends with the market closed:
  ```
  .venv/bin/python scripts/run_accounting_soak.py --run-number 1 --hours 24 \
    --database data/soaks/<UTC stamp>-a0-check/soak-accounting-1.db \
    --report data/soaks/<UTC stamp>-a0-check/soak-accounting-1.json --port 8766
  ```
- [ ] **Step 4: Acceptance criteria.** All must hold, read from the report, the runtime log and the database:
  1. No `ERROR` log events, except `alpaca_rate_limited_retrying` warnings, which must stay at or below 5.
  2. Zero `trading_supervisor_lane_failed`.
  3. `all_lanes_continuous` is true.
  4. Zero `reconciliation_issues`. The only `drift_detected` records allowed are `asset_fee` supersessions with reason `new_accounting_evidence`.
  5. Every filled intent has one completed settlement per fill and one attribution per closure; `missing_attributions == duplicate_attributions == 0`.
  6. Broker order count equals submitted intents: no duplicate orders.
  7. Zero integrity holds or incidents; the session never reaches `financial_integrity_blocked`.
  8. `fee_receipts_conserved` is true after the fee batch.
  9. `runtime_boundaries_complete` is true, with the restart exercise performed.
  10. Peak runtime RSS stays below 1.5 GB.
  11. The report is preserved, with its SHA-256 recorded.

  Coverage invariants (`all_asset_classes_exercised`, `slippage_*`, duration and round-trip counts) may fail in 24 hours. They are recorded, not gating.
- [ ] **Step 5:** Record the outcome against each criterion in `docs/rev130-phase-a0.md`. Phase A starts only after every criterion holds.
