import pytest

from tradepulse.cli import _build_parser, _require_scan_credentials
from tradepulse.config import Settings, SettingsError


def test_scan_subcommand_parses() -> None:
    args = _build_parser().parse_args(["scan"])
    assert args.command == "scan"


def test_missing_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


def test_scan_requires_alpaca_and_anthropic_credentials() -> None:
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        _require_scan_credentials(Settings.from_env({}))
    with pytest.raises(SettingsError, match="ANTHROPIC_API_KEY"):
        _require_scan_credentials(
            Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"})
        )
    _require_scan_credentials(
        Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key"})
    )
