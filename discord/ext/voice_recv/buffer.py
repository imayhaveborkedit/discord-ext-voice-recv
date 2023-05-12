# -*- coding: utf-8 -*-

from __future__ import annotations

import bisect
import threading

from collections import deque

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional, List, Deque, overload, Literal
    from .rtp import RTPPacket

__all__ = [
    'SimpleJitterBuffer',
    'NoPacket'
]


class NoPacket:
    __slots__ = ('sequence',)

    def __init__(self, seq: int):
        self.sequence = seq
        # TODO: timestamp?

    def __bool__(self):
        return False

    def __lt__(self, other):
        return self.sequence < other.sequence

    def __eq__(self, other):
        return self.sequence == other.sequence


if TYPE_CHECKING:
    SomePacket = RTPPacket | NoPacket


class SimpleJitterBuffer:
    """Push item in, returns as many contiguous items as possible"""

    def __init__(self, maxsize=10, *, prefill=3):
        if maxsize < 1:
            raise ValueError('maxsize must be greater than 0')

        self.maxsize = maxsize
        self.prefill = prefill
        self._prefill = prefill # original prefill
        self._last_seq: int = 0
        self._buffer: list[RTPPacket] = []

    def __len__(self):
        return len(self._buffer)

    def push(self, item: RTPPacket) -> list[RTPPacket | None]:
        if item.sequence <= self._last_seq and self._last_seq:
            return []

        bisect.insort(self._buffer, item)

        if self.prefill > 0:
            self.prefill -= 1
            return []

        return self._get_ready_batch()

    def _get_ready_batch(self) -> list[RTPPacket | None]:
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

            return segment # type: ignore

        # size check and add skips as None
        if len(self._buffer) > self.maxsize:
            buf: list[RTPPacket | None] = [
                None for _ in range(self._buffer[0].sequence-self._last_seq-1)
            ]
            self._last_seq = self._buffer[0].sequence - 1
            buf.extend(self._get_ready_batch())
            return buf

        return []

    def flush(self, reset: bool=False) -> list[RTPPacket | None]:
        if reset:
            self.prefill = self._prefill

        if not self._buffer:
            return []

        seq = self._buffer[0].sequence
        remaining: list[RTPPacket | None] = []

        if self._last_seq + 1 != seq:
            assert self._last_seq + 1 < seq
            jump = seq - self._last_seq + 1
            remaining.extend(None for _ in range(jump))

        for packet in self._buffer:
            gap = packet.sequence - seq
            remaining.extend(None for _ in range(gap))
            remaining.append(packet)
            seq = packet.sequence + 1

        return remaining


class NewSimpleJitterBuffer:
    """Push item in, returns as many contiguous items as possible"""

    def __init__(self, maxsize: int=10, *, prefsize: int=1, prefill: int=1):
        if maxsize < 1:
            raise ValueError(f'maxsize ({maxsize}) must be greater than 0')

        if not 0 <= prefsize <= maxsize:
            raise ValueError(f'prefsize must be between 0 and maxsize ({maxsize})')

        self.maxsize = maxsize
        self.prefsize = prefsize
        self.prefill = prefill
        self._prefill = prefill # original prefill
        self._last_seq: int = 0 # the sequence of the last packet popped from the buffer
        self._has_item = threading.Event()
        self._buffer: Deque[SomePacket] = deque(maxlen=maxsize)
        # I sure hope I dont need to add a lock to this

    def __len__(self):
        return len(self._buffer)

    def _get_ready_packet(self) -> SomePacket | None:
        return self._buffer[0] if len(self._buffer) > self.prefsize else None

    def _pop_ready_packet(self) -> SomePacket | None:
        return self._buffer.popleft() if len(self._buffer) > self.prefsize else None

    def _update_has_item(self):
        prefilled = self.prefill == 0
        packet_ready = len(self._buffer) > self.prefsize

        if not prefilled or not packet_ready:
            self._has_item.clear()
            return

        sequential = self._last_seq + 1 == self._buffer[0].sequence
        positive_seq = self._last_seq > 0

        # We have the next packet ready OR we havent sent a packet out yet
        if (sequential and positive_seq) or not positive_seq:
            self._has_item.set()
        else:
            self._has_item.clear()

    def peek(self, *, all: bool=False) -> SomePacket | None:
        if not self._buffer:
            return None

        if all:
            return self._buffer[0]
        else:
            return self._get_ready_packet()

    @overload
    def take(self, *, block: Literal[True]) -> SomePacket:
        ...

    @overload
    def take(self, *, block: Literal[False]) -> SomePacket | None:
        ...

    def take(self, *, block: bool=False) -> SomePacket | None:
        if block:
            self._has_item.wait()

        if self.prefill > 0:
            return None

        packet = self._pop_ready_packet()
        if packet is not None:
            self._last_seq = packet.sequence

        self._update_has_item()
        return packet

    def push(self, item: RTPPacket):
        bisect.insort(self._buffer, item)

        if self.prefill > 0:
            self.prefill -= 1

        self._update_has_item()

    def flush(self, *, reset: bool=False) -> list[SomePacket]:
        packets = list(self._buffer)
        self._buffer.clear()
        self._last_seq = packets[-1].sequence

        if reset:
            self.prefill = self._prefill
            self._last_seq = 0
            self._has_item.clear()

        return packets
