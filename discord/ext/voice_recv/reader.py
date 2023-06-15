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
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Optional, Callable, Any
    from .voice_client import VoiceRecvClient
    from .rtp import RTPPacket, RTCPPacket

    DecryptRTP = Callable[[RTPPacket], bytes]
    DecryptRTCP = Callable[[bytes], bytes]
    AfterCB = Callable[[Optional[Exception]], Any]

log = logging.getLogger(__name__)

__all__ = [
    'AudioReader'
]


class _ReaderBase(threading.Thread):
    def __init__(self, sink: AudioSink, client: VoiceRecvClient, **kwargs):
        daemon = kwargs.pop('daemon', True)
        super().__init__(daemon=daemon, **kwargs)

        self.sink = sink
        self.client = client

        self.sink._voice_client = client

        self.box = nacl.secret.SecretBox(bytes(client.secret_key))
        self.decrypt_rtp: DecryptRTP = getattr(self, '_decrypt_rtp_' + client.mode)
        self.decrypt_rtcp: DecryptRTCP = getattr(self, '_decrypt_rtcp_' + client.mode)

    def run(self):
        raise NotImplementedError

    def set_sink(self, sink: AudioSink) -> AudioSink:
        """Sets the new sink for the reader and returns the old one.
        Does not call cleanup()
        """

        old_sink = self.sink
        old_sink._voice_client = None

        sink._voice_client = self.client
        self.sink = sink

        return old_sink

    def update_secret_box(self):
        # Sure hope this isn't hilariously threadunsafe
        # if so this might not be the way i need to do this
        self.box = nacl.secret.SecretBox(bytes(self.client.secret_key))

    def _decrypt_rtp_xsalsa20_poly1305(self, packet):
        nonce = bytearray(24)
        nonce[:12] = packet.header
        result = self.box.decrypt(bytes(packet.data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305(self, data: bytes):
        nonce = bytearray(24)
        nonce[:8] = data[:8]
        result = self.box.decrypt(data[8:], bytes(nonce))

        return data[:8] + result

    def _decrypt_rtp_xsalsa20_poly1305_suffix(self, packet):
        nonce = packet.data[-24:]
        voice_data = packet.data[:-24]
        result = self.box.decrypt(bytes(voice_data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305_suffix(self, data: bytes):
        nonce = data[-24:]
        header = data[:8]
        result = self.box.decrypt(data[8:-24], nonce)

        return header + result

    def _decrypt_rtp_xsalsa20_poly1305_lite(self, packet):
        nonce = bytearray(24)
        nonce[:4] = packet.data[-4:]
        voice_data = packet.data[:-4]
        result = self.box.decrypt(bytes(voice_data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305_lite(self, data: bytes):
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]
        result = self.box.decrypt(data[8:-4], bytes(nonce))

        return header + result



class AudioReader(_ReaderBase):
    def __init__(self,
        sink: AudioSink,
        client: VoiceRecvClient,
        *,
        after: Optional[AfterCB]=None
    ):
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        super().__init__(sink, client)

        self.after = after
        self.router = PacketRouter(sink)

        self._current_error: Optional[Exception] = None
        self._end = threading.Event()

    @property
    def connected(self):
        return self.client._connected

    def set_sink(self, sink: AudioSink) -> AudioSink:
        # Definitely not threadsafe but idk if it matters yet
        sink = super().set_sink(sink)
        self.router.set_sink(sink)
        return sink

    def _do_run(self):
        while not self._end.is_set():
            if not self.connected.is_set():
                self.connected.wait()

            ready, _, err = select.select([self.client.socket], [],
                                          [self.client.socket], 0.01)
            if not ready:
                if err:
                    log.warning("Socket error in %s", self)
                continue

            try:
                raw_data = self.client.socket.recv(4096)
            except socket.error as e:
                t0 = time.time()

                if e.errno == 10038: # ENOTSOCK
                    continue

                if e.errno == 9: # Bad file descriptor
                    self.stop()
                    return

                log.exception("Socket error in reader thread %s", self)

                with self.client._connecting:
                    log.info("Waiting for client connection")
                    timed_out = self.client._connecting.wait(20)

                if not timed_out:
                    raise
                elif self.client.is_connected():
                    log.info("Reconnected in %.4fs", time.time()-t0)
                    continue
                else:
                    raise

            packet = None
            rtcp = False
            try:
                if not rtp.is_rtcp(raw_data):
                    packet = rtp.decode(raw_data)
                    assert isinstance(packet, rtp.RTPPacket)

                    packet.decrypted_data = self.decrypt_rtp(packet)
                else:
                    rtcp = True
                    packet = rtp.decode(self.decrypt_rtcp(raw_data))
                    assert isinstance(packet, rtp.RTCPPacket)

                    if not isinstance(packet, rtp.ReceiverReportPacket):
                        log.warning(
                            "Received unusual rtcp packet%s",
                            f"\n{'*'*78}\n{packet}\n{'*'*78}"
                        )
            except CryptoError:
                msg = "CryptoError decoding data:\n  packet=%s\n  raw_data=%s"
                log.exception(msg, packet, raw_data)
                continue

            except:
                log.exception("Error unpacking packet")
                traceback.print_exc()

            else:
                if not rtcp and packet.ssrc not in self.client._ssrc_to_id:
                    if packet.is_silence():
                        log.debug("Skipping silence packet for unknown ssrc %s", packet.ssrc)
                        continue
                    else:
                        log.info("Received packet for unknown ssrc %s:\n%s", packet.ssrc, packet)

            finally:
                if not packet:
                    continue

            # I could combine these in a function in the router but this is faster
            if rtcp:
                self.router.feed_rtcp(packet) # type: ignore
            else:
                self.router.feed_rtp(packet) # type: ignore

    def is_listening(self):
        return not self._end.is_set()

    def stop(self):
        self._end.set()

    def run(self):
        try:
            self._do_run()
        except socket.error as err:
            self._current_error = err

        except Exception as err:
            log.exception("Error in %s", self)
            self._current_error = err

        finally:
            self.stop()

            try:
                self.router.stop()
            except Exception:
                log.exception("Error stopping router in %s", self)

            self._call_after()

            try:
                self.sink.cleanup()
            except Exception:
                log.exception("Error calling sink cleanup() in %s", self)

    def _call_after(self):
         if self.after is not None:
            try:
                self.after(self._current_error)
            except Exception:
                log.exception('Calling the after function failed.')
