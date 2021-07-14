# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import time
import wave
import select
import socket
import audioop
import logging
import threading
import traceback

from .common import rtp
from .common.utils import Defaultdict
from .common.rtp import SilencePacket
from .common.opus import Decoder, BufferedDecoder
from discord.errors import DiscordException

try:
    import nacl.secret
    from nacl.exceptions import CryptoError
except ImportError:
    pass

log = logging.getLogger(__name__)

__all__ = [
    'AudioSink',
    'BasicSink',
    'AudioReader',
    # 'WaveSink',
    # 'PCMVolumeTransformerFilter',
    # 'ConditionalFilter',
    # 'TimedFilter',
    # 'UserFilter',
    # 'SinkExit',
]

class SinkExit(DiscordException):
    """A signal type exception (like ``GeneratorExit``) to raise in a Sink's write() method to stop it.

    TODO: make better words

    Parameters
    -----------
    drain: :class:`bool`
        ...
    flush: :class:`bool`
        ...
    """

    def __init__(self, *, drain=True, flush=False):
        self.kwargs = kwargs

class AudioSink:
    def __del__(self):
        self.cleanup()

    def write(self, data):
        raise NotImplementedError

    def wants_opus(self):
        return False

    def cleanup(self):
        pass

    # @staticmethod
    # def pack_data(data, user=None, packet=None):
    #     return VoiceData(data, user, packet) # is this even necessary?

class WaveSink(AudioSink):
    def __init__(self, destination):
        self._file = wave.open(destination, 'wb')
        self._file.setnchannels(Decoder.CHANNELS)
        self._file.setsampwidth(Decoder.SAMPLE_SIZE//Decoder.CHANNELS)
        self._file.setframerate(Decoder.SAMPLING_RATE)

    def write(self, data):
        self._file.writeframes(data.data)

    def cleanup(self):
        try:
            self._file.close()
        except:
            pass



class BasicSink(AudioSink):
    def __init__(self, event, *, rtcp_event=lambda _: None):
        self.on_voice_packet = event
        self.on_voice_rtcp_packet = rtcp_event



class PCMVolumeTransformerFilter(AudioSink):
    def __init__(self, destination, volume=1.0):
        if not isinstance(destination, AudioSink):
            raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(destination))

        if destination.wants_opus():
            raise ClientException('AudioSink must not request Opus encoding.')

        self.destination = destination
        self.volume = volume

    @property
    def volume(self):
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = max(value, 0.0)

    def write(self, data):
        data = audioop.mul(data.data, 2, min(self._volume, 2.0))
        self.destination.write(data)

# I need some sort of filter sink with a predicate or something
# Which means I need to sort out the write() signature issue
# Also need something to indicate a sink is "done", probably
# something like raising an exception and handling that in the write loop
# Maybe should rename some of these to Filter instead of Sink

class ConditionalFilter(AudioSink):
    def __init__(self, destination, predicate):
        self.destination = destination
        self.predicate = predicate

    def write(self, data):
        if self.predicate(data):
            self.destination.write(data)

class TimedFilter(ConditionalFilter):
    def __init__(self, destination, duration, *, start_on_init=False):
        super().__init__(destination, self._predicate)
        self.duration = duration
        if start_on_init:
            self.start_time = self.get_time()
        else:
            self.start_time = None
            self.write = self._write_once

    def _write_once(self, data):
        self.start_time = self.get_time()
        super().write(data)
        self.write = super().write

    def _predicate(self, data):
        return self.start_time and self.get_time() - self.start_time < self.duration

    def get_time(self):
        return time.time()

class UserFilter(ConditionalFilter):
    def __init__(self, destination, user):
        super().__init__(destination, self._predicate)
        self.user = user

    def _predicate(self, data):
        return data.user == self.user

# rename 'data' to 'payload'? or 'opus'? something else?
class VoiceData:
    __slots__ = ('data', 'user', 'packet')

    def __init__(self, data, user, packet):
        self.data = data
        self.user = user
        self.packet = packet

