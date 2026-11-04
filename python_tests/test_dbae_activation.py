"""DBAE verification activation (spec §15): the policy is frozen into a generation."""
import json
from decimal import Decimal

import pytest

from python_tests.opening_fixtures import OpeningBroker
from python_tests.test_paper_verification import generation, tree  # noqa: F401  (fixtures)
from tradepulse.dbae import bands, evidence
from tradepulse.dbae.evidence import LiquidationScope, LiquidationVerdict, dbae_policy_fingerprint
from tradepulse.models.market import AssetClass
from tradepulse.verification import service
from tradepulse.verification.integrity import VerificationError, read_manifest
from tradepulse.verification.service import Verification


def _reason(run: Verification) -> str:
    result = run.check()
    assert result["integrity_valid"] is False
    return str(result["reason"])


@pytest.fixture
def pre_dbae_generation(monkeypatch, request):
    """A genuinely bound generation frozen before DBAE: the freeze writes no "dbae" policy key."""
    real = service.freeze_source

    def without_dbae(*args, **kwargs):
        policy = {key: value for key, value in kwargs["policy"].items() if key != "dbae"}
        return real(*args, **{**kwargs, "policy": policy})

    monkeypatch.setattr(service, "freeze_source", without_dbae)
    run = request.getfixturevalue("generation")
    assert "dbae" not in read_manifest(run.directory)["policy"]
    return run


def test_fresh_freeze_records_dbae_policy(generation):  # noqa: F811
    manifest = read_manifest(generation.directory)
    assert manifest["policy"]["dbae"] == dbae_policy_fingerprint()
    assert manifest["policy"]["dbae"]["dbae_policy_version"] == "dbae-1"
    assert generation.check()["integrity_valid"] is True


@pytest.mark.parametrize("entry", ["check", "report", "acquire", "start", "begin", "runtime_started"])
async def test_pre_dbae_generation_cannot_bind_or_resume(pre_dbae_generation, entry):
    run = pre_dbae_generation
    if entry == "check":
        assert run.check()["reason"] == "generation_predates_dbae"
    elif entry == "report":
        assert run.report()["integrity"]["reason"] == "generation_predates_dbae"
    elif entry in ("acquire", "begin"):
        with pytest.raises(VerificationError):
            getattr(run, entry)()
    elif entry == "start":
        assert not await run.start()
    else:
        assert not await run.runtime_started(OpeningBroker())
    assert run._lease is None
    assert not (run.directory / "started.json").exists()
    if entry in ("begin", "runtime_started"):
        # No lease can exist (acquire is refused), so these refuse before reaching a start record.
        return
    # The refusal is recorded and sticky: the generation is invalidated, never migrated.
    invalid = json.loads((run.directory / "invalid.json").read_text())
    assert invalid["reason"]["reason"] == "generation_predates_dbae"


def test_band_threshold_change_is_configuration_mismatch(generation, monkeypatch):  # noqa: F811
    changed = ((bands.BAND_THRESHOLDS[0][0], Decimal(25)), *bands.BAND_THRESHOLDS[1:])
    monkeypatch.setattr(bands, "BAND_THRESHOLDS", changed)
    assert _reason(generation) == "frozen_configuration_mismatch"


def test_evidence_table_change_is_configuration_mismatch(generation, monkeypatch):  # noqa: F811
    scope = LiquidationScope("alpaca-paper", "margin", AssetClass.EQUITY, "fractional", "market", "day",
                             "regular", "notional_below_1_usd")
    row = LiquidationVerdict("verified", "evidence/g2.json", "0" * 64)
    monkeypatch.setattr(evidence, "SUB_MINIMUM_LIQUIDATION_EVIDENCE", {scope: row})
    assert _reason(generation) == "frozen_configuration_mismatch"
