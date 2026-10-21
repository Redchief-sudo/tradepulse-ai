# Phase A0: Broker Port, Simulated Broker and Conformance Kit — Implementation Plan (rev. 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every broker call behind a `BrokerPort` interface without changing Alpaca behaviour, make an account lease a precondition of every order submission, and add a deterministic simulated broker and a shared conformance kit that every future adapter (IBKR first) must pass.

**Architecture:**
- `BrokerPort` is a `typing.Protocol` that `AlpacaClient` satisfies structurally (it gains only `venue` and `implementation` attributes), verified statically and at runtime. Modules annotate against the port.
- A host-local **account lease** (a file lock keyed by venue and account) must be held to build an execution gateway. The gateway re-verifies it immediately before every submission.
- `SimulatedBroker` models Alpaca's contract in memory: an Alpaca-exact activity identity and ordering, a real exchange calendar, and an order state machine driven by scheduled events on a manual clock. Reads observe state and never advance it. The existing gateway, settlement, reconciliation and evidence code run against it unchanged.

**Tech Stack:** Python 3.12, asyncio, `typing.Protocol`, pytest with pytest-asyncio (`asyncio_mode = "auto"`), respx, Decimal for all money. Dev-only additions: `mypy`, `exchange_calendars`.

**Spec:** `docs/multi-broker-ibkr-design.md`, sections 3.1–3.5 and 7 (Phase A0).

**Revision history:**
- **rev. 2** applied the first external review.
- **rev. 3** applies the second review (findings A0-R2-01…05 and three ambiguities) and corrects one rev. 2 error found while verifying them: Alpaca FILL activity IDs carry **New York local** time, not UTC.

Every factual claim below is marked as one of:
- **[verified: source]:** checked against code or preserved soak evidence while writing this revision;
- **[docs: verify in task]:** to be confirmed against Alpaca's documentation, with a citation in the task report;
- **[scenario]:** a configurable assumption.

## Verified facts this plan relies on

| # | Fact | Source |
|---|---|---|
| F1 | `AlpacaClient.get_activities` requests `direction=asc` and pages with `page_token = <last id of the previous page>`. A page shorter than `page_size` is terminal. A repeated ID or token raises `AlpacaDataIntegrityError`. | [verified: `broker/alpaca_client.py:521-587`] |
| F2 | Alpaca's activity population arrives in ascending lexicographic ID order. Two fills sharing a millisecond arrive in UUID-suffix order. | [verified: soak 20261013T133032Z, 181 activities, `ids == sorted(ids)`; same-millisecond pair `…153511843::597f…`, `…153511843::ce1c…`] |
| F3 | FILL ID = `YYYYMMDDHHMMSSmmm` of `transaction_time` in **America/New_York**, then `::`, then a UUID4. FILL raw keys: `activity_type, cum_qty, id, leaves_qty, order_id, order_status, price, qty, side, symbol, transaction_time, type`. | [verified: `20261015095653634::…` for `2026-10-15T13:56:53.634768Z`] |
| F4 | FEE (`REG`, `TAF`, `CAT`, `ORF`, `OCC`) and CFEE IDs = the New York trade date followed by `000000000::<uuid4>`, so they sort **before** that day's fills. `date` = the trade date; `created_at` = posting time. REG/TAF/CAT/ORF were posted the next day 00:05–00:52 UTC; OCC within the session. Keys: `activity_sub_type, activity_type, created_at, currency, date, description, id, net_amount, status` (+ `execution_id` for OCC). | [verified: soak evidence 2026-08-04/05, 2026-09-29] |
| F5 | Late fees therefore land before an existing cursor. `activity_population` audits the full history on every pass for this reason. | [verified: `reconciliation/activity_cursor.py` docstring and code] |
| F6 | Order-capable code paths: `ExecutionGateway.place_order` is the only order submission. Gateways are built in `cli._run_scan` (line 436), `cli._run_monitor` (472) and `cli._run_application` (941), all via `session_commands.build_gateway`. Nothing in `tradepulse/` calls `cancel_order`. The dashboard builds no gateway. | [verified: grep 2026-10-20] |
| F7 | The existing `RUN_LOCK_KEY` lock is per database file, so it cannot see a runtime using another database. | [verified: `cli.py` `acquire_lock(database, RUN_LOCK_KEY, …)`] |

## Global Constraints

- **No placeholder or simplified logic.**
  - Every simulator behaviour is verified Alpaca behaviour (table above or a cited doc) or a named `[scenario]`.
  - A behaviour that cannot be modelled faithfully is omitted and listed in the task report.
  - No `TODO`, no stubs returning fixed values, no tests that assert only that a call ran.
- **Alpaca behaviour stays byte-identical** except for the Task 7 lease. The lease's only effects are:
  - a runtime or one-shot command refuses to start on an account whose lease is held;
  - a submission is rejected before any broker call if the lease is no longer verifiably held.

  Request parameters, persisted payload shapes, IDs, asset keys and alert text are unchanged. The existing suite passes with no edits to existing assertions.
- `asset_key_from_broker_symbol(asset_class, symbol)` keeps returning `f"{asset_class}:default:alpaca:{SYMBOL}"`.
- **Three identities stay distinct:**
  - **venue:** the broker contract, in asset keys;
  - **account:** `AccountRef`, for leases now and account-scoped state later;
  - **implementation:** `"alpaca_http"` or `"simulated"`, provenance only, never in keys.
