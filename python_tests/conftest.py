"""Suite-wide pytest configuration."""
import os
import shutil
import socket
import tempfile

import pytest

# Integration isolation (Phase A0 Task 8). Unless the -m expression selects
# `integration` items, they are deselected (never collected into the run, so
# never "skipped"), and resolving any *.alpaca.markets host raises: an
# unmocked broker call in the default run fails closed instead of reaching a
# real account. respx-mocked tests never resolve DNS, so they are unaffected.
BLOCKED_BROKER_DOMAIN = "alpaca.markets"
RESOLUTION_LOG_ENV = "TRADEPULSE_TEST_RESOLUTION_LOG"  # a sentinel file: one line per blocked lookup


def integration_selected(config: pytest.Config) -> bool:
    """True when the -m expression would select an item marked only
    `integration` (so "-m 'not integration'" is False)."""
    expression = config.getoption("markexpr", default="") or ""
    if not expression.strip():
        return False
    from _pytest.mark.expression import Expression

    return Expression.compile(expression).evaluate(lambda name: name == "integration")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if integration_selected(config):
        return
    deselected = [item for item in items if item.get_closest_marker("integration") is not None]
    if deselected:
        items[:] = [item for item in items if item.get_closest_marker("integration") is None]
        config.hook.pytest_deselected(items=deselected)


def _is_broker_host(host: object) -> bool:
    name = (host.decode() if isinstance(host, bytes) else str(host or "")).rstrip(".").lower()
    return name == BLOCKED_BROKER_DOMAIN or name.endswith("." + BLOCKED_BROKER_DOMAIN)


@pytest.fixture(scope="session", autouse=True)
def _block_real_broker_hosts(request: pytest.FixtureRequest):
    if integration_selected(request.config):
        yield
        return
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if _is_broker_host(host):
            sentinel = os.environ.get(RESOLUTION_LOG_ENV)
            if sentinel:
                with open(sentinel, "a") as log:
                    log.write(f"{host}\n")
            raise RuntimeError("real broker host blocked in default test run")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo

_owned_basetemp: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Give every pytest process its own temp root.

    By default concurrent pytest processes share /tmp/pytest-of-<user> and its
    rotating numbered directories. Two runs at once (parallel review agents,
    or a run during another run) then interfere through each other's SQLite
    files, which is what made several tests look flaky "under load"."""
    global _owned_basetemp
    if config.option.basetemp is None and not hasattr(config, "workerinput"):
        _owned_basetemp = tempfile.mkdtemp(prefix="tradepulse-pytest-")
        config.option.basetemp = _owned_basetemp


def pytest_unconfigure(config: pytest.Config) -> None:
    if _owned_basetemp is not None:
        shutil.rmtree(_owned_basetemp, ignore_errors=True)


@pytest.fixture(autouse=True)
def held_lease(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own lease directory, so no test touches the real
    one, and holds the lease for TEST_ACCOUNT -- the lease that gateways
    built directly in tests carry (python_tests/lease_support.py)."""
    import lease_support

    from tradepulse.runtime_lease import account_lease

    directory = tmp_path_factory.mktemp("leases")
    monkeypatch.setenv("TRADEPULSE_LEASE_DIR", str(directory))
    with account_lease(lease_support.TEST_ACCOUNT, holder="pytest", directory=directory) as lease:
        lease_support.HELD = lease
        try:
            yield lease
        finally:
            lease_support.HELD = None


@pytest.fixture
def fill_cursor_bootstrapped(monkeypatch: pytest.MonkeyPatch):
    """For runtime-command tests that are not about Rev.140's bootstrap gate:
    treat the database as bootstrapped. The gate itself is tested in
    test_rev140_bootstrap.py and test_cli.py."""
    async def bootstrapped(repositories, lease) -> bool:
        return True

    monkeypatch.setattr("tradepulse.cli._require_fill_cursor", bootstrapped)


async def seed_fill_cursor(repositories, cursor: str = "zzzz-test-cursor") -> None:
    """For gateway and monitor tests that are not about Rev.140's cursor: a
    fill cursor beyond every mocked activity ID, so the sell authorization's
    read after C is empty. No bootstrap record, so reconciliation's
    completeness pass stays idle. Cursor behaviour itself is tested in
    test_rev140_unresolved.py and test_rev140_bootstrap.py."""
    from datetime import UTC, datetime

    from tradepulse.reconciliation.unresolved import _advance_cursor

    await repositories.broker_activity_cursors.database.run(
        lambda connection: _advance_cursor(connection, cursor, "test", datetime.now(UTC)), write=True)


def mock_open_orders(orders: list | None = None):
    """GET /v2/orders?status=open for the sell authorization's two reads."""
    import httpx
    import respx

    return respx.get("https://paper-api.alpaca.markets/v2/orders", params__contains={"status": "open"}).mock(
        return_value=httpx.Response(200, json=orders or []))


def mock_cursor_read(cursor: str = "zzzz-test-cursor"):
    """The sell authorization's FILL read after a seeded cursor: empty. Matches
    only that request (page_token = the cursor), so it never shadows a test's
    own activities mock."""
    import httpx
    import respx

    return respx.get("https://paper-api.alpaca.markets/v2/account/activities",
                     params__contains={"page_token": cursor}).mock(return_value=httpx.Response(200, json=[]))


from rev140_support import rt  # noqa: F401 - the shared Rev.140 runtime fixture


@pytest.fixture(autouse=True)
def _reset_clock_calendar_monitor():
    """Rev.144: the clock-calendar disagreement monitor is process state; no
    test may inherit another's episode."""
    from tradepulse.calendar.disagreement import MONITOR

    MONITOR.reset()
    yield
    MONITOR.reset()


def _open_calendar():
    """Rev.144 test calendar: every New York day is a session, 00:01 to
    23:59, so wall-clock tests with a mocked "open" broker clock agree with
    the calendar at any hour. Alpaca and always-open simulators report the
    same sessions, so the daily check verifies every date."""
    from datetime import UTC, datetime, time, timedelta

    from tradepulse.calendar import MarketCalendar, ny_date, ny_time

    today = ny_date(datetime.now(UTC))
    first, last = today - timedelta(days=400), today + timedelta(days=400)
    sessions = {first + timedelta(days=n): (ny_time(first + timedelta(days=n), time(0, 1)), ny_time(first + timedelta(days=n), time(23, 59)))
                for n in range((last - first).days + 1)}
    return MarketCalendar(sessions, first=first, last=last, option_close_from_equity=True)


@pytest.fixture
def open_market_calendar(monkeypatch):
    """For tests written before Rev.144 that mock the broker clock as open on
    the real wall clock: the process calendar becomes the always-in-session
    test calendar, and the mocked brokers report it. Rev.144's own tests use
    the pinned library calendar instead."""
    from tradepulse.broker import AlpacaClient
    from tradepulse.broker.simulated import SimulatedBroker
    from tradepulse.calendar import BrokerSession, Venue

    calendar = _open_calendar()
    monkeypatch.setattr("tradepulse.calendar.market_calendar", lambda today=None: calendar)

    async def sessions(self, start, end):
        return [BrokerSession(venue, s.date, s.open, s.close, "test:open_calendar")
                for venue in (Venue.US_EQUITY, Venue.US_OPTION) for s in calendar.sessions_between(venue, start, end)]

    real = SimulatedBroker.get_calendar

    async def simulated(self, start, end):
        return await sessions(self, start, end) if self._session.always else await real(self, start, end)

    monkeypatch.setattr(AlpacaClient, "get_calendar", sessions)
    monkeypatch.setattr(SimulatedBroker, "get_calendar", simulated)
    return calendar
