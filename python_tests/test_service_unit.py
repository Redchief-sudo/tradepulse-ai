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
    assert service["ExecStart"].split()[-5:] == ["run", "--no-browser", "--resume",
                                                 "--require-database", "sqlite:///tradepulse-service.db"]
    assert service["Restart"] == "on-failure"
    assert service["RestartPreventExitStatus"] == "78"  # a database refusal is not retried
    assert "ExecStartPre" not in service  # the runtime checks the database it resolved, not a grep of .env


def test_run_refuses_a_database_other_than_the_required_one(tmp_path, monkeypatch):
    """Rev.120: an exported TRADEPULSE_DATABASE_URL (or a duplicate .env line)
    can make the runtime resolve a different database than .env's service
    line. --require-database compares the resolved one and refuses with 78
    before opening anything."""
    from tradepulse import cli

    started = []
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TRADEPULSE_DATABASE_URL=sqlite:///tradepulse.db\n"
                                   "TRADEPULSE_DATABASE_URL=sqlite:///tradepulse-service.db\n")
    # setenv first so teardown restores the original state: main()'s
    # _load_dotenv writes .env values into os.environ.
    monkeypatch.setenv("TRADEPULSE_DATABASE_URL", "placeholder")
    monkeypatch.delenv("TRADEPULSE_DATABASE_URL")
    monkeypatch.setenv("TRADEPULSE_EXECUTION_MODE", "paper")
    monkeypatch.setenv("TRADEPULSE_LIVE_TRADING_ENABLED", "false")

    async def fake_run_application(*args, **kwargs):
        started.append(args)
        return 0

    monkeypatch.setattr(cli, "_run_application", fake_run_application)
    argv = ["run", "--no-browser", "--resume", "--require-database", "sqlite:///tradepulse-service.db"]
    assert cli.main(argv) == cli.EXIT_DATABASE_NOT_REQUIRED  # the first .env line wins: legacy database
    assert started == [] and not (tmp_path / "tradepulse.db").exists()

    monkeypatch.setenv("TRADEPULSE_DATABASE_URL", f"sqlite:///{tmp_path}/tradepulse-service.db")  # same file, absolute
    assert cli.main(argv) == 0
    assert len(started) == 1


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    from tradepulse.cli import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("TRADEPULSE_EXECUTION_MODE=live\nTRADEPULSE_LIVE_TRADING_ENABLED=true\n")
    monkeypatch.setenv("TRADEPULSE_EXECUTION_MODE", "paper")
    monkeypatch.setenv("TRADEPULSE_LIVE_TRADING_ENABLED", "false")
    _load_dotenv(env_file)
    assert (os.environ["TRADEPULSE_EXECUTION_MODE"], os.environ["TRADEPULSE_LIVE_TRADING_ENABLED"]) == ("paper", "false")
