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
