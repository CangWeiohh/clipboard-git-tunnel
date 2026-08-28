"""Protocol primitives for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "qtc-clipboard-1"
WIRE_PREFIX = "QTC1:"
DEFAULT_CHUNK_BYTES = 256 * 1024
MAX_CLIPBOARD_CHARS = 1_500_000


class ProtocolError(ValueError):
    """Raised when a clipboard frame is malformed or unsafe."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError("payload must be base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError("invalid base64 payload") from exc


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Frame:
    kind: str
    session: str
    seq: int = 0
    total: int = 1
    payload: bytes = b""
    checksum: str | None = None
    meta: dict[str, Any] | None = None
    retry: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ProtocolError("frame kind is required")
        if not isinstance(self.session, str) or not self.session:
            raise ProtocolError("session is required")
        if not isinstance(self.seq, int) or not isinstance(self.total, int):
            raise ProtocolError("sequence fields must be integers")
        if self.seq < 0 or self.total <= 0 or self.seq >= self.total:
            raise ProtocolError("invalid sequence")
        if self.retry < 0 or not isinstance(self.retry, int):
            raise ProtocolError("invalid retry")
        if self.checksum is not None and self.checksum != digest(self.payload):
            raise ProtocolError("payload checksum mismatch")

    def to_text(self) -> str:
        record: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "kind": self.kind,
            "session": self.session,
            "seq": self.seq,
            "total": self.total,
            "retry": self.retry,
            "payload": _b64(self.payload),
        }
        if self.checksum is not None:
            record["sha256"] = self.checksum
        if self.meta is not None:
            record["meta"] = self.meta
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        text = WIRE_PREFIX + _b64(raw)
        if len(text) > MAX_CLIPBOARD_CHARS:
            raise ProtocolError(f"frame exceeds clipboard limit: {len(text)} chars")
        return text

    @classmethod
    def from_text(cls, text: str) -> "Frame | None":
        if not text or not text.startswith(WIRE_PREFIX):
            return None
        try:
            raw = _unb64(text[len(WIRE_PREFIX):])
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid frame JSON") from exc
        if not isinstance(record, dict) or record.get("v") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported protocol version")
        for field in ("kind", "session"):
            if not isinstance(record.get(field), str):
                raise ProtocolError(f"{field} must be text")
        if not isinstance(record.get("seq"), int) or isinstance(record.get("seq"), bool):
            raise ProtocolError("seq must be an integer")
        if not isinstance(record.get("total"), int) or isinstance(record.get("total"), bool):
            raise ProtocolError("total must be an integer")
        if not isinstance(record.get("retry", 0), int) or isinstance(record.get("retry", 0), bool):
            raise ProtocolError("retry must be an integer")
        payload = _unb64(record.get("payload", ""))
        checksum = record.get("sha256")
        if checksum is not None and checksum != digest(payload):
            raise ProtocolError("invalid payload checksum")
        meta = record.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise ProtocolError("meta must be an object")
        return cls(
            kind=record.get("kind", ""),
            session=record.get("session", ""),
            seq=record.get("seq", -1),
            total=record.get("total", 0),
            payload=payload,
            checksum=checksum,
            meta=meta,
            retry=record.get("retry", 0),
        )


def make_frame(kind: str, session: str, payload: bytes = b"", *, seq: int = 0,
               total: int = 1, meta: dict[str, Any] | None = None) -> Frame:
    return Frame(kind, session, seq, total, payload, digest(payload), meta)


def json_payload(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_json_payload(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON payload") from exc
    if not isinstance(value, dict):
        raise ProtocolError("JSON payload must be an object")
    return value