class _ReaderBase(threading.Thread):
    def __init__(self, client, **kwargs):
        daemon = kwargs.pop('daemon', True)
        super().__init__(daemon=daemon, **kwargs)

        self.client = client
        self.box = nacl.secret.SecretBox(bytes(client.secret_key))
        self.decrypt_rtp = getattr(self, '_decrypt_rtp_' + client.mode)
        self.decrypt_rtcp = getattr(self, '_decrypt_rtcp_' + client.mode)

    def _decrypt_rtp_xsalsa20_poly1305(self, packet):
        nonce = bytearray(24)
        nonce[:12] = packet.header
        result = self.box.decrypt(bytes(packet.data), bytes(nonce))

        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_xsalsa20_poly1305(self, data):
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

    def _decrypt_rtcp_xsalsa20_poly1305_suffix(self, data):
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

    def _decrypt_rtcp_xsalsa20_poly1305_lite(self, data):
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]
        result = self.box.decrypt(data[8:-4], bytes(nonce))

        return header + result

    def run(self):
        raise NotImplementedError


class PCMEventAudioReader(_ReaderBase):
    def __init__(self, sink, client, *, after=None):
        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

        super().__init__(client)

        self.sink = sink
        self.client = client
        self.after = after

        self._current_error = None
        self._end = threading.Event()
        self._noop = lambda *_: None

    @property
    def connected(self):
        return self.client._connected

    def dispatch(self, event, *args):
        event = getattr(self.sink, 'on_'+event, self._noop)
        event(*args)

    def _get_user(self, packet):
        _, user_id = self.client._get_ssrc_mapping(ssrc=packet.ssrc)
        # may need to change this for calls or something
        return self.client.guild.get_member(user_id)

    def _do_run(self):
        while not self._end.is_set():
            if not self.connected.is_set():
                self.connected.wait()

            ready, _, err = select.select([self.client.socket], [],
                                          [self.client.socket], 0.01)
            if not ready:
                if err:
                    print("Socket error")
                continue

            try:
                raw_data = self.client.socket.recv(4096)
            except socket.error as e:
                t0 = time.time()

                if e.errno == 10038: # ENOTSOCK
                    continue

                log.exception("Socket error in reader thread ")
                print(f"Socket error in reader thread: {e} {t0}")

                with self.client._connecting:
                    timed_out = self.client._connecting.wait(20)

                if not timed_out:
                    raise
                elif self.client.is_connected():
                    print(f"Reconnected in {time.time()-t0:.4f}s")
                    continue
                else:
                    raise

            try:
                packet = None
                if not rtp.is_rtcp(raw_data):
                    packet = rtp.decode(raw_data)
                    packet.decrypted_data = self.decrypt_rtp(packet)
                else:
                    packet = rtp.decode(self.decrypt_rtcp(raw_data))
                    if not isinstance(packet, rtp.ReceiverReportPacket):
                        print('Received unusual rtcp packet')
                        print('*'*64)
                        print(packet)
                        print('*'*64)

                        # TODO: Fabricate and send SenderReports and see what happens

                    self.dispatch('voice_rtcp_packet', packet)
                    continue

            except CryptoError:
                log.exception("CryptoError decoding packet %s", packet)
                continue

            except:
                log.exception("Error unpacking packet")
                traceback.print_exc()

            else:
                if packet.ssrc not in self.client._ssrc_to_id:
                    log.debug("Received packet for unknown ssrc %s", packet.ssrc)

                self.dispatch('voice_packet', self._get_user(packet), packet)

    def is_listening(self):
        return not self._end.is_set()

    def stop(self):
        self._end.set()

    def run(self):
        try:
            self._do_run()
        except socket.error as exc:
            self._current_error = exc
            self.stop()
        except Exception as exc:
            traceback.print_exc()
            self._current_error = exc
            self.stop()
        finally:
            self._call_after()

    def _call_after(self):
         if self.after is not None:
            try:
                self.after(self._current_error)
            except Exception:
                log.exception('Calling the after function failed.')


