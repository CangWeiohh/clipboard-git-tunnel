"""Small helpers shared by the A and B HTTP endpoints."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator

from .protocol import DEFAULT_CHUNK_BYTES, Frame, make_frame


def new_session() -> str:
    return uuid.uuid4().hex


def frame_chunks(kind: str, session: str, data: bytes,
                 chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> Iterator[Frame]:
    total = max(1, math.ceil(len(data) / chunk_bytes))
    if not data:
        yield make_frame(kind, session, b"", seq=0, total=1)
        return
    for seq in range(total):
        start = seq * chunk_bytes
        yield make_frame(kind, session, data[start:start + chunk_bytes], seq=seq, total=total)


def reassemble(frames: list[Frame]) -> bytes:
    if not frames:
        return b""
    frames = sorted(frames, key=lambda item: item.seq)
    total = frames[0].total
    if len(frames) != total or [item.seq for item in frames] != list(range(total)):
        raise ValueError("missing or duplicate clipboard frames")
    if any(item.total != total or item.session != frames[0].session for item in frames):
        raise ValueError("inconsistent clipboard frame set")
    return b"".join(item.payload for item in frames)
