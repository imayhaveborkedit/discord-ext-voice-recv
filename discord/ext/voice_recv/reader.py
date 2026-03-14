# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import json
import os
import logging
import threading
from dataclasses import dataclass

from collections import defaultdict, deque
from operator import itemgetter
from typing import TYPE_CHECKING

from . import rtp
from .dave import parse_dave_payload
from .sinks import AudioSink
from .router import PacketRouter, SinkEventRouter

try:
    import davey
except ImportError:
    davey = None

try:
    import nacl.secret
    from nacl.exceptions import CryptoError
except ImportError as e:
    raise RuntimeError("pynacl is required") from e

if TYPE_CHECKING:
    from typing import Optional, Callable, Any, Dict, Literal, Union

    from discord import Member
    from discord.types.voice import SupportedModes
    from .voice_client import VoiceRecvClient
    from .rtp import RTPPacket

    DecryptRTP = Callable[[RTPPacket], bytes]
    DecryptRTCP = Callable[[bytes], bytes]
    AfterCB = Callable[[Optional[Exception]], Any]
    SpeakingEvent = Literal['voice_member_speaking_start', 'voice_member_speaking_stop']
    EncryptionBox = Union[nacl.secret.SecretBox, nacl.secret.Aead]

log = logging.getLogger(__name__)

__all__ = [
    'AudioReader',
]


@dataclass
class PendingInnerPacket:
    packet: 'RTPPacket'
    payload: bytes
    reason: str
    queued_at: float
    attempts: int = 0


@dataclass
class PendingUnknownPacket:
    packet: 'RTPPacket'
    queued_at: float