#class AudioReader(_ReaderBase):
#    def __init__(self, sink, client, *, after=None):
#        if after is not None and not callable(after):
#            raise TypeError('Expected a callable for the "after" parameter.')
#
#        super().__init__()
#
#        self.sink = sink
#        self.client = client
#        self.after = after
#
#        self._current_error = None
#        self._end = threading.Event()
#        self._decoder_lock = threading.Lock()
#
#        self.decoder = BufferedDecoder(self)
#        self.decoder.start()
#
#        # TODO: inject sink functions(?)
#
#    @property
#    def connected(self):
#        return self.client._connected
#
#    def _reset_decoders(self, *ssrcs):
#        self.decoder.reset(*ssrcs)
#
#    def _stop_decoders(self, **kwargs):
#        self.decoder.stop(**kwargs)
#
#    def _ssrc_removed(self, ssrc):
#        # An user has disconnected but there still may be
#        # packets from them left in the buffer to read
#        # For now we're just going to kill the decoder and see how that works out
#        # I *think* this is the correct way to do this
#        # Depending on how many leftovers I end up with I may reconsider
#
#        self.decoder.drop_ssrc(ssrc) # flush=True?
#
#    def _get_user(self, packet):
#        _, user_id = self.client._get_ssrc_mapping(ssrc=packet.ssrc)
#        # may need to change this for calls or something
#        return self.client.guild.get_member(user_id)
#
#    def _write_to_sink(self, pcm, opus, packet):
#        try:
#            data = opus if self.sink.wants_opus() else pcm
#            user = self._get_user(packet)
#            self.sink.write(VoiceData(data, user, packet))
#            # TODO: remove weird error handling in favor of injected functions
#        except SinkExit as e:
#            log.info("Shutting down reader thread %s", self)
#            self.stop()
#            self._stop_decoders(**e.kwargs)
#        except:
#            traceback.print_exc()
#            # insert optional error handling here
#
#    def _set_sink(self, sink):
#        with self._decoder_lock:
#            self.sink = sink
#        # if i were to fire a sink change mini-event it would be here
#
#    def _do_run(self):
#        while not self._end.is_set():
#            if not self.connected.is_set():
#                self.connected.wait()
#
#            ready, _, err = select.select([self.client.socket], [],
#                                          [self.client.socket], 0.01)
#            if not ready:
#                if err:
#                    print("Socket error")
#                continue
#
#            try:
#                raw_data = self.client.socket.recv(4096)
#            except socket.error as e:
#                t0 = time.time()
#
#                if e.errno == 10038: # ENOTSOCK
#                    continue
#
#                log.exception("Socket error in reader thread ")
#                print(f"Socket error in reader thread: {e} {t0}")
#
#                with self.client._connecting:
#                    timed_out = self.client._connecting.wait(20)
#
#                if not timed_out:
#                    raise
#                elif self.client.is_connected():
#                    print(f"Reconnected in {time.time()-t0:.4f}s")
#                    continue
#                else:
#                    raise
#
#            try:
#                packet = None
#                if not rtp.is_rtcp(raw_data):
#                    packet = rtp.decode(raw_data)
#                    packet.decrypted_data = self.decrypt_rtp(packet)
#                else:
#                    packet = rtp.decode(self.decrypt_rtcp(raw_data))
#                    if not isinstance(packet, rtp.ReceiverReportPacket):
#                        print(packet)
#
#                        # TODO: Fabricate and send SenderReports and see what happens
#
#                    self.decoder.feed_rtcp(packet)
#                    continue
#
#            except CryptoError:
#                log.exception("CryptoError decoding packet %s", packet)
#                continue
#
#            except:
#                log.exception("Error unpacking packet")
#                traceback.print_exc()
#
#            else:
#                if packet.ssrc not in self.client._ssrcs:
#                    log.debug("Received packet for unknown ssrc %s", packet.ssrc)
#
#                self.decoder.feed_rtp(packet)
#
#    def is_listening(self):
#        return not self._end.is_set()
#
#    def stop(self):
#        self._end.set()
#
#    def run(self):
#        try:
#            self._do_run()
#        except socket.error as exc:
#            self._current_error = exc
#            self.stop()
#        except Exception as exc:
#            traceback.print_exc()
#            self._current_error = exc
#            self.stop()
#        finally:
#            self._stop_decoders()
#            try:
#                self.sink.cleanup()
#            except:
#                log.exception("Error during sink cleanup")
#                # Testing only
#                traceback.print_exc()
#
#            self._call_after()
#
#    def _call_after(self):
#         if self.after is not None:
#            try:
#                self.after(self._current_error)
#            except Exception:
#                log.exception('Calling the after function failed.')


AudioReader = PCMEventAudioReader