- `SimulatedBroker` can never be built from `Settings` or `build_broker`, and refuses construction when `TRADEPULSE_EXECUTION_MODE=live`.
- **No real broker order from tests** unless all of these hold:
  1. `-m integration` is given;
  2. `TRADEPULSE_CONFORMANCE_ORDERS=1` is set;
  3. the configuration is paper with live trading disabled;
  4. the account lease is acquired;
  5. no `tradepulse-*` unit and no `tradepulse` runtime process is active.
- Money and quantities are `Decimal` end to end.
- Every task ends with:
  - the full suite green;
  - `ruff check` adding no new findings in changed files;
  - `mypy` clean on `tradepulse/broker/` and `tradepulse/runtime_lease.py`;
  - a signed commit on the `phase-a0` worktree branch.

## Acceptance Gates

1. **Protocol compatibility** (Tasks 1–3).
2. **Simulator correctness** (Tasks 4–6).
3. **Operational isolation** (Tasks 7–8).
4. **End-to-end accounting** (Task 9).
5. **Rev.130 verification** (Task 10).

## Review Focus

1. **Any order path** (run, one-shot scan or monitor, conformance) on an account whose lease is held elsewhere in the same lock domain must refuse before placing an order. A lease lost mid-run must stop submissions before the broker call (Tasks 7–8).
2. **Simulator activity ordering equals Alpaca's:** ascending lexicographic ID, including same-millisecond fills, backdated fee IDs before existing cursors, and page boundaries between same-millisecond IDs (Task 5).
3. **Polling cannot change execution.** One poll or fifty, from one caller or two, produce identical order states and activities at a given clock time (Task 5).
4. **A replayed fee batch** (the same activities read again on later passes) is never double-counted by settlement, attribution or reconciliation (Task 9).
5. **A default `pytest`** never runs, constructs or resolves the address of a real broker; integration items are deselected (Task 8).

---

### Task 1: Broker port, neutral names, account reference and static typing

**Files:**
- Create: `tradepulse/broker/port.py`, `python_tests/typing/port_conformance.py` (checked by mypy, never executed)
- Modify: `tradepulse/broker/alpaca_client.py` (class attributes `venue = "alpaca"`, `implementation = "alpaca_http"`), `tradepulse/broker/__init__.py` (exports), `pyproject.toml` (dev extras gain `mypy>=1.11` and `exchange_calendars>=4.5`; `[tool.mypy]` with `strict = true` for `tradepulse.broker.port`, `tradepulse.broker.simulated`, `tradepulse.runtime_lease` and `python_tests.typing`)
- Test: `python_tests/test_broker_port.py`

**Interfaces:**
- Produces, in `tradepulse/broker/port.py`:
  - Neutral aliases: `BrokerAccount = AlpacaAccount`, `BrokerPosition = AlpacaPosition`, `BrokerClock = AlpacaClock`, `OrderRequest = AlpacaOrderRequest`, `OrderState = AlpacaOrderResponse`, `BrokerActivity = AlpacaActivity`, `OptionContract = AlpacaOptionContract`, `RateLimitSnapshot = AlpacaRateLimitSnapshot`.
  - `@runtime_checkable class BrokerPort(Protocol)`: attributes `venue: str`, `implementation: str`, plus these members, all async except `set_market_data_feeds` and the property:
    - account and positions: `get_account() -> BrokerAccount`, `get_positions() -> list[BrokerPosition]`, `get_clock() -> BrokerClock`;
    - orders: `place_order(order: OrderRequest) -> OrderState`, `get_order(broker_order_id: str) -> OrderState`, `get_order_by_client_order_id(client_order_id: str) -> OrderState | None`, `get_open_orders() -> list[OrderState]`, `cancel_order(broker_order_id: str) -> None`;
    - evidence: `get_activities(activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100, *, after_id: str | None = None, page_evidence: list | None = None) -> list[BrokerActivity]`;
    - market data: `get_latest_quote(symbol: str, asset_class: AssetClass, feed_override: str | None = None) -> RawQuote`, `get_latest_option_quotes(symbols: list[str]) -> dict[str, RawQuote]`, `get_options_chain(underlying_symbol: str, expiration_gte: str, expiration_lte: str) -> list[OptionContract]`, `get_bars(symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250) -> list[RawBar]`, `set_market_data_feeds(*, equity_feed: str, option_feed: str) -> None`;
    - other: `rate_limit_snapshot` (property, `RateLimitSnapshot | None`), `aclose() -> None`.
  - `@dataclass(frozen=True) class AccountRef`: `venue: str`, `account_number: str`, with `key` property `f"{venue}:{account_number}"`. Both fields must match `^[A-Za-z0-9_-]{1,64}$` (they become a file name); otherwise `ValueError`.
  - `async def account_ref(broker: BrokerPort) -> AccountRef`: from `broker.venue` and `(await broker.get_account()).account_number`. Missing, blank or invalid raises `ValueError`.

