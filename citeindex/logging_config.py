"""Logging configuration for the CiteIndex CLI (stdlib only).

The default "console" format keeps the historical output. "json" emits one
JSON object per record on stderr for pipelines and log collectors.
"""

import json
import logging
from datetime import UTC, datetime

CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

VALID_LOG_FORMATS = ("console", "json")


class JsonFormatter(logging.Formatter):
    """Format each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(verbose: bool, log_format: str = "console") -> None:
    """Configure the root logger for the CLI.

    Idempotent: replaces existing root handlers so repeated calls in the
    same process do not duplicate output.
    """
    if log_format not in VALID_LOG_FORMATS:
        raise ValueError(f"unknown log format: {log_format!r}")
    level = logging.DEBUG if verbose else logging.WARNING
    formatter: logging.Formatter = (
        JsonFormatter() if log_format == "json" else logging.Formatter(CONSOLE_FORMAT)
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
