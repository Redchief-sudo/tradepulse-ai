import re
import subprocess
from datetime import UTC, datetime

from tradepulse.provenance import (
    COMPANY_NAME,
    COPYRIGHT_OWNER,
    CREATOR_NAME,
    get_provenance,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_identical_inputs_produce_identical_fingerprints() -> None:
    first = get_provenance(now=NOW)
    second = get_provenance(now=NOW)
    assert first.build_fingerprint == second.build_fingerprint


def test_changing_git_commit_changes_the_fingerprint(tmp_path, monkeypatch) -> None:
    """Simulated via two different repo directories (one a real git repo at
    a known commit, one with no git history at all -- "unknown" commit) --
    a different authoritative input must produce a different fingerprint."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    # This throwaway repo exists only to produce a real commit hash for the
    # test -- disabling signing here is scoped to this disposable directory
    # via local git config, never the developer's own tracked repository or
    # real commits.
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=tmp_path, check=True)

    with_commit = get_provenance(now=NOW, repo_path=tmp_path)

    subprocess.run(["rm", "-rf", str(tmp_path / ".git")])
    without_commit = get_provenance(now=NOW, repo_path=tmp_path)

    assert with_commit.git_commit != "unknown"
    assert without_commit.git_commit == "unknown"
    assert with_commit.build_fingerprint != without_commit.build_fingerprint


def test_fingerprint_is_a_valid_sha256_value() -> None:
    provenance = get_provenance(now=NOW)
    assert _SHA256_RE.match(provenance.build_fingerprint)


def test_creator_is_damien_johnson_fisher() -> None:
    assert get_provenance(now=NOW).creator_name == "Damien Johnson Fisher"
    assert CREATOR_NAME == "Damien Johnson Fisher"


def test_copyright_owner_is_damien_johnson_fisher() -> None:
    assert get_provenance(now=NOW).copyright_owner == "Damien Johnson Fisher"
    assert COPYRIGHT_OWNER == "Damien Johnson Fisher"


def test_company_is_separately_represented_from_creator() -> None:
    provenance = get_provenance(now=NOW)
    assert provenance.company_name == "Silvereyes Technologies, LLC"
    assert COMPANY_NAME == "Silvereyes Technologies, LLC"
    assert provenance.company_name != provenance.creator_name  # never conflated -- the company is not the copyright owner


def test_unavailable_git_metadata_is_handled_safely_not_fabricated(tmp_path) -> None:
    """No .git directory at all (e.g. a release archive with no VCS
    metadata) -- must not raise, must not fabricate a commit id."""
    provenance = get_provenance(now=NOW, repo_path=tmp_path)
    assert provenance.git_commit == "unknown"


def test_provenance_initialization_does_not_touch_trading_or_session_state(tmp_path) -> None:
    """get_provenance takes no repositories/database/settings argument at
    all -- calling it can structurally never read or write trading
    config or session state."""
    import inspect

    from tradepulse.provenance import get_provenance as fn

    params = inspect.signature(fn).parameters
    assert set(params) <= {"now", "repo_path"}  # no repositories/settings/session parameter exists to touch


def test_secrets_cannot_become_fingerprint_inputs(monkeypatch) -> None:
    """Setting credential-shaped environment variables must not change the
    fingerprint -- the canonical string is built only from creator/company
    slugs, software_version, and git_commit, never from os.environ."""
    baseline = get_provenance(now=NOW).build_fingerprint
    monkeypatch.setenv("ALPACA_API_KEY", "super-secret-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-secret")
    after = get_provenance(now=NOW).build_fingerprint
    assert baseline == after


def test_cli_provenance_command_requires_no_broker_credentials(monkeypatch, capsys) -> None:
    """tradepulse provenance must work with a completely empty environment
    -- no ALPACA_*/ANTHROPIC_*/OPENAI_* credentials, no .env, no database."""
    for key in list(__import__("os").environ):
        if key.startswith(("ALPACA_", "ANTHROPIC_", "OPENAI_", "TRADEPULSE_")):
            monkeypatch.delenv(key, raising=False)

    from tradepulse.cli import main

    exit_code = main(["provenance"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Damien Johnson Fisher" in out
    assert "Build Fingerprint:" in out
