# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = (
    'DaveSupplemental',
    'parse_dave_payload',
)

_DAVE_MARKER = b'\xfa\xfa'


@dataclass(frozen=True)
class DaveSupplemental:
    supplemental_size: int
    supplemental_start: int
    nonce: int
    ranges: tuple[tuple[int, int], ...]
    ciphertext_len: int

    @property
    def ranges_count(self) -> int:
        return len(self.ranges)


def parse_dave_payload(payload: bytes) -> Optional[DaveSupplemental]:
    if len(payload) < 2 or payload[-2:] != _DAVE_MARKER:
        return None
    if len(payload) <= 10:
        return None

    supplemental_size = payload[-3]
    if supplemental_size > len(payload) or supplemental_size <= 10:
        return None

    supplemental_start = len(payload) - supplemental_size
    nonce_start = supplemental_start + 8
    supplemental_end = len(payload) - 3
    if nonce_start > supplemental_end:
        return None

    try:
        nonce, cursor = _read_uleb128(payload, nonce_start, supplemental_end)
    except ValueError:
        return None

    ranges: list[tuple[int, int]] = []
    while cursor < supplemental_end:
        try:
            offset, cursor = _read_uleb128(payload, cursor, supplemental_end)
            size, cursor = _read_uleb128(payload, cursor, supplemental_end)
        except ValueError:
            return None
        ranges.append((offset, size))

    ciphertext_len = len(payload) - supplemental_size
    if ciphertext_len <= 0:
        return None

    return DaveSupplemental(
        supplemental_size=supplemental_size,
        supplemental_start=supplemental_start,
        nonce=nonce,
        ranges=tuple(ranges),
        ciphertext_len=ciphertext_len,
    )

def _read_uleb128(buf: bytes, start: int, end: int) -> tuple[int, int]:
    shift = 0
    value = 0
    index = start

    while index < end and shift <= 63:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return value, index
        shift += 7

    raise ValueError("invalid_uleb128")
