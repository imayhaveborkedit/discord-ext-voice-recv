# -*- coding: utf-8 -*-

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .rtp import RTPPacket, FECPacket
    Packet = RTPPacket | FECPacket

class SimpleJitterBuffer:
    """Push item in, returns as many contiguous items as possible"""

    def __init__(self, maxsize=10, *, prefill=3):
        if maxsize < 1:
            raise ValueError('maxsize must be greater than 0')

        self.maxsize = maxsize
        self.prefill = prefill
        self._last_seq: int = 0
        self._buffer: list[Packet] = []

    def push(self, item: Packet) -> list[Packet | None]:
        if item.sequence <= self._last_seq and self._last_seq:
            return []

        bisect.insort(self._buffer, item)

        if self.prefill > 0:
            self.prefill -= 1
            return []

        return self._get_ready_batch()

    def _get_ready_batch(self) -> list[Packet | None]:
        if not self._buffer or self.prefill > 0:
            return []

        if not self._last_seq:
            self._last_seq = self._buffer[0].sequence - 1

        # check to see if the next packet is the next one
        if self._last_seq + 1 == self._buffer[0].sequence:

            # Check for how many contiguous packets we have
            n = ok = 0
            for n in range(len(self._buffer)): # TODO: enumerate
                if self._last_seq + n + 1 != self._buffer[n].sequence:
                    break
                ok = n + 1

            # slice out the next section of the buffer
            segment = self._buffer[:ok]
            self._buffer = self._buffer[ok:]
            if segment:
                self._last_seq = segment[-1].sequence

            return segment

        # size check and add skips as None
        if len(self._buffer) > self.maxsize:
            buf: list[Packet | None] = [
                None for _ in range(self._buffer[0].sequence-self._last_seq-1)
            ]
            self._last_seq = self._buffer[0].sequence - 1
            buf.extend(self._get_ready_batch())
            return buf

        return []

    # TODO: add flush function