- [ ] **Step 1: Write the failing tests.**
  - `test_alpaca_client_satisfies_the_port` (`isinstance`, `venue == "alpaca"`, `implementation == "alpaca_http"`).
  - `test_every_broker_call_in_the_code_is_on_the_port`: AST-scan `tradepulse/` except `tradepulse/broker/`. Collect `X` from `broker.X` and `self._broker.X`, plus `self._client.X` in `providers/alpaca_market_data.py`. Every name must be a port member.
  - `test_account_ref_rejects_blank_none_and_path_characters` (`""`, `None`, `"../x"`, `"a/b"`).
  - `test_mypy_accepts_the_port_assignments`: run mypy on `python_tests/typing/port_conformance.py` (`_a: BrokerPort = AlpacaClient("k", "s", "paper", 10)`); exit 0. If mypy is missing, **fail** (it is a dev dependency), not skip.
- [ ] **Step 2:** `pytest python_tests/test_broker_port.py -v`. Expected: FAIL (`ImportError: BrokerPort`).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the test file and the full suite. Expected: PASS, with no other test changed.
- [ ] **Step 5:** Commit: `feat(broker): BrokerPort, neutral names, AccountRef, static typing (A0.1)`.

### Task 2: Identity semantics — venue, account, implementation

**Files:**
- Modify: `tradepulse/models/market.py` (`asset_key_from_broker_symbol`), `docs/multi-broker-ibkr-design.md` (new section 3.7, identity; new section 3.8, account ownership across hosts)
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Produces: `asset_key_from_broker_symbol(asset_class: AssetClass, symbol: str, venue: str = "alpaca") -> str`.
  - `"alpaca"` returns exactly today's key.
  - Other venues return `asset_identity_key(AssetIdentity(symbol, asset_class, native_asset_id=f"{venue}:{symbol}", venue=venue))`.
- **Section 3.7** states the venue, account and implementation rule above, and that account-scoped positions, reservations, reconciliation and risk are a later-phase migration.
- **Section 3.8** states that A0's lease is host-local (Task 7). Before any second host may hold credentials for the same account (for example the Oracle server), a cross-host ownership mechanism must exist; until then the rule is one host per account. The existing reconciliation integrity lock **detects** out-of-band fills after the fact; it does not prevent them.

- [ ] **Step 1: Write the failing tests.**
  - `test_default_venue_reproduces_existing_keys`: `("equity","AAPL")`, `("crypto","BTC/USD")` and `("option","AAPL261120C00200000")` each equal their pre-change literal.
  - `test_other_venues_never_collide_with_alpaca` (`"equity:ibkr:ibkr:AAPL"`).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, and write sections 3.7 and 3.8.
- [ ] **Step 4:** Run the tests and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(models): venue-qualified asset keys; identity and ownership documented (A0.2)`.

### Task 3: Modules depend on the port, not on Alpaca

**Files:**
- Modify the type annotations only (no logic) in:
  - `cli.py`, `execution/gateway.py`, `execution/fill_attribution.py`, `monitor/coordinator.py`;
  - `providers/alpaca_market_data.py`, `providers/market_data_capability.py`;
  - `reconciliation/coordinator.py`, `reconciliation/activity_cursor.py`, `risk/session.py`, `scanner/coordinator.py`;
  - `session_commands.py`, `valuation.py`, `web/app.py`.
- Test: `python_tests/test_broker_port.py` (append)

**Interfaces:**
- Produces: `AlpacaClient` is constructed only in `session_commands.build_broker`.

- [ ] **Step 1: Write the failing test.** `test_only_construction_sites_import_alpaca_client`: importers of `AlpacaClient` are exactly `{tradepulse/broker/__init__.py, tradepulse/broker/alpaca_client.py, tradepulse/session_commands.py}`.
- [ ] **Step 2:** Run. Expected: FAIL, listing the extra modules.
- [ ] **Step 3:** Change the annotations.
- [ ] **Step 4:** Run the full suite, `ruff check tradepulse --select F,I001` and mypy. Expected: PASS.
- [ ] **Step 5:** Commit: `refactor: modules depend on BrokerPort, not AlpacaClient (A0.3)`.

### Task 4: Simulator foundation — event clock, exchange calendar, market data, account

**Files:**
- Create: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py`

**Interfaces:**
- Produces:
  - **`class ManualClock`**: `__init__(start: datetime)` (aware); `__call__() -> datetime`; `advance(delta: timedelta) -> None`; `set(at: datetime) -> None`. It never moves backwards (`ValueError`). The simulator reads time **only** from it.
  - **`@dataclass(frozen=True) class SimSession`**: an ordered tuple of `(open, close)` aware UTC datetimes.
    - `is_open(at) -> bool`, `next_open(at) -> datetime | None`, `next_close(at) -> datetime | None`, `trade_date(at) -> date` (the New York calendar date).
    - Constructors: `from_exchange_calendar(code: str, start: date, end: date)` (exchange_calendars; `"XNYS"`), `always_open()` and `explicit(sessions)`.
  - **`class SimulatedBroker`** (satisfies `BrokerPort`):
    - `__init__(*, clock: ManualClock, session: SimSession, venue: str = "alpaca", implementation: str = "simulated", cash: Decimal = Decimal("100000"), account_number: str = "SIM0001", fee_scenario: FeeScenario | None = None)`. It raises `RuntimeError` when `TRADEPULSE_EXECUTION_MODE == "live"`.
    - Market data is a **time series**: `set_quote(symbol, asset_class, bid, ask, at: datetime)` appends. Lookups return the latest quote with `at <= clock()`, or for a fill, `<= event time`. `set_bars`, `set_option_chain`.
    - An unscripted quote returns `RawQuote(bid=None, ask=None, timestamp=None)`.
    - `get_clock` reflects the session (`is_open`, `next_open`, `next_close`); crypto is always tradable.
    - Account: equity = cash + Σ(qty × mark × multiplier). `last_equity` = equity at the most recent session close at or before `clock()` (computed, not stored by a call).

