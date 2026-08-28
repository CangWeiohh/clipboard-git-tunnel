"""Minimal config.yaml loading for the tunnel entry points.

The tunnel deliberately avoids a PyYAML dependency (the A-end embeddable
Python has no pip), so this module parses the small ``key: value`` subset the
projects uses. Supported:

- blank lines and full-line ``#`` comments
- ``key: value`` pairs (keys are alphanumeric + underscore)
- bare ``true`` / ``false`` booleans, integers, floats, or strings
- single- or double-quoted strings (trailing ``#`` comments are ignored
  only for bare values)
- flat keys only; per-side grouping is done by prefix (``a_*`` / ``b_*``)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_COMMENT = re.compile(r"\s+#.*$")
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith(('"', "'")) and len(value) >= 2 and value[-1] == value[0]:
        return value[1:-1]
    if _INT.match(value):
        return int(value)
    if _FLOAT.match(value):
        return float(value)
    return value


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Parse a flat ``key: value`` YAML file and return a dict.

    Missing files and empty values stay absent / empty strings respectively.
    Invalid input lines are skipped; callers rely on argparse validation for
    per-argument type errors.
    """

    result: dict[str, Any] = {}
    if path is None:
        return result
    path = Path(path)
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value.startswith(('"', "'")):
            parsed = _coerce(value)
        else:
            parsed = _coerce(_COMMENT.sub("", value).strip())
        result[key] = parsed
    return result


def side_defaults(config: dict[str, Any], side: str) -> dict[str, Any]:
    """Project per-side config keys onto argparse dest names.

    ``a_chunk_bytes`` / ``b_chunk_bytes`` become ``chunk_bytes`` so
    entrypoints can use ``defaults.get("chunk_bytes", builtin)``.
    """

    prefix = f"{side.lower()}_"
    return {
        key[len(prefix):]: value
        for key, value in config.items()
        if key.startswith(prefix) and len(key) > len(prefix)
    }