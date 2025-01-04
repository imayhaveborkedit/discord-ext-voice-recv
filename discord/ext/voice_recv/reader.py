# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import logging
import threading

from operator import itemgetter
from typing import TYPE_CHECKING

from . import rtp
from .sinks import AudioSink
from .router import PacketRouter, SinkEventRouter

try:
    import nacl.secret
    from nacl.exceptions import CryptoError
except ImportError as e:
    raise RuntimeError("pynacl is required") from e

if TYPE_CHECKING:
    from typing import Optional, Callable, Any, Dict, Literal

    from discord import Member
    from discord.types.voice import SupportedModes
    from .voice_client import VoiceRecvClient
    from .rtp import RTPPacket

    DecryptRTP = Callable[[RTPPacket], bytes]
    DecryptRTCP = Callable[[bytes], bytes]
    AfterCB = Callable[[Optional[Exception]], Any]
    SpeakingEvent = Literal['voice_member_speaking_start', 'voice_member_speaking_stop']

log = logging.getLogger(__name__)

__all__ = [
    'AudioReader',
]


class AudioReader:
    def __init__(self, sink: AudioSink, voice_client: VoiceRecvClient, *, after: Optional[AfterCB] = None):
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        self.sink: AudioSink = sink
        self.voice_client: VoiceRecvClient = voice_client
        self.after: Optional[AfterCB] = after

        # No need for the whole set_sink() call
        self.sink._voice_client = voice_client

        self.active: bool = False
        self.error: Optional[Exception] = None
        self.packet_router: PacketRouter = PacketRouter(sink, self)
        self.event_router: SinkEventRouter = SinkEventRouter(sink, self)
        self.decryptor: PacketDecryptor = PacketDecryptor(voice_client.mode, bytes(voice_client.secret_key))
        self.speaking_timer: SpeakingTimer = SpeakingTimer(self)

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

    def callback(self, packet_data: bytes) -> None:
        packet = rtp_packet = rtcp_packet = None
        try:
            if not rtp.is_rtcp(packet_data):
                packet = rtp_packet = rtp.decode_rtp(packet_data)
                packet.decrypted_data = self.decryptor.decrypt_rtp(packet)
            else:
                packet = rtcp_packet = rtp.decode_rtcp(self.decryptor.decrypt_rtcp(packet_data))

                if not isinstance(packet, rtp.ReceiverReportPacket):
                    log.info("Received unexpected rtcp packet: type=%s, %s", packet.type, type(packet))
                    log.debug("Packet info:\n  packet=%s\n  data=%s", packet, packet_data)
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
        finally:
            if self.error:
                self.stop()
                return
            if not packet:
                return

        if rtcp_packet:
            self.packet_router.feed_rtcp(rtcp_packet)
        elif rtp_packet:
            ssrc = rtp_packet.ssrc

            if ssrc not in self.voice_client._ssrc_to_id:
                if rtp_packet.is_silence():
                    # TODO: buffer packets from unknown ssrcs, 50 max?
                    # also remove this log later its pointless
                    log.debug("Skipping silence packet for unknown ssrc %s", ssrc)
                    return
                else:
                    log.info("Received packet for unknown ssrc %s:\n%s", ssrc, rtp_packet)

            self.speaking_timer.notify(ssrc)
            try:
                self.packet_router.feed_rtp(rtp_packet)
            except Exception as e:
                log.exception('Error processing rtp packet')
                self.error = e
                self.stop()


class PacketDecryptor:
    def __init__(self, mode: SupportedModes, secret_key: bytes) -> None:
        try:
            self.decrypt_rtp: DecryptRTP = getattr(self, '_decrypt_rtp_' + mode)
            self.decrypt_rtcp: DecryptRTCP = getattr(self, '_decrypt_rtcp_' + mode)
        except AttributeError as e:
            raise NotImplementedError(mode) from e

        self.box: nacl.secret.Aead = nacl.secret.Aead(bytes(secret_key))

    def update_secret_key(self, secret_key: bytes) -> None:
        self.box = nacl.secret.Aead(bytes(secret_key))

    def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self, packet: RTPPacket) -> bytes:
        nonce = bytearray(24)
        nonce[:4] = packet.data[-4:]
        voice_data = packet.data[:-4]

        result = self.box.decrypt(bytes(voice_data), bytes(packet.header), bytes(nonce))

        if packet.extended:
            # re-attach the extended header
            result = bytes(packet.header[-4:] + result)

            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_aead_xchacha20_poly1305_rtpsize(self, data: bytes) -> bytes:
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]
        result = self.box.decrypt(bytes(data[8:-4]), bytes(header), bytes(nonce))

        return bytes(header + result)


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