- [ ] **Step 1: Write the failing tests.**
  - The simulator is added to `python_tests/typing/port_conformance.py`.
  - `test_xnys_calendar_handles_dst_holidays_and_early_closes` (values [verified: exchange_calendars 4.x, 2026-10-20]):
    - 2026-10-30 opens 13:30 UTC;
    - 2026-11-02 opens 14:30 UTC;
    - 2026-11-26 is not a session;
    - 2026-11-27 closes 18:00 UTC.
  - `test_trade_date_is_new_york_date` (2026-10-16 01:30 UTC → 2026-10-15).
  - `test_manual_clock_never_goes_backwards`.
  - `test_quote_lookup_is_as_of_the_requested_time`.
  - `test_unscripted_quote_is_empty_not_invented`.
  - `test_bars_and_option_chain_are_filtered_by_date_and_expiry`.
  - `test_live_mode_refuses_construction`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): simulator clock, XNYS calendar, as-of market data, account (A0.4)`.

### Task 5: Simulator orders — scheduled execution, Alpaca-exact activities, fee scenarios

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**

**1. Execution is scheduled, never query-driven (A0-R2-04).**
- At `place_order`, the simulator computes a deterministic **event schedule** for the order and stores it.
- All state is a pure function of `(schedule, clock())`. A private `_materialize(now)` applies, in time order, every event with `event.at <= now` not yet applied. It is idempotent and monotonic because the clock never goes back.
- Read methods (`get_order`, `get_order_by_client_order_id`, `get_open_orders`, `get_positions`, `get_account`, `get_activities`) call `_materialize(clock())` and **observe**. No read creates, advances or reorders an event.
- Schedules:
  - **Unscripted order, venue open** (session open, or crypto): one full-fill event at `accepted_at + fill_latency`. `set_fill_latency(timedelta)` [scenario], default zero.
  - **Unscripted market `day` order, equity or option, session closed:** accepted and queued [docs: verify in task]; the fill event is at `next_open(accepted_at) + fill_latency`.
  - **`schedule_fills(client_order_id: str, fills: list[tuple[timedelta, Decimal, Decimal | None]], terminal: Literal["filled", "canceled", "expired"] = "filled")`:** registered **before** the gateway places the order. Offsets are from the order's execution start (acceptance, or the next open if queued). A `None` price takes the as-of quote at the event time. A scripted terminal of `canceled` or `expired` adds that event after the last fill.
  - **Day-order expiry:** an `expired` event at the session's `next_close` for any equity or option `day` order still open.
  - **Fill pricing:** a market buy fills at the ask and a sell at the bid of the as-of quote. A limit order's event applies only if the as-of quote is marketable, at its limit or better; otherwise the event re-arms on the next quote change.
- **State machine** (Alpaca status names [docs: verify in task]): `accepted` → `partially_filled` → `filled`; `accepted` / `partially_filled` → `canceled` (`cancel_order`, effective at `clock()`) or `expired`. Terminal states: `filled`, `canceled`, `expired`, `rejected`. Cancelling a terminal order raises `AlpacaError(status_code=422)` [docs: verify in task].
- **Invariants**, checked after every applied event by `_check_invariants()`, which raises:
  - `0 <= filled_qty <= qty`;
  - monotonic `filled_qty`;
  - `filled_avg_price` equals the exact `Decimal` VWAP;
  - terminal orders never change;
  - one FILL activity per applied fill event.

**2. Activity identity and ordering — one contract (A0-R2-01), from F1–F5.**
- **FILL:** `id = f"{t_ny:%Y%m%d%H%M%S}{t_ny.microsecond // 1000:03d}::{uuid4()}"`, where `t_ny` = `transaction_time` in America/New_York. Raw keys are exactly F3's.
- **FEE / CFEE:** `id = f"{trade_date:%Y%m%d}000000000::{uuid4()}"`, with raw keys exactly F4's.
- **The authoritative order is ascending lexicographic `id`**, both for the full population and for pagination. Insertion order is never used.
- `after_id` returns the activities with `id > after_id` lexicographically. A token that is not itself an activity ID still bounds lexicographically [docs: verify in task; Task 8 checks it read-only on paper].
- **Consequence, the same as Alpaca's (F5):** a later-posted fee can sort before an existing cursor. This is required, not a defect.
- `since` filters `transaction_time` (FILL) or `date` (FEE) after `since` [docs: verify in task]. `page_evidence` is filled exactly as `AlpacaClient` fills it.

**3. Visibility.**
- Each activity has `visible_at`:
  - fills: `transaction_time + activity_lag`;
  - fees: `created_at`.
- `set_activity_lag(timedelta)` [scenario], default zero. Order state is visible at the event time; the activity is visible at `visible_at`. `get_activities` returns only `visible_at <= clock()`.

**4. Fee scenarios (`FeeScenario`, all [scenario]; F4 grounds the shape and timing only).**
- **`FeeScenario.observed_alpaca_2026(rates: FeeRates, posting_time_utc: time)`:**
  - **REG, TAF, CAT** for equity sells (CAT also on buys) and **ORF** for option sells:
    - aggregated per (sub-type, New York trade date) into one activity;
    - `date` = the trade date, `created_at` = the next calendar day at `posting_time_utc`, which is when it becomes visible;
    - `description` follows F4's text pattern.
  - **OCC:** per option fill, `created_at` = fill time, with `execution_id`.
  - **CFEE:** per crypto fill, in the asset, `description = "Coin Pair Transaction Fee (Non USD)"`, `qty` negative, `net_amount = "0"`.
  - `FeeRates` carries each rate and a rounding mode with no defaults. Tests pass explicit values.
- **`FeeScenario.none()`.**
- **Idempotence:** a fee batch is a function of `(trade_date, sub-type)`. It is created exactly once, when the clock first passes its `created_at`. Its ID is fixed at creation, so repeated reads return identical rows.
- No method posts fees on demand; the rev. 2 `end_of_day()` is removed.

- [ ] **Step 1: Write the failing tests.**
  - **Scheduling:**
    - `test_market_buy_in_session_fills_at_the_ask_with_one_fill_activity`;
    - `test_polling_frequency_never_changes_execution`: two identical simulators, one polled once at T+10s and one polled 50 times (from two coroutines) up to T+10s, give identical order states, activity lists and positions;
    - `test_reads_never_advance_scheduled_fills`: with `schedule_fills` at +5s and +10s and the clock at +7s, 20 `get_order` calls all show one fill.
  - **Sessions:**
    - `test_market_order_out_of_session_queues_until_the_open_then_fills_at_the_open_quote`;
    - `test_day_order_open_at_close_expires_keeping_partial_fill`.
  - **Partial fills and cancellation:**
    - `test_scheduled_partials_vwap_and_cancel_keep_the_filled_part` (5 shares: 3 @ 100, then cancel; average 100; status `canceled`);
    - `test_cancel_on_a_terminal_order_is_422`;
    - `test_invariant_violation_raises`.
  - **Activity identity:**
    - `test_fill_ids_use_new_york_time` (`2026-10-15T13:56:53.634768Z` → prefix `20261015095653634`);
    - `test_fee_ids_use_trade_date_midnight_and_sort_before_that_days_fills`.
  - **Ordering and pagination:**
    - `test_population_is_ascending_id_including_same_millisecond_fills`: three fills at the same millisecond; the population equals `sorted(ids)`;
    - `test_pagination_boundary_inside_a_millisecond_is_complete`: `page_size=2` over the three same-millisecond fills plus one later fill; the union of pages equals the population, no duplicates, the page split falls between same-millisecond IDs;
    - `test_late_fee_sorts_before_an_existing_cursor`: a cursor at the last fill, then the fee posts; `after_id=cursor` does not return it and the full history does (F5);
    - `test_after_id_is_a_lexicographic_bound`.
  - **Visibility:** `test_activity_lag_hides_fills_until_visible_at`.
  - **Quantities:** `test_fractional_and_crypto_quantities_round_trip_exactly` (`9.877`, `0.12345678`).
  - **Fees:**
    - `test_fee_batch_is_created_once_and_rereads_are_identical`;
    - `test_fee_batch_aggregates_per_trade_date_in_new_york` (a sell at 2026-10-15 19:59 ET belongs to 10-15, posted 10-16 at the configured UTC time).
  - **Orders:**
    - `test_client_order_id_lookup_and_duplicate_is_422`;
    - `test_account_equity_is_cash_plus_marked_positions`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The report cites the Alpaca documentation (with URL and date) for each [docs: verify in task] item. An item that cannot be confirmed becomes a named scenario flag defaulting to the verified-or-documented behaviour, and is listed.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): scheduled simulator execution, Alpaca-exact activity identity, fee scenarios (A0.5)`.

