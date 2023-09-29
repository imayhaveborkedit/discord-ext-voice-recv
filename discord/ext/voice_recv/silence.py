# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import logging
import threading

from .opus import VoiceData
from .rtp import SilencePacket

from discord.utils import MISSING
from discord.opus import Decoder

from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from typing import Callable, Any, Dict, Optional, Final, Union
    from .rtp import AudioPacket
    from .types import MemberOrUser as User

    SilenceGenFN = Callable[[Optional[User], VoiceData], Any]
    SSRCData = Tuple[float, Optional[User], AudioPacket]

log = logging.getLogger(__name__)

__all__ = [
    'SilenceGenerator',
]

SILENCE_PCM: Final = b'\0' * Decoder.FRAME_SIZE
PACKET_INTERVAL: Final = 0.02


class SilenceGenerator(threading.Thread):
    """Generates and sends silence packets."""

    def __init__(self, callback: SilenceGenFN, *, grace_period: float = 0.015):
        super().__init__(daemon=True, name=f'silencegen-{id(self):x}')
        self.callback: SilenceGenFN = callback
        self.grace_period: float = grace_period

        self._ssrc_data: Dict[int, SSRCData] = {}  # {ssrc: (time, _, _)}
        self._last_timestamp: Dict[int, int] = {}  # {ssrc: timestamp}
        self._user_map_backup: Dict[int, int] = {}  # {id: ssrc}
        self._end: threading.Event = threading.Event()
        self._has_data: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()

    def push(self, user: Optional[User], packet: AudioPacket) -> None:
        """Updates the last time a packet was received and from whom.
        Calling this function will start generating silence packets for `packet.ssrc`
        until `drop(ssrc)` or `stop()` is called.
        """

        with self._lock:
            self._ssrc_data[packet.ssrc] = (time.perf_counter(), user, packet)
            self._last_timestamp[packet.ssrc] = packet.timestamp

            if user:
                self._user_map_backup[user.id] = packet.ssrc

            self._has_data.set()

    def _get_next_info(self) -> SSRCData:
        return min(self._ssrc_data.values())

    def drop(self, *, ssrc: Optional[int] = None, user: User = MISSING) -> None:
        """Stop generating silence packets for `ssrc`, or whatever is cached for `user`
        if `ssrc` is None, if any.
        """

        with self._lock:
            if ssrc is None:
                ssrc = self._user_map_backup.pop(user.id, None)
                if ssrc is None:
                    return  # weird but ok

            self._last_timestamp.pop(ssrc, None)
            last_data = self._ssrc_data.pop(ssrc, None)
            if last_data is None and user is not MISSING:
                ssrc = self._user_map_backup.pop(user.id)
                self._ssrc_data.pop(ssrc, None)

            if not self._ssrc_data:
                self._has_data.clear()

    def stop(self) -> None:
        """Stops generating silence for everything and clears the cache."""

        self._end.set()
        self._has_data.set()

        with self._lock:
            self._ssrc_data.clear()
            self._user_map_backup.clear()
            self._last_timestamp.clear()
            self._has_data.clear()

        self.join(1)

    def start(self) -> None:
        self._end.clear()
        super().start()

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as e:
            log.exception("Error in %s", self)

    def _do_run(self) -> None:
        while not self._end.is_set():
            self._has_data.wait()
            if self._end.is_set():
                return

            with self._lock:
                tlast, user, packet = self._get_next_info()
                ssrc = packet.ssrc

                # prepare the object before the sleep as a little micro optimization
                next_packet = SilencePacket(
                    ssrc, self._last_timestamp.get(ssrc, packet.timestamp) + Decoder.SAMPLES_PER_FRAME
                )
                # TODO: check if destination wants opus or not
                next_data = VoiceData(next_packet, user, pcm=SILENCE_PCM)

                tnext = tlast + PACKET_INTERVAL
                tnow = time.perf_counter()
                # wait a little bit longer than when the next one should be
                # so we don't have to race with the next packet
                delay = tnext + self.grace_period - tnow

            if delay > 0:
                time.sleep(delay)

            with self._lock:
                tlast2, luser, lpacket = self._ssrc_data.get(ssrc, (-1, None, packet))

            if next_packet.ssrc != lpacket.ssrc or tlast != tlast2 or self._end.is_set():
                continue  # another packet came in and bumped up the time

            next_data.source = luser  # is there any point in doing this?
            self.callback(luser, next_data)

            with self._lock:
                # If there was no packet update during the sleep...
                if tlast == tlast2 and ssrc in self._ssrc_data:
                    # update the existing packet time for the next window
                    self._ssrc_data[ssrc] = (tlast + PACKET_INTERVAL, user, packet)
                    self._last_timestamp[ssrc] += Decoder.SAMPLES_PER_FRAME
