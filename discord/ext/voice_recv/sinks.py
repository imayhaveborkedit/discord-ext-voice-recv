# -*- coding: utf-8 -*-

import time
import wave
import audioop
import logging

from .opus import Decoder
from .buffer import SimpleJitterBuffer

from discord.errors import DiscordException, ClientException

log = logging.getLogger(__name__)

__all__ = [
    'AudioSink',
    'BasicSink',
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
        # self.kwargs = kwargs
        self.drain = drain
        self.flush = flush

class AudioSink:
    def __del__(self):
        self.cleanup()

    def write(self, data):
        raise NotImplementedError

    def wants_opus(self):
        return False

    def cleanup(self):
        pass


class BasicSink(AudioSink):
    """Simple callback based sink."""

    def __init__(self, event, *, rtcp_event=lambda _: None):
        self.on_voice_packet = event
        self.on_voice_rtcp_packet = rtcp_event

class JitterBufferSink(AudioSink):
    def __init__(self, dest, **kwargs):
        self.destination = dest
        self._buffer = SimpleJitterBuffer(**kwargs)

    def wants_opus(self):
        return True

    def write(self, packet):
        items = self._buffer.push(packet)

        for item in items:
            self.destination.write(item)

class OpusDecoderSink(AudioSink):
    def __init__(self, dest):
        self.destination = dest
        self._decoder = Decoder()

    def wants_opus(self):
        return True

    def write(self, packet):
        self.destination.write(self._decoder.decode(packet.decrypted_data, fec=False))

class BundledOpusSink(AudioSink):
    def __init__(self, dest, **kwargs):
        self.destination = JitterBufferSink(OpusDecoderSink(dest), **kwargs)

    def on_voice_packet(self, packet):
        self.destination.write(packet)


###############################################################################


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
