"""The supervised unit must enforce paper mode and never re-activate a stopped or latched session."""
import os
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
    assert (os.environ["TRADEPULSE_EXECUTION_MODE"], os.environ["TRADEPULSE_LIVE_TRADING_ENABLED"]) == ("paper", "false")
