# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import logging
import threading

from operator import itemgetter
from typing import TYPE_CHECKING

from . import rtp
from .sinks import AudioSink
from .opus import PacketRouter

try:
    import nacl.secret
    from nacl.exceptions import CryptoError
except ImportError as e:
    raise RuntimeError("pynacl is required") from e

if TYPE_CHECKING:
    from typing import Optional, Callable, Any, Dict
    from discord import Member
    from .voice_client import VoiceRecvClient
    from .rtp import RTPPacket, RTCPPacket, RealPacket

    DecryptRTP = Callable[[RTPPacket], bytes]
    DecryptRTCP = Callable[[bytes], bytes]
    AfterCB = Callable[[Optional[Exception]], Any]

log = logging.getLogger(__name__)

__all__ = [
    'AudioReader',
]


class AudioReader:
    def __init__(self, sink: AudioSink, client: VoiceRecvClient, *, after: Optional[AfterCB] = None):
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        self.sink: AudioSink = sink
        self.client: VoiceRecvClient = client
        self.after: Optional[AfterCB] = after

        # No need for the whole set_sink() call
        self.sink._voice_client = client

        self.box: nacl.secret.SecretBox = nacl.secret.SecretBox(bytes(client.secret_key))
        self.decrypt_rtp: DecryptRTP = getattr(self, '_decrypt_rtp_' + client.mode)
        self.decrypt_rtcp: DecryptRTCP = getattr(self, '_decrypt_rtcp_' + client.mode)

        self.router: PacketRouter = PacketRouter(sink)
        self.active: bool = False
        self.error: Optional[Exception] = None

        self.speaking_timer: threading.Thread = threading.Thread(
            target=self._speaking_timer_loop, daemon=True, name=f'speaking-timer-{id(self):x}'
        )
        self.speaking_timer_event: threading.Event = threading.Event()
        self.speaking_timeout_delay: float = 0.2
        self.last_speaking_state: Dict[int, bool] = {}

    def set_sink(self, sink: AudioSink) -> AudioSink:
        """Sets the new sink for the reader and returns the old one.
        Does not call cleanup()
        """

        # This whole function is potentially very racy

        old_sink = self.sink
        old_sink._voice_client = None

        sink._voice_client = self.client
        self.router.set_sink(sink)
        self.sink = sink

        return old_sink

    def update_secret_box(self) -> None:
        self.box = nacl.secret.SecretBox(bytes(self.client.secret_key))

    def is_listening(self) -> bool:
        return self.active

    def start(self) -> None:
        if self.active:
            raise RuntimeError('Already started')

        self.speaking_timer.start()
        self.client._connection.add_socket_listener(self.callback)
        self.active = True

    def stop(self) -> None:
        if not self.active:
            log.info('Tried to stop an inactive reader')
            return

        self.client._connection.remove_socket_listener(self.callback)
        self.active = False
        self._notify_timer()

        try:
            self.router.stop()
        except Exception as e:
            self.error = e
            log.exception('Error stopping packet router')

        if self.after:
            try:
                self.after(self.error)
            except Exception:
                log.exception('Error calling listener after function')

        # TODO: cleanup in reverse?
        for sink in self.sink.root.walk_children(with_self=True):
            try:
                sink.cleanup()
            except Exception:
                log.exception('Error calling cleanup() for %s', sink)

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

    def callback(self, packet_data: bytes) -> None:
        packet = None
        rtcp = False
        try:
            if not rtp.is_rtcp(packet_data):
                packet = rtp.decode_rtp(packet_data)
                packet.decrypted_data = self.decrypt_rtp(packet)
            else:
                rtcp = True
                packet = rtp.decode_rtcp(self.decrypt_rtcp(packet_data))

                if not isinstance(packet, rtp.ReceiverReportPacket):
                    log.warning("Received unexpected rtcp packet type%s", f"\n{'*'*78}\n{packet}\n{'*'*78}")
        except CryptoError as e:
            self.error = e
            msg = "CryptoError decoding data:\n  packet=%s\n  packet_data=%s"
            log.exception(msg, packet, packet_data)
            return
        except Exception as e:
            self.error = e
            log.exception("Error unpacking packet")
        finally:
            if self.error:
                self.stop()
                return
            if not packet:
                return

        if rtcp:
            self.router.feed_rtcp(packet)  # type: ignore
        else:
            _packet: RTPPacket = packet  # type: ignore  # dumb typing hack

            if _packet.ssrc not in self.client._ssrc_to_id:
                if _packet.is_silence():
                    # TODO: make a list of ssrcs so this only gets logged once?
                    log.debug("Skipping silence packet for unknown ssrc %s", _packet.ssrc)
                    return
                else:
                    log.info("Received packet for unknown ssrc %s:\n%s", _packet.ssrc, _packet)

            self.maybe_dispatch_speaking_start(_packet.ssrc)
            self.client._speaking_cache[_packet.ssrc] = time.perf_counter()
            self.last_speaking_state[_packet.ssrc] = True
            self._notify_timer()
            try:
                self.router.feed_rtp(_packet)
            except Exception as e:
                log.exception('Error processing rtp packet')
                self.error = e
                self.stop()

    def _notify_timer(self) -> None:
        self.speaking_timer_event.set()
        self.speaking_timer_event.clear()

    def _speaking_timer_loop(self) -> None:
        _i1 = itemgetter(1)

        def get_next_entry():
            cache = sorted(self.client._speaking_cache.items(), key=_i1)
            for ssrc, tlast in cache:
                # only return pair if speaking
                if self.last_speaking_state.get(ssrc):
                    return ssrc, tlast

            return None, None

        self.speaking_timer_event.wait()
        while self.active:
            if not self.client._speaking_cache:
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

            self.dispatch_speaking_stop(ssrc)
            self.last_speaking_state[ssrc] = False

    def maybe_dispatch_speaking_start(self, ssrc: int) -> None:
        tlast = self.client._speaking_cache.get(ssrc)
        if tlast is None or tlast + self.speaking_timeout_delay < time.perf_counter():
            self.dispatch_speaking_start(ssrc)

    def dispatch_speaking_start(self, ssrc: int) -> None:
        who = self._lookup_member(ssrc)
        if not who:
            log.warning("Unknown ssrc %s", ssrc)
            return

        self.router.dispatch('voice_member_speaking_start', who)

    def dispatch_speaking_stop(self, ssrc: int) -> None:
        who = self._lookup_member(ssrc)
        if not who:
            log.warning("Unknown ssrc %s", ssrc)
            return

        self.router.dispatch('voice_member_speaking_stop', who)

    def _lookup_member(self, ssrc: int) -> Optional[Member]:
        whoid = self.client._get_id_from_ssrc(ssrc)
        if not whoid:
            return
        return self.client.guild.get_member(whoid)
