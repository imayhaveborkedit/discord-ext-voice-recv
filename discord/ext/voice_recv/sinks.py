# -*- coding: utf-8 -*-

from __future__ import annotations

import abc
import time
import wave
import audioop
import logging

from typing import TYPE_CHECKING

from .opus import Decoder
from .buffer import SimpleJitterBuffer

import discord

if TYPE_CHECKING:
    from .rtp import RTPPacket
    from .voice_client import VoiceRecvClient


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

class SinkExit(discord.DiscordException):
    """A signal type exception (like ``GeneratorExit``) to raise in a Sink's write() method to stop it.

    TODO: do i even keep this?

    Parameters
    -----------
    drain: :class:`bool`
        ...
    flush: :class:`bool`
        ...
    """

    def __init__(self, *, drain=True, flush=False):
        self.drain = drain
        self.flush = flush

class VoiceRecvException(discord.DiscordException):
    """Generic exception for voice recv related errors"""

    def __init__(self, message: str):
        self.message = message

class AudioSink(metaclass=abc.ABCMeta):
    _voice_client: VoiceRecvClient | None = None

    def __del__(self):
        self.cleanup()

    @property
    def voice_client(self) -> VoiceRecvClient:
        assert self._voice_client
        return self._voice_client

    @abc.abstractmethod
    def write(self, user: discord.User | discord.Member | None, packet: RTPPacket):
        """Callback for when the sink receives data"""
        raise NotImplementedError

    def write_rtcp(self, data):
        """Optional callback for when the sink receives an rtcp packet"""
        pass

    # TODO: handling opus vs pcm is not strictly mutually exclusive
    #       a sink could handle both but idk about that pattern
    @abc.abstractmethod
    def wants_opus(self) -> bool:
        """If sink handles opus data"""
        raise NotImplementedError

    @abc.abstractmethod
    def cleanup(self):
        raise NotImplementedError


class BasicSink(AudioSink):
    """Simple callback based sink."""

    def __init__(self, event, *, rtcp_event=None):
        self.cb = event
        self.cb_rtcp = rtcp_event

    def write(self, user, data):
        self.cb(user, data)

    def write_rtcp(self, data):
        self.cb_rtcp(data) if self.cb_rtcp else None

    def wants_opus(self):
        return True

    def cleanup(self):
        pass


#############################################################################
# OLD CODE BELOW
#############################################################################


# class JitterBufferSink(AudioSink):
#     def __init__(self, dest, **kwargs):
#         self.destination = dest
#         self._buffer = SimpleJitterBuffer(**kwargs)
# 
#     def wants_opus(self):
#         return True
# 
#     def write(self, packet):
#         items = self._buffer.push(packet)
# 
#         for item in items:
#             self.destination.write(item)
# 
#     def cleanup(self):
#         pass
# 
# class OpusDecoderSink(AudioSink):
#     def __init__(self, dest):
#         self.destination = dest
#         self._decoder = Decoder()
# 
#     def wants_opus(self):
#         return True
# 
#     def write(self, packet):
#         self.destination.write(self._decoder.decode(packet.decrypted_data, fec=False))
# 
#     def cleanup(self):
#         pass
# 
# class BundledOpusSink(AudioSink):
#     def __init__(self, dest, **kwargs):
#         self.destination = JitterBufferSink(OpusDecoderSink(dest), **kwargs)
# 
#     def on_voice_packet(self, packet):
#         self.destination.write(packet)
# 
# 
# ###############################################################################
# 
# 
# class WaveSink(AudioSink):
#     def __init__(self, destination):
#         self._file = wave.open(destination, 'wb')
#         self._file.setnchannels(Decoder.CHANNELS)
#         self._file.setsampwidth(Decoder.SAMPLE_SIZE//Decoder.CHANNELS)
#         self._file.setframerate(Decoder.SAMPLING_RATE)
# 
#     def write(self, data):
#         self._file.writeframes(data.data)
# 
#     def cleanup(self):
#         try:
#             self._file.close()
#         except:
#             pass
# 
# class PCMVolumeTransformerFilter(AudioSink):
#     def __init__(self, destination, volume=1.0):
#         if not isinstance(destination, AudioSink):
#             raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(destination))
# 
#         if destination.wants_opus:
#             raise VoiceRecvException('AudioSink must not request Opus encoding.')
# 
#         self.destination = destination
#         self.volume = volume
# 
#     @property
#     def volume(self):
#         """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
#         return self._volume
# 
#     @volume.setter
#     def volume(self, value):
#         self._volume = max(value, 0.0)
# 
#     def write(self, data):
#         data = audioop.mul(data.data, 2, min(self._volume, 2.0))
#         self.destination.write(None, data) # TODO: unfuck # type: ignore
# 
# # I need some sort of filter sink with a predicate or something
# # Which means I need to sort out the write() signature issue
# # Also need something to indicate a sink is "done", probably
# # something like raising an exception and handling that in the write loop
# # Maybe should rename some of these to Filter instead of Sink
# 
# class ConditionalFilter(AudioSink):
#     def __init__(self, destination, predicate):
#         self.destination = destination
#         self.predicate = predicate
# 
#     def write(self, data):
#         if self.predicate(data):
#             self.destination.write(data)
# 
# class TimedFilter(ConditionalFilter):
#     def __init__(self, destination, duration, *, start_on_init=False):
#         super().__init__(destination, self._predicate)
#         self.duration = duration
#         if start_on_init:
#             self.start_time = self.get_time()
#         else:
#             self.start_time = None
#             self.write = self._write_once
# 
#     def _write_once(self, data):
#         self.start_time = self.get_time()
#         super().write(data)
#         self.write = super().write
# 
#     def _predicate(self, data):
#         return self.start_time and self.get_time() - self.start_time < self.duration
# 
#     def get_time(self):
#         return time.time()
# 
# class UserFilter(ConditionalFilter):
#     def __init__(self, destination, user):
#         super().__init__(destination, self._predicate)
#         self.user = user
# 
#     def _predicate(self, data):
#         return data.user == self.user
