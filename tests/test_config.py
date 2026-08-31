from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qtc_tunnel.config import load_config, side_defaults


class ConfigTests(unittest.TestCase):
    def test_parse_types_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "# header comment\n"
                'a_python: "C:\\Python311\\python.exe"\n'
                'b_python: ""\n'
                "a_listen: 0.0.0.0:9999\n"
                "a_chunk_bytes: 262144\n"
                "a_ack_timeout: 5.0\n"
                "a_retries: 5\n"
                "a_write_gap: 4\n"
                "a_log_level: INFO\n"
                "a_window_keywords: \"\"\n"
                "a_flag: true\n"
                "\n"
                "b_target: 192.168.21.14:8888   # trailing comment\n"
                "b_upstream_header_timeout: 30\n"
                "b_upstream_idle_timeout: 2.5\n"
                "b_window_keywords: \"HSR\"\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config["a_python"], r"C:\Python311\python.exe")
            self.assertEqual(config["b_python"], "")
            self.assertEqual(config["a_listen"], "0.0.0.0:9999")
            self.assertEqual(config["a_chunk_bytes"], 262144)
            self.assertIsInstance(config["a_chunk_bytes"], int)
            self.assertEqual(config["a_ack_timeout"], 5.0)
            self.assertEqual(config["a_write_gap"], 4)
            self.assertEqual(config["a_log_level"], "INFO")
            self.assertEqual(config["a_window_keywords"], "")
            self.assertIs(config["a_flag"], True)
            self.assertEqual(config["b_target"], "192.168.21.14:8888")
            self.assertEqual(config["b_upstream_header_timeout"], 30)
            self.assertEqual(config["b_upstream_idle_timeout"], 2.5)
            self.assertEqual(config["b_window_keywords"], "HSR")

    def test_side_defaults_maps_prefixes(self):
        config = {"a_chunk_bytes": 1, "b_chunk_bytes": 2, "a_python": ""}
        self.assertEqual(side_defaults(config, "a"),
                         {"chunk_bytes": 1, "python": ""})
        self.assertEqual(side_defaults(config, "b"), {"chunk_bytes": 2})

    def test_missing_or_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_config(Path(tmp) / "nope.yaml"), {})
        self.assertEqual(load_config(None), {})

    def test_quoted_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text('a_log_dir: "D:/tunnel/logs"\n', encoding="utf-8")
            self.assertEqual(load_config(path)["a_log_dir"], "D:/tunnel/logs")


if __name__ == "__main__":
    unittest.main()