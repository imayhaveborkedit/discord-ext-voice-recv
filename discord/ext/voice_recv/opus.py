# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

from typing import TYPE_CHECKING, Final

from .buffer import HeapJitterBuffer as JitterBuffer
from .rtp import FakePacket
from .utils import add_wrapped

from discord.opus import Decoder, OpusError

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

    @property
    def sink(self) -> AudioSink:
        return self.router.sink

    def _stats_inc(self, key: str, value: int = 1) -> None:
        stats = getattr(self.router.reader, 'analysis_stats', None)
        if stats:
            stats.inc(key, value)

    def _stats_add_pcm(self, pcm_len: int) -> None:
        stats = getattr(self.router.reader, 'analysis_stats', None)
        if stats:
            stats.add_pcm(pcm_len)

    def _stats_add_opus_probe(
        self,
        *,
        payload: bytes,
        frames: Optional[int],
        samples_per_frame: Optional[int],
        frame_size: Optional[int],
        header_ok: bool,
        packet: AudioPacket,
    ) -> None:
        stats = getattr(self.router.reader, 'analysis_stats', None)
        if stats:
            stats.add_opus_probe(
                ssrc=self.ssrc,
                seq=packet.sequence,
                ts=packet.timestamp,
                payload_len=len(payload),
                frames=frames,
                samples_per_frame=samples_per_frame,
                frame_size=frame_size,
                header_ok=header_ok,
            )

    def _stats_add_decode_error_sample(
        self,
        *,
        stage: str,
        packet: AudioPacket,
        payload: bytes,
        frames: Optional[int],
        samples_per_frame: Optional[int],
        frame_size: Optional[int],
        exc: Exception,
    ) -> None:
        stats = getattr(self.router.reader, 'analysis_stats', None)
        if stats:
            stats.add_decode_error_sample(
                stage=stage,
                ssrc=self.ssrc,
                seq=packet.sequence,
                ts=packet.timestamp,
                payload=payload,
                frames=frames,
                samples_per_frame=samples_per_frame,
                frame_size=frame_size,
                exc_text=str(exc),
            )

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
        self._flag_ready_state()

    def destroy(self) -> None:
        self._buffer.reset()
        self._decoder = None
        self._flag_ready_state()

    def _get_next_packet(self, timeout: float) -> Optional[AudioPacket]:
        packet = self._buffer.pop(timeout=timeout)

        if packet is None:
            if self._buffer:
                # If the next packet is not sequential yet, emit one synthetic
                # packet and advance the jitter buffer cursor for PLC/FEC flow.
                if self._buffer.gap() > 0:
                    self._buffer.advance()
                    self._stats_inc('jitter_synthetic_packets')
                    return self._make_fakepacket()
            return
        elif not packet:
            packet = self._make_fakepacket()

        return packet

    def _make_fakepacket(self) -> FakePacket:
        seq = add_wrapped(self._last_seq, 1)
        ts = add_wrapped(self._last_ts, Decoder.SAMPLES_PER_FRAME, wrap=2**32)
        return FakePacket(self.ssrc, seq, ts)

    def _process_packet(self, packet: AudioPacket) -> VoiceData:
        pcm = None
        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        member = self._get_cached_member()

        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)  # type: ignore
            member = self._get_cached_member()

        data = VoiceData(packet, member, pcm=pcm)
        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp

        return data

    def _decode_packet(self, packet: AudioPacket) -> Tuple[AudioPacket, bytes]:
        assert self._decoder is not None

        # Decode as per usual
        if packet:
            ext = getattr(packet, 'extension_data', None)
            if isinstance(ext, dict) and ext.get('_voice_recv_needs_dave_inner_decrypt'):
                self._stats_inc('dave_inner_decode_skipped')
                self._stats_add_pcm(0)
                return packet, b''

            payload: bytes = packet.decrypted_data or b''
            frames: Optional[int] = None
            samples_per_frame: Optional[int] = None
            frame_size: Optional[int] = None
            header_ok = True
            try:
                frames = self._decoder.packet_get_nb_frames(payload)
                samples_per_frame = self._decoder.packet_get_samples_per_frame(payload)
                if frames <= 0 or samples_per_frame <= 0:
                    header_ok = False
                else:
                    frame_size = frames * samples_per_frame
            except Exception:
                header_ok = False

            self._stats_add_opus_probe(
                payload=payload,
                frames=frames,
                samples_per_frame=samples_per_frame,
                frame_size=frame_size,
                header_ok=header_ok,
                packet=packet,
            )
            try:
                pcm = self._decoder.decode(payload, fec=False)
                self._stats_inc('opus_decode_ok')
            except OpusError as exc:
                log.debug(
                    "Opus decode failed for ssrc=%s seq=%s ts=%s: %s",
                    self.ssrc,
                    packet.sequence,
                    packet.timestamp,
                    exc,
                )
                # Keep pipeline alive when payload is still DAVE-wrapped or frame is damaged.
                pcm = b''
                self._stats_inc('opus_decode_err')
                self._stats_add_decode_error_sample(
                    stage='decode',
                    packet=packet,
                    payload=payload,
                    frames=frames,
                    samples_per_frame=samples_per_frame,
                    frame_size=frame_size,
                    exc=exc,
                )
            self._stats_add_pcm(len(pcm))
            return packet, pcm

        # Fake packet, need to check next one to use fec
        next_packet = self._buffer.peek_next()

        if next_packet is not None:
            nextdata: bytes = next_packet.decrypted_data  # type: ignore

            log.debug(
                "Generating fec packet: fake=%s, fec=%s",
                packet.sequence,
                next_packet.sequence,
            )
            try:
                pcm = self._decoder.decode(nextdata, fec=True)
                self._stats_inc('opus_fec_ok')
            except OpusError as exc:
                log.debug(
                    "Opus FEC decode failed for ssrc=%s fake_seq=%s next_seq=%s: %s",
                    self.ssrc,
                    packet.sequence,
                    next_packet.sequence,
                    exc,
                )
                pcm = b''
                self._stats_inc('opus_fec_err')
                self._stats_add_decode_error_sample(
                    stage='fec',
                    packet=packet,
                    payload=nextdata,
                    frames=None,
                    samples_per_frame=None,
                    frame_size=None,
                    exc=exc,
                )

        # Need to drop a packet
        else:
            try:
                pcm = self._decoder.decode(None, fec=False)
                self._stats_inc('opus_plc_ok')
            except OpusError as exc:
                log.debug("Opus PLC decode failed for ssrc=%s seq=%s: %s", self.ssrc, packet.sequence, exc)
                pcm = b''
                self._stats_inc('opus_plc_err')
                self._stats_add_decode_error_sample(
                    stage='plc',
                    packet=packet,
                    payload=b'',
                    frames=None,
                    samples_per_frame=None,
                    frame_size=None,
                    exc=exc,
                )

        self._stats_add_pcm(len(pcm))

        return packet, pcm