### Task 6: Simulator fault injection

**Files:**
- Modify: `tradepulse/broker/simulated.py`
- Test: `python_tests/test_simulated_broker.py` (append)

**Interfaces:**
- **`class Fault(StrEnum)`**, each with an **operation scope**:

  | Fault | Scope | Effect |
  |---|---|---|
  | `SUBMIT_AMBIGUOUS_ACCEPTED` | `place_order` | order recorded and scheduled; raises `httpx.ConnectError` |
  | `SUBMIT_AMBIGUOUS_NOT_ACCEPTED` | `place_order` | nothing recorded; raises `httpx.ConnectError` |
  | `SUBMIT_REJECTED` | `place_order` | nothing recorded; raises `AlpacaError(status_code=422)` |
  | `LOOKUP_TIMEOUT` | `get_order`, `get_order_by_client_order_id` | raises `httpx.ReadTimeout` |
  | `RATE_LIMITED` | any method | raises `AlpacaError(status_code=429)` |
  | `DISCONNECTED` | any method | raises `httpx.ConnectError` until `reconnect()` |

- `inject(fault: Fault, times: int = 1)`.
- **Precedence:**
  1. `DISCONNECTED` (a state, not queued) overrides everything.
  2. Otherwise the **first queued fault whose scope includes the called method** fires and is decremented.
  3. Queued faults out of scope stay queued, in order.
