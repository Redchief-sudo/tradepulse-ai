import json
import logging

from tradepulse.config.logging import JsonFormatter


def _format(**extra) -> dict:
    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="something_happened", args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_arbitrary_extra_fields_survive_formatting() -> None:
    """The formatter used to only forward a hardcoded allowlist of `extra`
    keys -- silently dropping anything else (e.g. `error`, `status`), which
    is exactly why a failed scan's real error message never reached the
    logs. Any `extra=` field must now survive."""
    payload = _format(event="scan_cycle_failed", error="model: bad-model-id is not a valid model", status="failed")
    assert payload["error"] == "model: bad-model-id is not a valid model"
    assert payload["status"] == "failed"
    assert payload["event"] == "scan_cycle_failed"


def test_previously_allowlisted_fields_still_work() -> None:
    payload = _format(event="fill_recorded", trade_intent_id="ti-1", fill_id="fill-1")
    assert payload["trade_intent_id"] == "ti-1"
    assert payload["fill_id"] == "fill-1"


def test_no_extra_fields_still_produces_valid_json() -> None:
    payload = _format()
    assert payload["message"] == "something_happened"
    assert "error" not in payload
