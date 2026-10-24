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