- A fault fires **before** any state change, except `SUBMIT_AMBIGUOUS_ACCEPTED`, which records the order first by definition.
- Faults never alter the event schedule of already-accepted orders.

- [ ] **Step 1: Write the failing tests.**
  - One per fault, asserting the raised type, `is_definitive_rejection(exc)` (true only for `SUBMIT_REJECTED`) and the simulator state afterwards.
  - `test_fault_precedence_is_first_in_scope_and_disconnect_overrides`: queue `LOOKUP_TIMEOUT` then `SUBMIT_REJECTED`; `place_order` gets the 422, `get_order` the timeout; `DISCONNECTED` preempts both.
  - `test_faults_classified_in_operation_context`: through the real `ExecutionGateway`:
    - a `place_order` `ConnectError` leaves the intent unresolved and recovery finds it (for the accepted variant) or confirms absence (for the not-accepted variant);
    - a `LOOKUP_TIMEOUT` during recovery never produces a second order;
    - the asserted intent statuses are read from the current gateway code. If the gateway's classification differs from this description, stop and report rather than change it.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(broker): scoped, ordered simulator fault injection (A0.6)`.

### Task 7: Account lease — a precondition of every order submission

**Files:**
- Create: `tradepulse/runtime_lease.py`
- Modify:
  - `tradepulse/session_commands.py`: `build_gateway` requires an `AccountLease`;
  - `tradepulse/execution/gateway.py`: a lease fence beside the reservation fence;
  - `tradepulse/cli.py`: `_run_application`, `_run_scan` and `_run_monitor` acquire it.
- Test: `python_tests/test_runtime_lease.py`

**Guarantee (A0-R2-02), stated exactly as it goes into docstrings and the runbook:**
> Mutual exclusion holds among processes on **one host** that resolve the **same lease directory** on a **local filesystem**. It does not cover another host, another user or `TRADEPULSE_LEASE_DIR` (a different domain), or broker API clients that are not TradePulse. Those remain the operator's responsibility (one host per account, design section 3.8), and reconciliation's integrity lock detects them after the fact.

**Interfaces:**
- `def lease_directory() -> Path`:
  - `TRADEPULSE_LEASE_DIR` if set, else `$XDG_STATE_HOME/tradepulse/leases`, else `~/.local/state/tradepulse/leases`;
  - created with mode 0700;
  - **refuses** (`AccountLeaseUnavailable`) if `/proc/self/mountinfo` puts it on `nfs`, `nfs4`, `cifs`, `smb3` or `fuse.sshfs`.

  It is the one resolver used by every caller.
- `class AccountLeaseHeld(RuntimeError)` names the recorded holder. `class AccountLeaseUnavailable(RuntimeError)`.
- `class AccountLease`: `ref: AccountRef`, `path: Path`, `verify() -> None`.
  - `verify()` raises `AccountLeaseUnavailable` unless the file descriptor is open and `os.fstat(fd).st_ino == os.stat(path).st_ino`. This detects a deleted or replaced lock file, which would otherwise let a second process lock a new inode.
  - Lease files are never unlinked.
- `@contextmanager def account_lease(ref: AccountRef, *, holder: str, directory: Path | None = None) -> Iterator[AccountLease]`:
  - opens `<dir>/<venue>-<account_number>.lock` with `O_CREAT | O_RDWR | O_NOFOLLOW` and mode 0600;
  - takes `fcntl.flock(LOCK_EX | LOCK_NB)`, then verifies the inode as above, retrying once on mismatch;
  - writes `{"holder", "pid", "acquired_at"}`, which is diagnostic only;
  - releases on exit; the operating system releases it if the process dies.
- `def account_lease_in_use(ref, *, directory=None) -> str | None`: a non-holding probe.
- **Gateway:**
  - `build_gateway(..., lease: AccountLease)` raises `ValueError` if `lease.ref.venue != broker.venue`;
  - `ExecutionGateway` keeps the lease;
  - immediately before the reservation fence (`gateway.py` around line 485), `lease.verify()`; on failure it calls `_reject_before_submission(approved, "ACCOUNT_LEASE_NOT_HELD")` and sends a critical alert. `place_order` is never reached.
- **Lifetime (A0-R2-03).** In `_run_application`, `_run_scan` and `_run_monitor`, in this order:
  1. build the broker;
  2. `account_ref(broker)`;
  3. enter `account_lease` (`AccountLeaseHeld` → log `account_lease_held`, close clients, exit 1);
  4. only then build the gateway and start any order-capable task;
  5. on any exit path, including exceptions, cancel and **await** every order-capable task (the trading supervisor, scan and monitor legs) **before** the lease context exits.

  In `_run_application` the account lease is taken **before** the existing per-database `RUN_LOCK_KEY` and released after it.
