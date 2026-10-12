# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

from typing import TYPE_CHECKING, Final, Set

from .buffer import HeapJitterBuffer as JitterBuffer
from .rtp import FakePacket
from .utils import add_wrapped

from discord.opus import Decoder, OpusError

try:
    import davey
except ImportError:  # pragma: no cover
    davey = None

if TYPE_CHECKING:
    from typing import Optional, Tuple, Dict, Callable, Any
    from .rtp import AudioPacket
    from .sinks import AudioSink
    from .router import PacketRouter
    from .voice_client import VoiceRecvClient
    from .types import MemberOrUser as User

    EventCB = Callable[..., Any]
    EventData = Tuple[str, Tuple[Any, ...], Dict[str, Any]]

log = logging.getLogger(__name__)

__all__ = [
    'VoiceData',
]


class VoiceData:
    """Container object for audio data and source user."""

    __slots__ = ('packet', 'source', 'pcm')

    def __init__(self, packet: AudioPacket, source: Optional[User], *, pcm: Optional[bytes] = None):
        self.packet: AudioPacket = packet
        self.source: Optional[User] = source
        self.pcm: bytes = pcm if pcm else b''

    @property
    def opus(self) -> Optional[bytes]:
        return self.packet.decrypted_data


class PacketDecoder:
    def __init__(self, router: PacketRouter, ssrc: int):
        self.router: PacketRouter = router
        self.ssrc: int = ssrc

        self._decoder: Optional[Decoder] = None if self.sink.wants_opus() else Decoder()
        self._buffer: JitterBuffer = JitterBuffer()
        self._cached_id: Optional[int] = None

        self._last_seq: int = -1
        self._last_ts: int = -1

        # DAVE bookkeeping.  The FEC path peeks at the *next* packet before it is popped, so
        # frames can get decrypted ahead of time; these remember what already happened to a
        # sequence number so a frame is never decrypted twice (which always fails).
        self._dave_ok: Set[int] = set()
        self._dave_bad: Set[int] = set()
        self._dave_drops: int = 0
        self._opus_errors: int = 0

    @property
    def sink(self) -> AudioSink:
        return self.router.sink

    def _get_user(self, user_id: int) -> Optional[User]:
        vc: VoiceRecvClient = self.sink.voice_client  # type: ignore
        return vc.guild.get_member(user_id) or vc.client.get_user(user_id)

    def _get_cached_member(self) -> Optional[User]:
        return self._get_user(self._cached_id) if self._cached_id else None

    def _flag_ready_state(self):
        if self._buffer.peek():
            self.router.waiter.register(self)
        else:
            self.router.waiter.unregister(self)

    def push_packet(self, packet: AudioPacket) -> None:
        self._buffer.push(packet)
        self._flag_ready_state()

    def pop_data(self, *, timeout: float = 0) -> Optional[VoiceData]:
        packet = self._get_next_packet(timeout)
        self._flag_ready_state()

        if packet is None:
            return

        return self._process_packet(packet)

    def set_user_id(self, user_id: int) -> None:
        self._cached_id = user_id

    def reset(self) -> None:
        self._buffer.reset()
        self._decoder = None if self.sink.wants_opus() else Decoder()
        self._last_seq = self._last_ts = -1
        self._dave_ok.clear()
        self._dave_bad.clear()
        self._flag_ready_state()

    def destroy(self) -> None:
        self._buffer.reset()
        self._decoder = None
        self._flag_ready_state()

    def _get_next_packet(self, timeout: float) -> Optional[AudioPacket]:
        packet = self._buffer.pop(timeout=timeout)

        if packet is None:
            # Gets the last (buffered) packet out (i think)
            # TODO: revist this, might be an issue
            if self._buffer:
                packets = self._buffer.flush()
                if any(packets[1:]):
                    log.warning(
                        "%s packets were lost being flushed in decoder-%s\n --> (last=%s) %s",
                        len(packets) - 1,
                        self.ssrc,
                        self._last_seq,
                        [p.sequence for p in packets],
                    )
                return packets[0]
            return
        elif not packet:
            packet = self._make_fakepacket()

        return packet

    def _make_fakepacket(self) -> FakePacket:
        seq = add_wrapped(self._last_seq, 1)
        ts = add_wrapped(self._last_ts, Decoder.SAMPLES_PER_FRAME, wrap=2**32)
        return FakePacket(self.ssrc, seq, ts)

    def _process_packet(self, packet: AudioPacket) -> Optional[VoiceData]:
        pcm = None

        member = self._get_cached_member()

        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)  # type: ignore
            member = self._get_cached_member()

        # Keep the sequence/timestamp bookkeeping correct even if the frame ends up dropped,
        # otherwise the jitter buffer would think a packet was lost.
        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp

        if not self._dave_decrypt(packet):
            return None

        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        return VoiceData(packet, member, pcm=pcm)

    def _dave_log_drop(self, reason: str) -> None:
        self._dave_drops += 1
        if self._dave_drops == 1 or self._dave_drops % 250 == 0:
            log.warning('DAVE: dropped %s undecryptable frame(s) on ssrc %s (%s)', self._dave_drops, self.ssrc, reason)

    def _dave_decrypt(self, packet: AudioPacket, *, peek: bool = False) -> bool:
        """Strips the DAVE (E2EE) layer from a frame, in place.

        Discord voice calls are end-to-end encrypted: after the normal transport decryption every
        real voice frame is still MLS ciphertext, and feeding it to opus gives ``corrupted stream``.
        The decryption keys live in the :class:`davey.DaveSession` discord.py already maintains.

        Returns ``True`` if the frame is usable (decrypted, or never encrypted in the first place) and
        ``False`` if it can't be decrypted and must be dropped.  Never raises.
        """
        # Fake (lost) packets, and anything without payload, have nothing to decrypt.
        if davey is None or not packet or not packet.decrypted_data:
            return True

        seq = packet.sequence
        if not peek:
            if seq in self._dave_ok:
                self._dave_ok.discard(seq)
                return True
            if seq in self._dave_bad:
                self._dave_bad.discard(seq)
                return False

        ok = self._dave_decrypt_inner(packet)

        if peek:
            if len(self._dave_ok) + len(self._dave_bad) > 64:
                self._dave_ok.clear()
                self._dave_bad.clear()
            (self._dave_ok if ok else self._dave_bad).add(seq)

        return ok

    def _dave_decrypt_inner(self, packet: AudioPacket) -> bool:
        # Silence/keepalive frames are sent as plaintext and decode fine as-is.
        if packet.is_silence():
            return True

        vc = self.sink.voice_client
        state = getattr(vc, '_connection', None)

        if not getattr(state, 'dave_protocol_version', 0):
            return True  # not an E2EE call (or downgraded to protocol 0): plain opus

        session = getattr(state, 'dave_session', None)
        if session is None or not session.ready:
            self._dave_log_drop('session not ready')
            return False

        user_id = self._cached_id
        if user_id is None:
            user_id = self._cached_id = vc._get_id_from_ssrc(self.ssrc)  # type: ignore
        if user_id is None:
            # Without the sender we can't pick the right key ratchet.
            self._dave_log_drop('ssrc not mapped to a user yet')
            return False

        try:
            packet.decrypted_data = session.decrypt(int(user_id), davey.MediaType.audio, bytes(packet.decrypted_data))
            return True
        except BaseException as e:  # davey/pyo3 panics are BaseExceptions
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if 'UnencryptedWhenPassthroughDisabled' in (str(e) or repr(e)):
                # Sender is still transmitting plain opus (not in the MLS group yet / transition gap):
                # the frame has no DAVE layer, so hand it to opus untouched instead of dropping it.
                return True
            self._dave_log_drop(str(e) or type(e).__name__)
            return False

    def _decode_safe(self, data: Optional[bytes], *, fec: bool) -> bytes:
        """Opus decode that can never take the whole packet router down."""
        assert self._decoder is not None

        try:
            return self._decoder.decode(data, fec=fec)
        except OpusError as e:
            self._opus_errors += 1
            if self._opus_errors == 1 or self._opus_errors % 250 == 0:
                log.warning('Opus decode error on ssrc %s (%s total): %s', self.ssrc, self._opus_errors, e)

        # Try packet loss concealment, and as a last resort emit one frame of silence so the
        # stream timing stays intact.
        try:
            return self._decoder.decode(None, fec=False)
        except OpusError:
            return b'\x00' * Decoder.FRAME_SIZE

    def _decode_packet(self, packet: AudioPacket) -> Tuple[AudioPacket, bytes]:
        assert self._decoder is not None

        # Decode as per usual
        if packet:
            pcm = self._decode_safe(packet.decrypted_data, fec=False)
            return packet, pcm

        # Fake packet, need to check next one to use fec
        next_packet = self._buffer.peek_next()

        # The next packet is still sitting in the buffer, so it has not been through DAVE yet.
        if next_packet is not None and self._dave_decrypt(next_packet, peek=True):
            nextdata: bytes = next_packet.decrypted_data  # type: ignore

            log.debug(
                "Generating fec packet: fake=%s, fec=%s",
                packet.sequence,
                next_packet.sequence,
            )
            pcm = self._decode_safe(nextdata, fec=True)

        # Need to drop a packet
        else:
            pcm = self._decode_safe(None, fec=False)

        return packet, pcm
