# -*- coding: utf-8 -*-

from __future__ import annotations

import heapq
import threading

from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from typing import Literal, Optional, List
    from .rtp import RTPPacket

__all__ = [
    'HeapJitterBuffer',
]


class HeapJitterBuffer:
    """Push item in, pop items out"""

    def __init__(self, maxsize: int = 10, *, prefsize: int = 1, prefill: int = 1):
        if maxsize < 1:
            raise ValueError(f'maxsize ({maxsize}) must be greater than 0')

        if not 0 <= prefsize <= maxsize:
            raise ValueError(f'prefsize must be between 0 and maxsize ({maxsize})')

        self.maxsize: int = maxsize
        self.prefsize: int = prefsize
        self.prefill: int = prefill
        self._prefill: int = prefill

        self._last_rx: int = 0
        self._last_tx: int = 0

        self._has_item: threading.Event = threading.Event()
        self._lock: threading.RLock = threading.RLock()
        
        # I sure hope I dont need to add a lock to this
        self._buffer: List[tuple[int, RTPPacket]] = []

    def __bool__(self) -> bool:
        return len(self._buffer) > 0

    def __len__(self) -> int:
        return len(self._buffer)

    def _push(self, packet: RTPPacket, seq: int) -> None:
        heapq.heappush(self._buffer, (seq, packet))

    def _pop(self) -> RTPPacket:
        return heapq.heappop(self._buffer)[1]

    def _get_packet_if_ready(self) -> Optional[RTPPacket]:
        return self._buffer[0][1] if len(self._buffer) > self.prefsize else None

    def _pop_if_ready(self) -> Optional[RTPPacket]:
        return self._pop() if len(self._buffer) > self.prefsize else None

    def _update_has_item(self) -> None:
        prefilled = self._prefill == 0
        packet_ready = len(self._buffer) > self.prefsize

        if not prefilled or not packet_ready:
            self._has_item.clear()
            return

        sequential = self._last_tx + 1 == self._buffer[0][0]
        positive_seq = self._last_tx > 0

        # We have the next packet ready
        # OR we havent sent a packet out yet
        # OR the buffer is full
        if (sequential and positive_seq) or not positive_seq or len(self._buffer) >= self.maxsize:
            self._has_item.set()
        else:
            self._has_item.clear()

    def _cleanup(self) -> None:
        while len(self._buffer) > self.maxsize:
            heapq.heappop(self._buffer)
        while self._buffer and self._buffer[0][0] <= self._last_tx:
            print(f"ERROR: clearing buffer {self._buffer[0][0]} | {self._last_tx}")
            heapq.heappop(self._buffer)

    def _get_seq(self, packet: RTPPacket) -> int:
        return packet.sequence

    def push(self, packet: RTPPacket) -> bool:
        """
        Push a packet into the buffer.  If the packet would make the buffer
        exceed its maxsize, the oldest packet will be dropped.
        """

        seq = packet.sequence
        # if the seq has rolled over, it'll be significantly lower than last rx seq
        if seq + 32768 < self._last_rx:
            with self._lock: # For thread safety, can be moved elsewhere if necessary
                self.reset()

        # Ignore the packet if its too old
        if seq <= self._last_rx and self._last_rx > 0:
            return False

        self._push(packet, seq)

        if self._prefill > 0:
            self._prefill -= 1

        self._last_rx = seq

        self._cleanup()
        self._update_has_item()

        return True

    @overload
    def pop(self, *, timeout: float = 1.0) -> Optional[RTPPacket]:
        ...

    @overload
    def pop(self, *, timeout: Literal[0]) -> Optional[RTPPacket]:
        ...

    def pop(self, *, timeout=1.0):
        """
        If timeout is a positive number, wait as long as timeout for a packet
        to be ready and return that packet, otherwise return None.
        """

        ok = self._has_item.wait(timeout)
        if not ok:
            return None

        if self._prefill > 0:
            return None

        # This function should actually be redundant but i'll leave it for now
        packet = self._pop_if_ready()

        if packet is not None:
            with self._lock: # For thread safety since last tx is also set in reset
                self._last_tx = packet.sequence

        self._update_has_item()
        return packet

    def peek(self, *, all: bool = False) -> Optional[RTPPacket]:
        """
        Returns the next packet in the buffer only if it is ready, meaning it can
        be popped. When `all` is set to True, it returns the next packet, if any.
        """

        if not self._buffer:
            return None

        if all:
            return self._buffer[0][1]
        else:
            return self._get_packet_if_ready()

    def peek_next(self) -> Optional[RTPPacket]:
        """
        Returns the next packet in the buffer only if it is sequential.
        """

        packet = self.peek(all=True)

        if packet and self._get_seq(packet) == self._last_tx + 1:
            return packet

    def gap(self) -> int:
        """
        Returns the number of missing packets between the last packet to be
        popped and the currently held next packet.  Returns 0 otherwise.
        """

        if self._buffer and self._last_tx > 0:
            return self._buffer[0][0] - self._last_tx + 1

        return 0

    def flush(self) -> List[RTPPacket]:
        """
        Return all remaining packets.
        """

        packets = [p for (_, p) in sorted(self._buffer)]
        self._buffer.clear()

        if packets:
            self._last_tx = packets[-1].sequence

        self._prefill = self.prefill
        self._has_item.clear()

        return packets

    def reset(self) -> None:
        """
        Clear buffer and reset internal counters.
        """

        self._buffer.clear()
        self._has_item.clear()
        self._prefill = self.prefill
        self._last_tx = self._last_rx = 0
