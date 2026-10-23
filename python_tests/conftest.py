"""Suite-wide pytest configuration."""
import shutil
import tempfile

import pytest

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