- Logged at acquisition: `account_lease_acquired` with `venue`, `account_number`, `path` and `holder`.
- `settle`, `reconcile`, `status`, `start`, `stop`, `dashboard` and the verification commands take no lease; F6 shows they place no orders.

- [ ] **Step 1: Write the failing tests.**
  - `test_second_holder_is_refused_and_named`.
  - `test_lease_frees_when_the_holder_process_dies` (a subprocess holds the lease and is killed with SIGKILL).
  - `test_probe_does_not_hold_the_lease`.
  - `test_replaced_lock_file_fails_verify` (unlink and recreate the path, then `verify()` raises).
  - `test_network_filesystem_directory_is_refused` (monkeypatched mountinfo).
  - `test_gateway_cannot_be_built_without_a_lease` (a type error under mypy and a runtime `TypeError`).
  - `test_lost_lease_rejects_before_any_broker_call` (`SimulatedBroker`: break `verify`, request a BUY; the intent is `REJECTED` / `ACCOUNT_LEASE_NOT_HELD` and the simulator holds zero orders).
  - `test_each_order_capable_command_refuses_when_the_lease_is_held`: `run`, `scan` and `monitor` with a stub broker exit 1, build no gateway and start no task.
  - `test_lease_released_only_after_order_capable_tasks_finish`: raise inside `work()` after the supervisor starts; a release hook asserts the supervisor task is `done()`.
  - `test_different_accounts_do_not_conflict`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `python_tests/conftest.py` gains an autouse fixture pointing `TRADEPULSE_LEASE_DIR` at a temporary directory, so tests never touch the real one.
- [ ] **Step 4:** Run the tests and the full suite. Expected: PASS. Existing assertions are unchanged; existing gateway construction in tests goes through a `held_lease` test fixture.
- [ ] **Step 5:** Commit: `feat: account lease is a precondition of every order submission (A0.7)`.

### Task 8: Broker conformance kit and fail-closed integration isolation

**Files:**
- Create: `python_tests/broker_conformance/__init__.py`, `python_tests/broker_conformance/conftest.py`, `python_tests/broker_conformance/test_conformance.py`
- Modify:
  - `python_tests/conftest.py`;
  - `docs/operations-runbook.md`: running the kit, and the lease guarantee from Task 7.

**Interfaces:**
- **Deselection, not skipping.** `pytest_collection_modifyitems`: unless the `-m` expression contains `integration`, every `integration` item is removed from `items` and passed to `config.hook.pytest_deselected`.
- **Fail-closed network guard.** A session autouse fixture: unless integration is selected, `socket.getaddrinfo` raises `RuntimeError("real broker host blocked in default test run")` for any host ending in `alpaca.markets`. respx-mocked tests never resolve DNS, so they are unaffected.
- **`conformant_broker` fixture**, parametrized:
  - `simulated`: XNYS calendar, plus `always_open` for crypto;
  - `alpaca_paper`: marked `integration`. It refuses unless `execution_mode == "paper"` and live trading is disabled. Read-only by default.
- **Order tests additionally need** `TRADEPULSE_CONFORMANCE_ORDERS=1`. They then:
  1. resolve `AccountRef`;
  2. acquire `account_lease` (holder `"conformance kit"`);
  3. probe that no `tradepulse-*` user unit is active and no `tradepulse` runtime process is running;
  4. require the market to be open (otherwise skip);
  5. place a 1-share SPY round trip under the lease.
- **One test per design 3.4 area,** against `BrokerPort` only:
  - identity round-trip;
  - accept and reject classification;
  - fills (simulator: scheduled partials; paper: full);
  - recovery by client order id, and not-found;
  - fees captured once;
  - **ordering:** the population equals `sorted(ids)`, and on paper (read-only) `after_id` set to a synthetic token between two real IDs returns exactly the IDs greater than it, which confirms Task 5's [docs] item;
  - account and position totals;
  - calendar versus `get_clock`;
  - outages (simulator only).

- [ ] **Step 1: Write the kit, plus these guard tests:**
  - `test_default_run_deselects_paper_and_never_resolves_the_broker`: a subprocess `pytest python_tests/broker_conformance -q` reports `alpaca_paper` items as **deselected** (not skipped) and passes, with a sentinel recording zero `alpaca.markets` resolutions.
  - `test_unmocked_broker_call_in_default_run_fails_closed`: an unmocked `AlpacaClient.get_clock()` raises the guard error.
  - `test_order_tests_refuse_while_the_account_lease_is_held`.
  - `test_order_tests_refuse_live_configuration`.
  - `test_order_tests_refuse_when_a_runtime_process_or_unit_is_active`.
- [ ] **Step 2:** `pytest python_tests/broker_conformance -v`. Expected: simulator variants PASS, paper deselected.
- [ ] **Step 3:** With the operator's agreement, run it read-only on paper: `pytest -m integration python_tests/broker_conformance -k "alpaca_paper and not order"`. Expected: PASS. Record the `after_id` result.
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5:** Commit: `test(broker): conformance kit; fail-closed integration isolation (A0.8)`.

### Task 9: The whole runtime against the simulator

**Files:**
- Test: `python_tests/test_runtime_on_simulator.py`