class ReceiveAnalysisStats:
    def __init__(self, *, ws_jsonl_path: str = ""):
        self._lock = threading.Lock()
        self._ws_jsonl_lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._max_samples = 20
        self._max_non_audio_samples = 64
        self._decode_err_samples: list[Dict[str, Any]] = []
        self._dave_unhandled_samples: list[Dict[str, Any]] = []
        self._non_audio_rtp_samples: list[Dict[str, Any]] = []
        self._dave_nonce_last: dict[int, int] = {}
        self._dave_seq_last: dict[int, int] = {}
        self._voice_ws_recent_events: list[Dict[str, Any]] = []
        self._max_ws_recent = 64
        self._voice_ws_jsonl_path = ws_jsonl_path.strip()
        if self._voice_ws_jsonl_path:
            parent = os.path.dirname(self._voice_ws_jsonl_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    def inc(self, key: str, value: int = 1) -> None:
        if value == 0:
            return
        with self._lock:
            self._counters[key] += value

    def _append_sample(self, target: list[Dict[str, Any]], item: Dict[str, Any]) -> None:
        if len(target) >= self._max_samples:
            return
        target.append(item)

    @staticmethod
    def _append_ring_sample(target: list[Dict[str, Any]], item: Dict[str, Any], *, maxlen: int) -> None:
        if maxlen < 1:
            return
        if len(target) >= maxlen:
            del target[0 : len(target) - maxlen + 1]
        target.append(item)

    @staticmethod
    def _wrapped_delta(current: int, last: int, *, wrap: int) -> int:
        raw = (current - last) % wrap
        if raw > (wrap // 2):
            return raw - wrap
        return raw

    def _ws_context_unlocked(self) -> Dict[str, Any]:
        if not self._voice_ws_recent_events:
            return {}

        tail = self._voice_ws_recent_events[-5:]
        return {
            'ws_total': self._counters.get('voice_ws_total', 0),
            'ws_last_op': tail[-1].get('op'),
            'ws_recent_ops': [item.get('op') for item in tail],
            'dave_ws_total': self._counters.get('dave_ws_total', 0),
            'dave_ws_last_op': self._counters.get('dave_ws_last_op', -1),
        }

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): ReceiveAnalysisStats._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [ReceiveAnalysisStats._to_jsonable(v) for v in value]
        return repr(value)

    def _append_ws_jsonl(self, event: Dict[str, Any], event_index: int) -> None:
        if not self._voice_ws_jsonl_path:
            return

        payload = {
            'index': event_index,
            'ts_unix_ms': int(event.get('ts_unix_ms', int(time.time() * 1000))),
            'transport': event.get('transport', 'json'),
            'op': event.get('op'),
            'd': self._to_jsonable(event.get('d', {})),
            'extra': self._to_jsonable(event.get('extra', {})),
        }

        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n'
            with self._ws_jsonl_lock:
                with open(self._voice_ws_jsonl_path, 'a', encoding='utf-8') as fp:
                    fp.write(line)
            self.inc('voice_ws_jsonl_write_ok')
        except Exception:
            self.inc('voice_ws_jsonl_write_err')

    def add_voice_ws_event(self, event: Dict[str, Any]) -> None:
        interesting_keys = (
            'seq',
            'transition_id',
            'epoch',
            'generation',
            'generation_qualifier',
            'protocol_version',
            'group_id',
            'ssrc',
            'audio_ssrc',
            'video_ssrc',
            'user_id',
        )
        op = int(event.get('op', -1))
        data = event.get('d')
        payload = data if isinstance(data, dict) else {}
        extra = event.get('extra')
        extras = extra if isinstance(extra, dict) else {}
        transport = str(event.get('transport', 'json'))

        event_index = 0
        with self._lock:
            self._counters['voice_ws_total'] += 1
            self._counters[f'voice_ws_transport_{transport}'] += 1
            self._counters[f'voice_ws_op_{op}'] += 1
            self._counters['voice_ws_last_op'] = op
            event_index = self._counters['voice_ws_total']

            item: Dict[str, Any] = {
                'transport': transport,
                'op': op,
                'keys': sorted(str(k) for k in payload.keys())[:16],
            }
            seq = extras.get('seq')
            if isinstance(seq, int):
                item['seq'] = seq

            for key in interesting_keys:
                value = payload.get(key, extras.get(key))
                if isinstance(value, (str, int, float, bool)) or value is None:
                    if value is not None:
                        item[key] = value
            if transport == 'binary':
                raw_len = extras.get('raw_len')
                payload_len = extras.get('payload_len')
                if isinstance(raw_len, int):
                    item['raw_len'] = raw_len
                if isinstance(payload_len, int):
                    item['payload_len'] = payload_len

            self._voice_ws_recent_events.append(item)
            if len(self._voice_ws_recent_events) > self._max_ws_recent:
                self._voice_ws_recent_events = self._voice_ws_recent_events[-self._max_ws_recent :]

            if 21 <= op <= 31:
                self._counters['dave_ws_total'] += 1
                self._counters[f'dave_ws_transport_{transport}'] += 1
                self._counters[f'dave_ws_op_{op}'] += 1
                self._counters['dave_ws_last_op'] = op

        self._append_ws_jsonl(event, event_index)

    def add_non_audio_rtp_packet(
        self,
        *,
        kind: str,
        ssrc: int,
        seq: int,
        ts: int,
        payload_type: int,
        packet_len: int,
        known_user: bool,
        extended: bool,
    ) -> None:
        with self._lock:
            self._counters['rtp_non_audio_packets_total'] += 1
            self._counters[f'rtp_non_audio_{kind}_packets'] += 1
            self._counters[f'rtp_non_audio_payload_type_{payload_type}'] += 1

            item: Dict[str, Any] = {
                'kind': kind,
                'ssrc': ssrc,
                'seq': seq,
                'ts': ts,
                'payload_type': payload_type,
                'packet_len': packet_len,
                'known_user': known_user,
                'extended': extended,
            }
            item.update(self._ws_context_unlocked())
            self._append_ring_sample(self._non_audio_rtp_samples, item, maxlen=self._max_non_audio_samples)

    def add_pcm(self, pcm_len: int) -> None:
        with self._lock:
            self._counters['pcm_frames'] += 1
            self._counters['pcm_bytes_total'] += pcm_len
            if pcm_len == 0:
                self._counters['pcm_empty'] += 1
            if pcm_len not in (0, 3840):
                self._counters['pcm_non_20ms'] += 1

    def add_dave_nonce(self, ssrc: int, seq: int, nonce: int) -> None:
        with self._lock:
            last_nonce = self._dave_nonce_last.get(ssrc)
            last_seq = self._dave_seq_last.get(ssrc)
            if last_nonce is None:
                self._counters['dave_nonce_first'] += 1
            else:
                nonce_delta = nonce - last_nonce
                seq_delta = self._wrapped_delta(seq, last_seq, wrap=2**16) if last_seq is not None else 0

                if last_seq is not None:
                    if seq_delta == 1:
                        self._counters['dave_seq_delta1'] += 1
                    elif seq_delta > 1:
                        self._counters['dave_seq_gap_events'] += 1
                        self._counters['dave_seq_gap_total'] += seq_delta - 1
                    else:
                        self._counters['dave_seq_rewind_events'] += 1

                if nonce_delta == 1:
                    self._counters['dave_nonce_delta1'] += 1
                elif nonce_delta > 1:
                    self._counters['dave_nonce_gap_events'] += 1
                    self._counters['dave_nonce_gap_total'] += nonce_delta - 1
                    item: Dict[str, Any] = {
                        'kind': 'dave_nonce_gap',
                        'ssrc': ssrc,
                        'seq': seq,
                        'last_nonce': last_nonce,
                        'nonce': nonce,
                        'delta': nonce_delta,
                        'seq_delta': seq_delta,
                    }
                    item.update(self._ws_context_unlocked())
                    self._append_sample(
                        self._dave_unhandled_samples,
                        item,
                    )
                else:
                    self._counters['dave_nonce_rewind_events'] += 1
                    item = {
                        'kind': 'dave_nonce_rewind',
                        'ssrc': ssrc,
                        'seq': seq,
                        'last_nonce': last_nonce,
                        'nonce': nonce,
                        'delta': nonce_delta,
                        'seq_delta': seq_delta,
                    }
                    item.update(self._ws_context_unlocked())
                    self._append_sample(
                        self._dave_unhandled_samples,
                        item,
                    )

                if last_seq is not None and nonce_delta != seq_delta:
                    self._counters['dave_nonce_seq_mismatch_events'] += 1
                    item = {
                        'kind': 'dave_nonce_seq_mismatch',
                        'ssrc': ssrc,
                        'seq': seq,
                        'last_seq': last_seq,
                        'seq_delta': seq_delta,
                        'last_nonce': last_nonce,
                        'nonce': nonce,
                        'nonce_delta': nonce_delta,
                    }
                    item.update(self._ws_context_unlocked())
                    self._append_sample(self._dave_unhandled_samples, item)

            self._dave_nonce_last[ssrc] = nonce
            self._dave_seq_last[ssrc] = seq

    def add_dave_unhandled_sample(
        self,
        *,
        reason: str,
        ssrc: int,
        seq: int,
        ts: int,
        payload_len: int,
        has_marker: bool,
        ciphertext_len: Optional[int] = None,
        ranges_count: Optional[int] = None,
    ) -> None:
        with self._lock:
            item: Dict[str, Any] = {
                'kind': 'dave_unhandled',
                'reason': reason,
                'ssrc': ssrc,
                'seq': seq,
                'ts': ts,
                'payload_len': payload_len,
                'has_marker': has_marker,
                'ciphertext_len': ciphertext_len,
                'ranges_count': ranges_count,
            }
            item.update(self._ws_context_unlocked())
            self._append_sample(
                self._dave_unhandled_samples,
                item,
            )

    def add_opus_probe(
        self,
        *,
        ssrc: int,
        seq: int,
        ts: int,
        payload_len: int,
        frames: Optional[int],
        samples_per_frame: Optional[int],
        frame_size: Optional[int],
        header_ok: bool,
    ) -> None:
        with self._lock:
            self._counters['opus_probe_total'] += 1
            bucket = self._len_bucket(payload_len)
            self._counters[f'opus_len_bucket_{bucket}'] += 1
            if header_ok:
                self._counters['opus_probe_header_ok'] += 1
            else:
                self._counters['opus_probe_header_err'] += 1
                self._append_sample(
                    self._decode_err_samples,
                    {
                        'stage': 'probe',
                        'ssrc': ssrc,
                        'seq': seq,
                        'ts': ts,
                        'payload_len': payload_len,
                        'frames': frames,
                        'samples_per_frame': samples_per_frame,
                        'frame_size': frame_size,
                        'reason': 'invalid_opus_header',
                    },
                )
            if isinstance(frames, int):
                self._counters[f'opus_probe_frames_{frames}'] += 1
            if isinstance(samples_per_frame, int):
                self._counters[f'opus_probe_spf_{samples_per_frame}'] += 1
            if isinstance(frame_size, int):
                self._counters[f'opus_probe_frame_size_{frame_size}'] += 1

    def add_decode_error_sample(
        self,
        *,
        stage: str,
        ssrc: int,
        seq: int,
        ts: int,
        payload: bytes,
        frames: Optional[int],
        samples_per_frame: Optional[int],
        frame_size: Optional[int],
        exc_text: str,
    ) -> None:
        with self._lock:
            item: Dict[str, Any] = {
                'stage': stage,
                'ssrc': ssrc,
                'seq': seq,
                'ts': ts,
                'payload_len': len(payload),
                'head_hex': payload[:8].hex(),
                'tail_hex': payload[-8:].hex() if payload else '',
                'frames': frames,
                'samples_per_frame': samples_per_frame,
                'frame_size': frame_size,
                'exc': exc_text,
            }
            item.update(self._ws_context_unlocked())
            self._append_sample(
                self._decode_err_samples,
                item,
            )

    @staticmethod
    def _len_bucket(payload_len: int) -> str:
        if payload_len <= 3:
            return 'le3'
        if payload_len <= 20:
            return '4_20'
        if payload_len <= 40:
            return '21_40'
        if payload_len <= 80:
            return '41_80'
        if payload_len <= 120:
            return '81_120'
        if payload_len <= 160:
            return '121_160'
        if payload_len <= 240:
            return '161_240'
        return 'gt240'

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            decode_err_samples = list(self._decode_err_samples)
            dave_unhandled_samples = list(self._dave_unhandled_samples)
            non_audio_rtp_samples = list(self._non_audio_rtp_samples)
            voice_ws_recent_events = list(self._voice_ws_recent_events)

        pcm_frames = counters.get('pcm_frames', 0)
        decode_ok = counters.get('opus_decode_ok', 0)
        decode_err = counters.get('opus_decode_err', 0)
        decode_total = decode_ok + decode_err

        counters['pcm_avg_bytes'] = round(counters.get('pcm_bytes_total', 0) / pcm_frames, 2) if pcm_frames else 0.0
        counters['opus_decode_err_ratio'] = round((decode_err / decode_total), 4) if decode_total else 0.0
        counters['decode_err_sample_count'] = len(decode_err_samples)
        counters['dave_unhandled_sample_count'] = len(dave_unhandled_samples)
        counters['non_audio_rtp_sample_count'] = len(non_audio_rtp_samples)
        counters['voice_ws_recent_event_count'] = len(voice_ws_recent_events)
        counters['voice_ws_jsonl_path'] = self._voice_ws_jsonl_path
        if voice_ws_recent_events:
            counters['voice_ws_recent_ops'] = [
                item['op'] for item in voice_ws_recent_events[-10:] if isinstance(item.get('op'), int)
            ]
            counters['voice_ws_last_event'] = voice_ws_recent_events[-1]
        else:
            counters['voice_ws_recent_ops'] = []
            counters['voice_ws_last_event'] = {}

        dave_ws_recent_events = [item for item in voice_ws_recent_events if 21 <= int(item.get('op', -1)) <= 31]
        counters['dave_ws_recent_event_count'] = len(dave_ws_recent_events)
        if dave_ws_recent_events:
            counters['dave_ws_recent_ops'] = [
                item['op'] for item in dave_ws_recent_events[-10:] if isinstance(item.get('op'), int)
            ]
            counters['dave_ws_last_event'] = dave_ws_recent_events[-1]
        else:
            counters['dave_ws_recent_ops'] = []
            counters['dave_ws_last_event'] = {}
        counters['decode_err_samples'] = decode_err_samples
        counters['dave_unhandled_samples'] = dave_unhandled_samples
        counters['non_audio_rtp_samples'] = non_audio_rtp_samples
        counters['voice_ws_recent_events'] = voice_ws_recent_events
        counters['dave_ws_recent_events'] = dave_ws_recent_events
        return counters


class AudioReader:
    def __init__(
        self,
        sink: AudioSink,
        voice_client: VoiceRecvClient,
        *,
        after: Optional[AfterCB] = None,
        ws_jsonl_path: str = "",
    ):
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        self.sink: AudioSink = sink
        self.voice_client: VoiceRecvClient = voice_client
        self.after: Optional[AfterCB] = after

        # No need for the whole set_sink() call
        self.sink._voice_client = voice_client

        self.active: bool = False
        self.error: Optional[Exception] = None

        self.analysis_stats: ReceiveAnalysisStats = ReceiveAnalysisStats(ws_jsonl_path=ws_jsonl_path)
        self.packet_router: PacketRouter = PacketRouter(sink, self)
        self.event_router: SinkEventRouter = SinkEventRouter(sink, self)
        self.decryptor: PacketDecryptor = PacketDecryptor(
            voice_client.mode,
            bytes(voice_client.secret_key),
            voice_client=voice_client,
            stats=self.analysis_stats,
        )
        self.speaking_timer: SpeakingTimer = SpeakingTimer(self)
        self.keepalive: UDPKeepAlive = UDPKeepAlive(voice_client)
        self._unknown_ssrc_notice: set[int] = set()
        self._unknown_ssrc_drop_count: dict[int, int] = {}
        self._unknown_ssrc_last_info_at: dict[int, float] = {}
        self._unknown_ssrc_last_info_count: dict[int, int] = {}
        self._unknown_ssrc_info_every_packets = 200
        self._unknown_ssrc_info_every_sec = 5.0
        self._unexpected_rtcp_count: dict[tuple[int, str], int] = {}
        self._unexpected_rtcp_last_info_at: dict[tuple[int, str], float] = {}
        self._unexpected_rtcp_last_info_count: dict[tuple[int, str], int] = {}
        self._unexpected_rtcp_info_every_packets = 200
        self._unexpected_rtcp_info_every_sec = 5.0
        self._pending_unknown_packets: dict[int, deque[PendingUnknownPacket]] = defaultdict(deque)
        self._pending_unknown_lock = threading.Lock()
        self._pending_unknown_max_per_ssrc = 32
        self._pending_unknown_max_age_sec = 0.5
        if ws_jsonl_path:
            log.info("Voice WS capture enabled: path=%s", ws_jsonl_path)
        else:
            log.info("Voice WS capture disabled")

    def is_listening(self) -> bool:
        return self.active

    def update_secret_key(self, secret_key: bytes) -> None:
        self.decryptor.update_secret_key(secret_key)

    def start(self) -> None:
        if self.active:
            log.debug('Reader is already started', exc_info=True)
            return

        self.speaking_timer.start()
        self.event_router.start()
        self.packet_router.start()
        self.voice_client._connection.add_socket_listener(self.callback)
        self.keepalive.start()
        self.active = True

    def stop(self) -> None:
        if not self.active:
            log.debug('Tried to stop an inactive reader', exc_info=True)
            return

        self.voice_client._connection.remove_socket_listener(self.callback)
        self.active = False
        self.speaking_timer.notify()

        threading.Thread(target=self._stop, name=f'audioreader-stopper-{id(self):x}').start()

    def _stop(self) -> None:
        try:
            self.packet_router.stop()
        except Exception as e:
            self.error = e
            log.exception('Error stopping packet router')

        try:
            self.event_router.stop()
        except Exception as e:
            self.error = e
            log.exception('Error stopping event router')

        self.speaking_timer.stop()
        self.keepalive.stop()
        self.decryptor.close()

        if self.after:
            try:
                self.after(self.error)
            except Exception:
                log.exception('Error calling listener after function')

        for sink in self.sink.root.walk_children(with_self=True):
            try:
                sink.cleanup()
            except Exception:
                log.exception('Error calling cleanup() for %s', sink)

    def set_sink(self, sink: AudioSink) -> AudioSink:
        """Sets the new sink for the reader and returns the old one.
        Does not call cleanup()
        """
        # This whole function is potentially very racy
        old_sink = self.sink
        old_sink._voice_client = None
        sink._voice_client = self.voice_client
        self.packet_router.set_sink(sink)
        self.sink = sink

        return old_sink

    def _is_ip_discovery_packet(self, data: bytes) -> bool:
        return len(data) == 74 and data[1] == 0x02

    def _log_unexpected_rtcp_packet(self, packet: 'rtp.RTCPPacket', packet_data: bytes) -> None:
        packet_type = int(packet.type)
        packet_class = type(packet).__name__
        key = (packet_type, packet_class)
        now = time.monotonic()

        total = self._unexpected_rtcp_count.get(key, 0) + 1
        self._unexpected_rtcp_count[key] = total

        last_info_at = self._unexpected_rtcp_last_info_at.get(key)
        last_info_count = self._unexpected_rtcp_last_info_count.get(key, 0)

        if last_info_at is None:
            self._unexpected_rtcp_last_info_at[key] = now
            self._unexpected_rtcp_last_info_count[key] = total
            log.info(
                "Received unexpected rtcp packet: type=%s class=%s delta=%s total=%s",
                packet_type,
                packet_class,
                1,
                total,
            )
            log.debug("Unexpected RTCP packet detail: packet=%s data=%s", packet, packet_data)
            return

        should_log_info = (
            total - last_info_count >= self._unexpected_rtcp_info_every_packets
            or now - last_info_at >= self._unexpected_rtcp_info_every_sec
        )
        if should_log_info:
            delta = total - last_info_count
            window_sec = now - last_info_at
            rate = (delta / window_sec) if window_sec > 0 else 0.0
            self._unexpected_rtcp_last_info_at[key] = now
            self._unexpected_rtcp_last_info_count[key] = total
            log.info(
                "Still receiving unexpected rtcp packet: type=%s class=%s delta=%s total=%s window_sec=%.2f rate=%.2f/s",
                packet_type,
                packet_class,
                delta,
                total,
                window_sec,
                rate,
            )
        else:
            log.debug(
                "Unexpected rtcp packet: type=%s class=%s total=%s packet=%s",
                packet_type,
                packet_class,
                total,
                packet,
            )

    def _expire_pending_unknown_locked(self, now: float) -> None:
        for ssrc, queue in list(self._pending_unknown_packets.items()):
            while queue and now - queue[0].queued_at > self._pending_unknown_max_age_sec:
                queue.popleft()
                self.analysis_stats.inc('unknown_ssrc_expired')
            if not queue:
                del self._pending_unknown_packets[ssrc]

    def _flush_pending_unknown_for_ssrc(self, ssrc: int) -> list['RTPPacket']:
        now = time.monotonic()
        with self._pending_unknown_lock:
            self._expire_pending_unknown_locked(now)
            queue = self._pending_unknown_packets.pop(ssrc, None)
            if not queue:
                return []
            return [item.packet for item in queue]

    def _queue_pending_unknown_packet(self, packet: RTPPacket) -> None:
        now = time.monotonic()
        with self._pending_unknown_lock:
            self._expire_pending_unknown_locked(now)
            queue = self._pending_unknown_packets[packet.ssrc]
            queue.append(PendingUnknownPacket(packet=packet, queued_at=now))
            self.analysis_stats.inc('unknown_ssrc_queued')
            while len(queue) > self._pending_unknown_max_per_ssrc:
                queue.popleft()
                self.analysis_stats.inc('unknown_ssrc_overflow')

    def flush_pending_unknown_for_ssrc(self, ssrc: int) -> None:
        packets = self._flush_pending_unknown_for_ssrc(ssrc)
        if not packets:
            return

        for packet in packets:
            media_kind = self.voice_client._get_ssrc_media_kind(packet.ssrc)
            if media_kind != 'audio':
                self.analysis_stats.inc('unknown_ssrc_flushed_non_audio')
                continue
            self.analysis_stats.inc('unknown_ssrc_flushed')
            self.speaking_timer.notify(packet.ssrc)
            self.packet_router.feed_rtp(packet)

    def _route_rtp_packet(self, rtp_packet: RTPPacket) -> None:
        ssrc = rtp_packet.ssrc

        if ssrc in self.voice_client._ssrc_to_id:
            self._unknown_ssrc_notice.discard(ssrc)
            self._unknown_ssrc_drop_count.pop(ssrc, None)
            self._unknown_ssrc_last_info_at.pop(ssrc, None)
            self._unknown_ssrc_last_info_count.pop(ssrc, None)

            pending_packets = self._flush_pending_unknown_for_ssrc(ssrc)
            for packet in pending_packets:
                media_kind = self.voice_client._get_ssrc_media_kind(packet.ssrc)
                if media_kind != 'audio':
                    self.analysis_stats.inc('unknown_ssrc_flushed_non_audio')
                    continue
                self.analysis_stats.inc('unknown_ssrc_flushed')
                self.speaking_timer.notify(packet.ssrc)
                self.packet_router.feed_rtp(packet)

        if ssrc not in self.voice_client._ssrc_to_id:
            self.analysis_stats.inc('rtp_unknown_ssrc_dropped')
            if rtp_packet.is_silence():
                self.analysis_stats.inc('rtp_unknown_ssrc_silence_dropped')
                return

            media_kind = self.voice_client._get_ssrc_media_kind(ssrc)
            if media_kind == 'unknown':
                self._queue_pending_unknown_packet(rtp_packet)

            drop_count = self._unknown_ssrc_drop_count.get(ssrc, 0) + 1
            self._unknown_ssrc_drop_count[ssrc] = drop_count
            now = time.monotonic()
            payload_size = len(rtp_packet.data)

            if ssrc not in self._unknown_ssrc_notice:
                self._unknown_ssrc_notice.add(ssrc)
                self._unknown_ssrc_last_info_at[ssrc] = now
                self._unknown_ssrc_last_info_count[ssrc] = drop_count
                log.info(
                    "Received packet for unknown ssrc=%s kind=%s dropped=%s seq=%s ts=%s payload=%s",
                    ssrc,
                    media_kind,
                    drop_count,
                    rtp_packet.sequence,
                    rtp_packet.timestamp,
                    payload_size,
                )
            else:
                last_info_at = self._unknown_ssrc_last_info_at.get(ssrc, 0.0)
                last_info_count = self._unknown_ssrc_last_info_count.get(ssrc, 0)
                should_log_info = (
                    drop_count - last_info_count >= self._unknown_ssrc_info_every_packets
                    or now - last_info_at >= self._unknown_ssrc_info_every_sec
                )
                if should_log_info:
                    self._unknown_ssrc_last_info_at[ssrc] = now
                    self._unknown_ssrc_last_info_count[ssrc] = drop_count
                    log.info(
                        "Still receiving unknown ssrc=%s kind=%s dropped=%s last_seq=%s last_ts=%s payload=%s",
                        ssrc,
                        media_kind,
                        drop_count,
                        rtp_packet.sequence,
                        rtp_packet.timestamp,
                        payload_size,
                    )
                else:
                    log.debug(
                        "Received packet for unknown ssrc=%s kind=%s dropped=%s seq=%s",
                        ssrc,
                        media_kind,
                        drop_count,
                        rtp_packet.sequence,
                    )
            return

        self.speaking_timer.notify(ssrc)
        self.packet_router.feed_rtp(rtp_packet)

    def callback(self, packet_data: bytes) -> None:
        packet = rtp_packet = rtcp_packet = None
        recovered_rtp_packets: list[RTPPacket] = []
        defer_current_rtp = False
        try:
            if not rtp.is_rtcp(packet_data):
                packet = rtp_packet = rtp.decode_rtp(packet_data)
                media_kind = self.voice_client._get_ssrc_media_kind(packet.ssrc)
                self.analysis_stats.inc('rtp_media_packets_total')
                self.analysis_stats.inc(f'rtp_media_{media_kind}_packets')

                if media_kind in ('video', 'screen', 'rtx', 'test'):
                    self.analysis_stats.add_non_audio_rtp_packet(
                        kind=media_kind,
                        ssrc=packet.ssrc,
                        seq=packet.sequence,
                        ts=packet.timestamp,
                        payload_type=packet.payload,
                        packet_len=len(packet_data),
                        known_user=(self.voice_client._get_id_from_ssrc(packet.ssrc) is not None),
                        extended=packet.extended,
                    )
                    return

                packet.decrypted_data = self.decryptor.decrypt_rtp(packet)
                self.analysis_stats.inc('rtp_packets_total')
                self.analysis_stats.inc(f'rtp_payload_type_{packet.payload}')
                recovered_rtp_packets = self.decryptor.pop_recovered_rtp_packets()
                defer_current_rtp = self.decryptor.is_deferred_packet(packet)
                if any(recovered is packet for recovered in recovered_rtp_packets):
                    defer_current_rtp = True
            else:
                packet = rtcp_packet = rtp.decode_rtcp(self.decryptor.decrypt_rtcp(packet_data))
                self.analysis_stats.inc('rtcp_packets_total')
                self.analysis_stats.inc(f'rtcp_type_{packet.type}')

                # RFC 3550 defines RTCP PT=200 as Sender Report (SR) and
                # PT=201 as Receiver Report (RR) (see RFC 3550 section 6.4).
                # Compound RTCP traffic is expected to begin with SR or RR
                # (section 6.1), so both are treated as normal baseline
                # control-plane packets here.
                # Only non-SR/RR RTCP classes are logged as unexpected/noisy.
                if not isinstance(packet, (rtp.ReceiverReportPacket, rtp.SenderReportPacket)):
                    self._log_unexpected_rtcp_packet(packet, packet_data)
        except CryptoError as e:
            log.error("CryptoError decoding packet data")
            log.debug("CryptoError details:\n  data=%s\n  secret_key=%s", packet_data, self.voice_client.secret_key)
            return
        except Exception as e:
            if self._is_ip_discovery_packet(packet_data):
                log.debug("Ignoring ip discovery packet")
                return

            log.exception("Error unpacking packet")
            log.debug("Packet data: len=%s data=%s", len(packet_data), packet_data)

        if self.error:
            self.stop()
            return
        if not packet:
            return

        if rtcp_packet:
            self.packet_router.feed_rtcp(rtcp_packet)
        elif rtp_packet:
            try:
                for recovered in recovered_rtp_packets:
                    self._route_rtp_packet(recovered)

                if defer_current_rtp:
                    self.analysis_stats.inc('dave_inner_defer_current_skipped')
                    return

                self._route_rtp_packet(rtp_packet)
            except Exception as e:
                log.exception('Error processing rtp packet')
                self.error = e
                self.stop()


class PacketDecryptor:
    supported_modes: list[SupportedModes] = [
        'aead_xchacha20_poly1305_rtpsize',
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    ]

    def __init__(
        self,
        mode: SupportedModes,
        secret_key: bytes,
        *,
        voice_client: Optional[VoiceRecvClient] = None,
        stats: Optional[ReceiveAnalysisStats] = None,
    ) -> None:
        self.mode: SupportedModes = mode
        try:
            self.decrypt_rtp: DecryptRTP = getattr(self, '_decrypt_rtp_' + mode)
            self.decrypt_rtcp: DecryptRTCP = getattr(self, '_decrypt_rtcp_' + mode)
        except AttributeError as e:
            raise NotImplementedError(mode) from e

        self.box: EncryptionBox = self._make_box(secret_key)
        self._voice_client = voice_client
        self._stats = stats
        self._dave_audio_strip_logged: bool = False
        self._pending_inner_packets: dict[int, list[PendingInnerPacket]] = defaultdict(list)
        self._pending_inner_ready: list[RTPPacket] = []
        self._pending_inner_max_per_ssrc = 128
        self._pending_inner_max_attempts = 16
        self._pending_inner_max_age_sec = 2.5

    def _make_box(self, secret_key: bytes) -> EncryptionBox:
        if self.mode.startswith("aead"):
            return nacl.secret.Aead(secret_key)
        else:
            return nacl.secret.SecretBox(secret_key)

    def update_secret_key(self, secret_key: bytes) -> None:
        self.box = self._make_box(secret_key)

    def close(self) -> None:
        return

    def _inc(self, key: str, value: int = 1) -> None:
        if self._stats:
            self._stats.inc(key, value)

    def _add_dave_nonce(self, *, ssrc: int, seq: int, nonce: int) -> None:
        if self._stats:
            self._stats.add_dave_nonce(ssrc, seq, nonce)

    def _add_dave_unhandled_sample(
        self,
        *,
        reason: str,
        packet: RTPPacket,
        payload_len: int,
        has_marker: bool,
        ciphertext_len: Optional[int] = None,
        ranges_count: Optional[int] = None,
    ) -> None:
        if self._stats:
            self._stats.add_dave_unhandled_sample(
                reason=reason,
                ssrc=packet.ssrc,
                seq=packet.sequence,
                ts=packet.timestamp,
                payload_len=payload_len,
                has_marker=has_marker,
                ciphertext_len=ciphertext_len,
                ranges_count=ranges_count,
            )

    @staticmethod
    def _is_retryable_inner_reason(reason: str) -> bool:
        return reason in {
            'no_session',
            'session_not_ready',
            'no_user_id',
            'decrypt_error',
        }

    def _defer_pending_inner_packet(
        self,
        *,
        packet: RTPPacket,
        payload: bytes,
        reason: str,
        ranges_count: int,
    ) -> None:
        queue = self._pending_inner_packets[packet.ssrc]
        queue.append(
            PendingInnerPacket(
                packet=packet,
                payload=bytes(payload),
                reason=reason,
                queued_at=time.monotonic(),
            )
        )

        packet.extension_data['_voice_recv_pending_inner_decrypt'] = True
        packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = True
        packet.extension_data['_voice_recv_dave_ranges_count'] = ranges_count

        self._inc('dave_inner_defer_queued')
        self._inc(f'dave_inner_defer_reason_{reason}')

        if len(queue) <= self._pending_inner_max_per_ssrc:
            return

        dropped = queue.pop(0)
        dropped.packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
        dropped.packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = True
        self._inc('dave_inner_defer_drop_overflow')
        self._add_dave_unhandled_sample(
            reason='inner_defer_overflow',
            packet=dropped.packet,
            payload_len=len(dropped.payload),
            has_marker=True,
            ranges_count=dropped.packet.extension_data.get('_voice_recv_dave_ranges_count'),
        )

    def _drain_pending_inner_packets(self) -> None:
        if not self._pending_inner_packets:
            return

        now = time.monotonic()
        for ssrc, queue in list(self._pending_inner_packets.items()):
            kept: list[PendingInnerPacket] = []
            for pending in queue:
                age = now - pending.queued_at
                if age > self._pending_inner_max_age_sec:
                    pending.packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
                    self._inc('dave_inner_defer_drop_expired')
                    self._add_dave_unhandled_sample(
                        reason='inner_defer_expired',
                        packet=pending.packet,
                        payload_len=len(pending.payload),
                        has_marker=True,
                        ranges_count=pending.packet.extension_data.get('_voice_recv_dave_ranges_count'),
                    )
                    continue

                plain, reason = self._try_dave_inner_decrypt(
                    pending.packet,
                    pending.payload,
                    emit_error_sample=False,
                )
                if plain is not None:
                    pending.packet.decrypted_data = plain
                    pending.packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
                    pending.packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = False
                    pending.packet.extension_data['_voice_recv_dave_ranges_count'] = 0
                    pending.packet.extension_data['_voice_recv_dave_inner_deferred_recovered'] = True
                    self._pending_inner_ready.append(pending.packet)
                    self._inc('dave_inner_defer_recovered')
                    continue

                pending.attempts += 1
                retryable = self._is_retryable_inner_reason(reason)
                if retryable and pending.attempts < self._pending_inner_max_attempts:
                    kept.append(pending)
                    continue

                pending.packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
                if retryable:
                    self._inc('dave_inner_defer_drop_attempts')
                    drop_reason = 'inner_defer_drop_attempts'
                else:
                    self._inc('dave_inner_defer_drop_unrecoverable')
                    drop_reason = f'inner_defer_drop_{reason}'
                self._add_dave_unhandled_sample(
                    reason=drop_reason,
                    packet=pending.packet,
                    payload_len=len(pending.payload),
                    has_marker=True,
                    ranges_count=pending.packet.extension_data.get('_voice_recv_dave_ranges_count'),
                )

            if kept:
                self._pending_inner_packets[ssrc] = kept
            else:
                del self._pending_inner_packets[ssrc]

    def pop_recovered_rtp_packets(self) -> list[RTPPacket]:
        self._drain_pending_inner_packets()
        if not self._pending_inner_ready:
            return []

        ready = self._pending_inner_ready
        self._pending_inner_ready = []
        return ready

    @staticmethod
    def is_deferred_packet(packet: RTPPacket) -> bool:
        return bool(packet.extension_data.get('_voice_recv_pending_inner_decrypt'))

    def _try_dave_inner_decrypt(
        self,
        packet: RTPPacket,
        payload: bytes,
        *,
        emit_error_sample: bool = True,
    ) -> tuple[Optional[bytes], str]:
        if davey is None:
            self._inc('dave_inner_decrypt_no_davey')
            return None, 'no_davey'

        if self._voice_client is None:
            self._inc('dave_inner_decrypt_no_voice_client')
            return None, 'no_voice_client'

        state = getattr(self._voice_client, '_connection', None)
        session = getattr(state, 'dave_session', None)
        if session is None:
            self._inc('dave_inner_decrypt_no_session')
            return None, 'no_session'

        if not getattr(session, 'ready', False):
            self._inc('dave_inner_decrypt_session_not_ready')
            return None, 'session_not_ready'

        user_id = self._voice_client._get_id_from_ssrc(packet.ssrc)
        if user_id is None:
            self._inc('dave_inner_decrypt_no_user_id')
            return None, 'no_user_id'

        try:
            decrypted = session.decrypt(int(user_id), davey.MediaType.audio, bytes(payload))
            decrypted_bytes = bytes(decrypted)
        except Exception as exc:
            self._inc('dave_inner_decrypt_err')
            if emit_error_sample:
                self._add_dave_unhandled_sample(
                    reason='inner_decrypt_error',
                    packet=packet,
                    payload_len=len(payload),
                    has_marker=True,
                )
            log.debug(
                "DAVE inner decrypt failed: ssrc=%s user_id=%s seq=%s ts=%s err=%s",
                packet.ssrc,
                user_id,
                packet.sequence,
                packet.timestamp,
                exc,
            )
            return None, 'decrypt_error'

        self._inc('dave_inner_decrypt_ok')
        packet.extension_data['_voice_recv_dave_inner_decrypted'] = True
        packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = False
        packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
        return decrypted_bytes, 'ok'

    def _decrypt_rtp_transport_aead_xchacha20_poly1305_rtpsize(self, packet: RTPPacket) -> bytes:
        packet.adjust_rtpsize()

        nonce = bytearray(24)
        nonce[:4] = packet.nonce
        voice_data = packet.data

        assert isinstance(self.box, nacl.secret.Aead)
        result = self.box.decrypt(bytes(voice_data), bytes(packet.header), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtp_xsalsa20_poly1305(self, packet: RTPPacket) -> bytes:
        nonce = bytearray(24)
        nonce[:12] = packet.header
        result = self.box.decrypt(bytes(packet.data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305(self, data: bytes) -> bytes:
        nonce = bytearray(24)
        nonce[:8] = data[:8]
        result = self.box.decrypt(data[8:], bytes(nonce))

        return data[:8] + result

    def _decrypt_rtp_xsalsa20_poly1305_suffix(self, packet: RTPPacket) -> bytes:
        nonce = packet.data[-24:]
        voice_data = packet.data[:-24]
        result = self.box.decrypt(bytes(voice_data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305_suffix(self, data: bytes) -> bytes:
        nonce = data[-24:]
        header = data[:8]
        result = self.box.decrypt(data[8:-24], nonce)

        return header + result

    def _decrypt_rtp_xsalsa20_poly1305_lite(self, packet: RTPPacket) -> bytes:
        nonce = bytearray(24)
        nonce[:4] = packet.data[-4:]
        voice_data = packet.data[:-4]
        result = self.box.decrypt(bytes(voice_data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305_lite(self, data: bytes) -> bytes:
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]
        result = self.box.decrypt(data[8:-4], bytes(nonce))

        return header + result

    def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self, packet: RTPPacket) -> bytes:
        result = self._decrypt_rtp_transport_aead_xchacha20_poly1305_rtpsize(packet)

        has_marker = len(result) >= 2 and result[-2:] == b'\xfa\xfa'
        parsed = parse_dave_payload(result)

        if has_marker:
            self._inc('dave_marker_packets')

        if parsed:
            self._inc('dave_parse_ok')
            self._add_dave_nonce(ssrc=packet.ssrc, seq=packet.sequence, nonce=parsed.nonce)
            packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = True
            packet.extension_data['_voice_recv_pending_inner_decrypt'] = False
            packet.extension_data['_voice_recv_dave_nonce'] = parsed.nonce
            packet.extension_data['_voice_recv_dave_ranges_count'] = parsed.ranges_count
            self._inc('dave_needs_inner_decrypt_packets')

            inner_plain, inner_reason = self._try_dave_inner_decrypt(packet, result)
            if inner_plain is not None:
                packet.extension_data['_voice_recv_dave_ranges_count'] = 0
                return inner_plain

            if parsed.ranges_count == 0 and 0 < parsed.ciphertext_len <= len(result):
                result = result[:parsed.ciphertext_len]
                packet.extension_data['_voice_recv_needs_dave_inner_decrypt'] = False
                packet.extension_data['_voice_recv_dave_ranges_count'] = 0
                self._inc('dave_strip_success')
                if not self._dave_audio_strip_logged:
                    self._dave_audio_strip_logged = True
                    log.info(
                        "DAVE audio strip enabled: marker detected, trimming supplemental trailer before Opus decode"
                    )
            else:
                self._inc('dave_inner_unresolved_packets')
                if parsed.ranges_count > 0:
                    self._inc('dave_ranges_nonzero')
                    if self._is_retryable_inner_reason(inner_reason):
                        self._defer_pending_inner_packet(
                            packet=packet,
                            payload=result,
                            reason=inner_reason,
                            ranges_count=parsed.ranges_count,
                        )
                        self._inc('dave_ranges_nonzero_deferred')
                        self._add_dave_unhandled_sample(
                            reason=f'ranges_nonzero_deferred_{inner_reason}',
                            packet=packet,
                            payload_len=len(result),
                            has_marker=has_marker,
                            ciphertext_len=parsed.ciphertext_len,
                            ranges_count=parsed.ranges_count,
                        )
                    else:
                        self._add_dave_unhandled_sample(
                            reason=f'ranges_nonzero_{inner_reason}',
                            packet=packet,
                            payload_len=len(result),
                            has_marker=has_marker,
                            ciphertext_len=parsed.ciphertext_len,
                            ranges_count=parsed.ranges_count,
                        )
                else:
                    self._add_dave_unhandled_sample(
                        reason=f'invalid_ciphertext_len_{inner_reason}',
                        packet=packet,
                        payload_len=len(result),
                        has_marker=has_marker,
                        ciphertext_len=parsed.ciphertext_len,
                        ranges_count=parsed.ranges_count,
                    )
                self._inc('dave_strip_unhandled')
        else:
            if has_marker:
                self._inc('dave_parse_fail')
                self._inc('dave_strip_unhandled')
                self._add_dave_unhandled_sample(
                    reason='parse_fail',
                    packet=packet,
                    payload_len=len(result),
                    has_marker=has_marker,
                )
            else:
                self._inc('dave_non_marker_packets')

        return result

    def _decrypt_rtcp_aead_xchacha20_poly1305_rtpsize(self, data: bytes) -> bytes:
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]

        assert isinstance(self.box, nacl.secret.Aead)
        result = self.box.decrypt(data[8:-4], bytes(header), bytes(nonce))

        return header + result

class SpeakingTimer(threading.Thread):
    def __init__(self, reader: AudioReader):
        super().__init__(daemon=True, name=f'speaking-timer-{id(self):x}')

        self.reader: AudioReader = reader
        self.voice_client = reader.voice_client
        self.speaking_timeout_delay: float = 0.2
        self.last_speaking_state: Dict[int, bool] = {}
        self.speaking_cache: Dict[int, float] = {}
        self.speaking_timer_event: threading.Event = threading.Event()
        self._end_thread: threading.Event = threading.Event()

    def _lookup_member(self, ssrc: int) -> Optional[Member]:
        whoid = self.voice_client._get_id_from_ssrc(ssrc)
        return self.voice_client.guild.get_member(whoid) if whoid else None

    def maybe_dispatch_speaking_start(self, ssrc: int) -> None:
        tlast = self.speaking_cache.get(ssrc)
        if tlast is None or tlast + self.speaking_timeout_delay < time.perf_counter():
            self.dispatch('voice_member_speaking_start', ssrc)

    def dispatch(self, event: SpeakingEvent, ssrc: int) -> None:
        who = self._lookup_member(ssrc)
        if not who:
            return
        self.voice_client.dispatch_sink(event, who)

    def notify(self, ssrc: Optional[int] = None) -> None:
        if ssrc is not None:
            self.last_speaking_state[ssrc] = True
            self.maybe_dispatch_speaking_start(ssrc)
            self.speaking_cache[ssrc] = time.perf_counter()

        self.speaking_timer_event.set()
        self.speaking_timer_event.clear()

    def drop_ssrc(self, ssrc: int) -> None:
        self.speaking_cache.pop(ssrc, None)
        state = self.last_speaking_state.pop(ssrc, None)
        if state:
            self.dispatch('voice_member_speaking_stop', ssrc)
        self.notify()

    def get_speaking(self, ssrc: int) -> Optional[bool]:
        return self.last_speaking_state.get(ssrc)

    def stop(self) -> None:
        self._end_thread.set()
        self.notify()

    def run(self) -> None:
        _i1 = itemgetter(1)

        def get_next_entry():
            cache = sorted(self.speaking_cache.items(), key=_i1)
            for ssrc, tlast in cache:
                # only return pair if speaking
                if self.last_speaking_state.get(ssrc):
                    return ssrc, tlast

            return None, None

        self.speaking_timer_event.wait()
        while not self._end_thread.is_set():
            if not self.speaking_cache:
                self.speaking_timer_event.wait()

            tnow = time.perf_counter()
            ssrc, tlast = get_next_entry()

            # no ssrc has been speaking, nothing to timeout
            if ssrc is None or tlast is None:
                self.speaking_timer_event.wait()
                continue

            self.speaking_timer_event.wait(tlast + self.speaking_timeout_delay - tnow)

            if time.perf_counter() < tlast + self.speaking_timeout_delay:
                continue

            self.dispatch('voice_member_speaking_stop', ssrc)
            self.last_speaking_state[ssrc] = False


# TODO: unify into a single thread that does all keepalives
class UDPKeepAlive(threading.Thread):
    delay: int = 5000

    def __init__(self, voice_client: VoiceRecvClient):
        super().__init__(daemon=True, name=f"voice-udp-keepalive-{id(self):x}")

        self.voice_client: VoiceRecvClient = voice_client

        self.last_time: float = 0
        self.counter: int = 0
        self._end_thread: threading.Event = threading.Event()

    def run(self) -> None:
        self.voice_client.wait_until_connected()

        while not self._end_thread.is_set():
            vc = self.voice_client
            try:
                packet = self.counter.to_bytes(8, 'big')
            except OverflowError:
                self.counter = 0
                continue

            try:
                vc._connection.socket.sendto(packet, (vc._connection.endpoint_ip, vc._connection.voice_port))
            except Exception as e:
                log.debug("Error sending keepalive to socket: %s: %s", e.__class__.__name__, e)
                # TODO: test connection interruptions
                vc.wait_until_connected()
                if vc.is_connected():
                    continue
                break
            else:
                self.counter += 1
                time.sleep(self.delay)

    def stop(self) -> None:
        self._end_thread.set()
