"""Tests for citeindex.logging_config."""

import json
import logging
import sys

from citeindex.logging_config import JsonFormatter, configure_logging


def test_json_formatter_emits_single_json_line():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="citeindex.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "citeindex.test"
    assert payload["message"] == "hello world"
    assert payload["time"].endswith("Z")


def test_json_formatter_includes_exception():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="citeindex.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_replaces_handlers_and_formatter():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure_logging(verbose=True, log_format="json")
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)

        configure_logging(verbose=False, log_format="console")
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
