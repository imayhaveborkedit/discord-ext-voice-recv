# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import select
import socket
import logging
import threading
import traceback

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
    from typing import Optional, Callable, Any
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

        self.active: bool = False
        self.router: PacketRouter = PacketRouter(sink)

    def set_sink(self, sink: AudioSink) -> AudioSink:
        """Sets the new sink for the reader and returns the old one.
        Does not call cleanup()
        """

        old_sink = self.sink
        old_sink._voice_client = None

        sink._voice_client = self.client
        self.sink = sink

        return old_sink

    def update_secret_box(self) -> None:
        self.box = nacl.secret.SecretBox(bytes(self.client.secret_key))

    def is_listening(self) -> bool:
        return self.active

    def start(self) -> None:
        if self.active:
            raise RuntimeError('Already started')

        self.client._connection.add_socket_listener(self.callback)
        self.active = True

    def stop(self) -> None:
        if not self.active:
            return

        self.client._connection.remove_socket_listener(self.callback)
        self.active = False

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
                packet = rtp.decode(packet_data)
                assert isinstance(packet, rtp.RTPPacket)

                packet.decrypted_data = self.decrypt_rtp(packet)
            else:
                rtcp = True
                packet = rtp.decode(self.decrypt_rtcp(packet_data))
                assert isinstance(packet, rtp.RTCPPacket)

                if not isinstance(packet, rtp.ReceiverReportPacket):
                    log.warning("Received unexpected rtcp packet type%s", f"\n{'*'*78}\n{packet}\n{'*'*78}")
        except CryptoError:
            msg = "CryptoError decoding data:\n  packet=%s\n  packet_data=%s"
            log.exception(msg, packet, packet_data)
            return
        except:
            log.exception("Error unpacking packet")
            traceback.print_exc()
        else:
            if not rtcp and packet.ssrc not in self.client._ssrc_to_id:
                if packet.is_silence():
                    log.debug("Skipping silence packet for unknown ssrc %s", packet.ssrc)
                    return
                else:
                    log.info("Received packet for unknown ssrc %s:\n%s", packet.ssrc, packet)
        finally:
            if not packet:
                return

        # I could combine these in a function in the router but this is faster
        if rtcp:
            self.router.feed_rtcp(packet)  # type: ignore
        else:
            self.client._speaking_cache[packet.ssrc] = time.time()
            self.router.feed_rtp(packet)  # type: ignore
