from __future__ import annotations

import logging
import re
import tempfile
import unittest
from pathlib import Path

from qtc_tunnel.logging_utils import (
    BeijingFormatter,
    log_event,
    log_exception,
    safe_http_path,
    setup_logging,
)


class LoggingFormatTests(unittest.TestCase):
    def test_beijing_timestamp_format(self):
        formatter = BeijingFormatter()
        record = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        stamped = formatter.formatTime(record)
        self.assertRegex(stamped, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_safe_http_path_strips_userinfo(self):
        self.assertEqual(
            safe_http_path("http://user:pass@127.0.0.1:9998/fsdp/a.git/info/refs?service=git-upload-pack"),
            "/fsdp/a.git/info/refs?service=git-upload-pack",
        )
        self.assertEqual(safe_http_path("/plain/path"), "/plain/path")
        self.assertEqual(safe_http_path("a" * 600), "a" * 512)


class LoggingSetupTests(unittest.TestCase):
    def test_setup_writes_rotating_file_and_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logger, log_path = setup_logging(side="A", project_root=root)
            self.assertEqual(log_path, (root / "logs" / "a-tunnel.log").resolve())
            self.assertTrue(log_path.parent.is_dir())
            log_event(logger, logging.INFO, "test.event", key="v", n=1)
            for handler in logger.handlers:
                handler.flush()
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("test.event | key=\"v\" n=1", content)
            self.assertRegex(content, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| A \|")

    def test_setup_same_side_does_not_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logger, _ = setup_logging(side="B", project_root=root)
            count_after_first = len(logger.handlers)
            setup_logging(side="B", project_root=root)
            self.assertEqual(len(logger.handlers), count_after_first)

    def test_log_exception_is_single_line(self):
        import io
        stream = io.StringIO()
        logger = logging.getLogger("clipboard_git_tunnel.test_exc")
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(BeijingFormatter(
            fmt="%(asctime)s | %(levelname)s | T | %(message)s"))
        logger.addHandler(handler)
        try:
            raise ValueError("boom detail")
        except ValueError as exc:
            log_exception(logger, "process.request_failed", exc, session="abc12345")
        for h in logger.handlers:
            h.flush()
        line = stream.getvalue().strip()
        self.assertNotIn("\n", line)
        self.assertIn('error_type="ValueError"', line)
        self.assertIn('error="boom detail"', line)


if __name__ == "__main__":
    unittest.main()
