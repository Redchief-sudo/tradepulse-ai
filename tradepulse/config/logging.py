from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


# Every attribute a plain LogRecord carries with no `extra=` at all -- computed
# dynamically (not hand-listed) so it stays correct across Python versions.
# Anything else on the record was added via `extra=` and must be surfaced.
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message"}

# HTTP client request lines carry the full URL, and some provider URLs embed a
# credential (the Telegram Bot API puts the bot token in the path). Keep these
# transport loggers at WARNING so secrets never reach INFO logs.
_URL_LOGGING_TRANSPORTS = ("httpx", "httpcore")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key == "event":
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in _URL_LOGGING_TRANSPORTS:
        logging.getLogger(name).setLevel(logging.WARNING)