**Interfaces:**
- Consumes: `SimulatedBroker`, `ManualClock`, `FeeScenario`, `account_lease`, the existing `ExecutionGateway`, `run_position_monitor`, `SettlementProcessor` and `run_reconciliation`, and `AlpacaMarketDataProvider(SimulatedBroker)`.

- [ ] **Step 1: Write the tests.**
  - `test_buy_settles_then_stop_exit_and_reconciliation_is_clean`:
    1. In session: BUY AAPL; it fills; settlement creates the lot.
    2. The quote drops below the stop; the monitor exits.
    3. The clock passes the fee posting time.
    4. `run_reconciliation` is `ok` with no drift, and each fee is attributed once.
  - `test_replayed_fee_batch_is_never_double_counted`: after the fees are visible, run settlement and reconciliation three more times. Cash ledger entries per fee ID stay at exactly 1, there are no new attributions, and no `drift_detected` (Review Focus 4).
  - `test_fee_posted_before_an_existing_cursor_is_captured` (F5 end to end).
  - `test_ambiguous_submission_is_recovered_without_a_second_order` (the simulator holds exactly one order).
  - `test_lagged_fill_activity_is_attributed_once_visible`.
  - `test_out_of_session_exit_is_deferred_and_taken_at_the_open`.
- [ ] **Step 2:** Run. Expected: PASS. A failure is a real incompatibility: fix the simulator. Production code changes only if it is wrong for every broker; in that case stop and report first.
- [ ] **Step 3:** Full suite green.
- [ ] **Step 4:** Commit: `test: end-to-end runtime on the simulated broker (A0.9)`.

### Task 10: Merge and the 24-hour verification run

**Files:**
- Create: `scripts/a0_order_census.py`, `scripts/a0_verification_window.py`, `docs/rev130-phase-a0.md`
- Modify: `docs/multi-broker-ibkr-design.md` (A0 status), `docs/operations-runbook.md`

**Interfaces:**
- **`a0_verification_window.py --start <ISO> --hours 24`** exits 0 only if both the start and the end fall outside every XNYS session, and outside the 15 minutes before an open. It uses `SimSession.from_exchange_calendar`, so holidays, weekends and early closes are handled. It prints both boundaries in UTC and New York time. The launcher refuses to start the run otherwise. (Crypto trades continuously; the boundary rule concerns equity and option sessions.)
- **`a0_order_census.py --database … --start … --end …`** is read-only.
  - It pages Alpaca `GET /v2/orders?status=all&after=…&until=…&direction=asc` (read-only HTTP; not on the port, since it is verification tooling) and the intents table.
  - It reports:
    - `submitted_requests`: intents that reached `SUBMITTED`;
    - `accepted_orders`: broker orders whose `client_order_id` is an intent;
    - `definitive_rejections`;
    - `unresolved_submissions`;
    - `duplicate_orders`: more than one broker order per intent;
    - `unauthorized_orders`: a broker order with no intent.

- [ ] **Step 1:** Whole-branch review; address its findings.
- [ ] **Step 2:** Merge `phase-a0` into main as Rev.130: suites pass, mypy is clean, signed, pushed, zip cut.
- [ ] **Step 3: The 24-hour run.** It needs operator approval. Account positions are flat, or a deliberate inherited state recorded beforehand. `a0_verification_window.py` must pass for the chosen start. Then:
  ```
  .venv/bin/python scripts/run_accounting_soak.py --run-number 1 --hours 24 \
    --database data/soaks/<UTC stamp>-a0-check/soak-accounting-1.db \
    --report data/soaks/<UTC stamp>-a0-check/soak-accounting-1.json --port 8766
  ```
- [ ] **Step 4: Acceptance criteria.** Every one must hold:
  1. **Logs:**
     - zero records at level `ERROR` or `CRITICAL`;
     - `WARNING` records are counted by event and listed in the report;
     - `alpaca_rate_limited_retrying` stays at or below 5;
     - any other warning event not seen in the Rev.129 baseline is investigated and explained in `docs/rev130-phase-a0.md` before the run counts as passed.
  2. Zero `trading_supervisor_lane_failed`; `all_lanes_continuous` is true.
  3. Zero `reconciliation_issues`. The only `drift_detected` allowed is an `asset_fee` supersession with reason `new_accounting_evidence`.
  4. One completed settlement per fill; `missing_attributions == duplicate_attributions == 0`.
  5. **Order census:**
     - `duplicate_orders == unauthorized_orders == unresolved_submissions == 0`;
     - `accepted_orders + definitive_rejections == submitted_requests`;
     - every count is reported.
  6. Zero integrity holds or incidents; the session never reaches `financial_integrity_blocked`.
  7. `fee_receipts_conserved` is true after the fee batch posts (the run must span a fee posting time).
  8. `runtime_boundaries_complete` is true, with the restart exercise performed.
  9. Logs show `account_lease_acquired` at each runtime start and no `ACCOUNT_LEASE_NOT_HELD`.
  10. Peak runtime RSS stays below 1.5 GB.
  11. The start and end met the verified window; the report is preserved with its SHA-256 recorded.

  Coverage invariants (`all_asset_classes_exercised`, `slippage_*`, round-trip counts) are recorded, not gating.
- [ ] **Step 5:** Record each criterion's outcome in `docs/rev130-phase-a0.md`. Phase A starts only when all hold.
