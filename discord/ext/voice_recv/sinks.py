# -*- coding: utf-8 -*-

from __future__ import annotations

import abc
import time
import wave
import audioop
import logging

from typing import TYPE_CHECKING

from .opus import VoiceData

import discord

from discord.opus import Decoder as OpusDecoder

if TYPE_CHECKING:
    from typing import Callable, Optional, Any, IO, Sequence

    from .rtp import RTPPacket, RTCPPacket, FakePacket
    from .voice_client import VoiceRecvClient
    from .opus import VoiceData

    Packet = RTPPacket | FakePacket
    User = discord.User | discord.Member

    BasicSinkWriteCB = Callable[[Optional[User], VoiceData], Any]
    BasicSinkWriteRTCPCB = Callable[[RTCPPacket], Any]
    ConditionalFilterFn = Callable[[Optional[User], VoiceData], bool]


log = logging.getLogger(__name__)

__all__ = [
    'AudioSink',
    'MultiAudioSink',
    'BasicSink',
    'WaveSink',
    'PCMVolumeTransformer',
    'ConditionalFilter',
    'TimedFilter',
    'UserFilter',
]

# TODO: use this in more places
class VoiceRecvException(discord.DiscordException):
    """Generic exception for voice recv related errors"""

    def __init__(self, message: str):
        self.message = message


class BaseSink(metaclass=abc.ABCMeta):
    _voice_client: Optional[VoiceRecvClient] = None
    _parent: Optional[AudioSink] = None

    @property
    @abc.abstractmethod
    def parent(self) -> Optional[AudioSink]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def child(self) -> Optional[AudioSink]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def children(self) -> Sequence[AudioSink]:
        raise NotImplementedError

    # TODO: handling opus vs pcm is not strictly mutually exclusive
    #       a sink could handle both but idk about that pattern
    @abc.abstractmethod
    def wants_opus(self) -> bool:
        """If sink handles opus data"""
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, user: Optional[User], data: VoiceData):
        """Callback for when the sink receives data"""
        raise NotImplementedError

    def write_rtcp(self, packet: RTCPPacket):
        """Optional callback for when the sink receives an rtcp packet"""
        pass

    @abc.abstractmethod
    def cleanup(self):
        raise NotImplementedError


class AudioSink(BaseSink):
    def __init__(self, destination: Optional[AudioSink]=None, /):
        self._child = destination

        if destination is not None:
            destination._parent = self

    def __del__(self):
        self.cleanup()

    @property
    def voice_client(self) -> VoiceRecvClient | None:
        """
        Guaranteed to not be None inside write()
        """
        if self.parent is not None:
            return self.parent.voice_client
        else:
            return self._voice_client

    @property
    def parent(self) -> Optional[AudioSink]:
        return self._parent

    @property
    def child(self) -> Optional[AudioSink]:
        return self._child

    @property
    def children(self) -> Sequence[AudioSink]:
        return [self._child] if self._child else []


class MultiAudioSink(AudioSink):
    def __init__(self, destinations: Sequence[AudioSink], /):
        # Intentionally not calling super().__init__ here
        self._children = tuple(destinations)

        if destinations is not None:
            for dest in destinations:
                dest._parent = self

    @property
    def child(self) -> Optional[AudioSink]:
        return self._children[0] if self._children else None

    @property
    def children(self) -> Sequence[AudioSink]:
        return self._children


class BasicSink(AudioSink):
    """Simple callback based sink."""

    def __init__(self,
        event: BasicSinkWriteCB,
        *,
        rtcp_event: Optional[BasicSinkWriteRTCPCB]=None,
        decode: bool=True
    ):
        super().__init__()

        self.cb = event
        self.cb_rtcp = rtcp_event
        self.decode = decode

    def wants_opus(self) -> bool:
        return not self.decode

    def write(self, user: Optional[User], data: VoiceData):
        self.cb(user, data)

    def write_rtcp(self, data: RTCPPacket):
        self.cb_rtcp(data) if self.cb_rtcp else None

    def cleanup(self):
        pass


class WaveSink(AudioSink):
    """Endpoint AudioSink that generates a wav file.
    Best used in conjunction with a silence generating sink. (TBD)
    """

    CHANNELS = OpusDecoder.CHANNELS
    SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE//OpusDecoder.CHANNELS
    SAMPLING_RATE = OpusDecoder.SAMPLING_RATE

    def __init__(self, destination: str | IO[bytes]):
        super().__init__()

        self._file = wave.open(destination, 'wb')
        self._file.setnchannels(self.CHANNELS)
        self._file.setsampwidth(self.SAMPLE_WIDTH)
        self._file.setframerate(self.SAMPLING_RATE)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[User], data: VoiceData):
        self._file.writeframes(data.pcm)

    def cleanup(self):
        try:
            self._file.close()
        except Exception:
            log.info("WaveSink got error closing file on cleanup", exc_info=True)


class PCMVolumeTransformer(AudioSink):
    """AudioSink used to change the volume of PCM data, just like
    :class:`discord.PCMVolumeTransformer`.
    """

    def __init__(self, destination: AudioSink, volume: float=1.0):
        if not isinstance(destination, AudioSink):
            raise TypeError(f'expected AudioSink not {type(destination).__name__}')

        if destination.wants_opus():
            raise VoiceRecvException('AudioSink must not request Opus encoding.')

        super().__init__(destination)

        self.destination = destination
        self.volume = volume

    def wants_opus(self) -> bool:
        return False

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value: float): # TODO: type range
        self._volume = max(value, 0.0)

    def write(self, user: Optional[User], data: VoiceData):
        data.pcm = audioop.mul(data.pcm, 2, min(self._volume, 2.0))
        self.destination.write(user, data)

    def write_rtcp(self, packet: RTCPPacket):
        self.destination.write_rtcp(packet)

    def cleanup(self):
        pass


class ConditionalFilter(AudioSink):
    """AudioSink for filtering packets based on an arbitrary predicate function."""

    def __init__(self, destination: AudioSink, predicate: ConditionalFilterFn):
        super().__init__(destination)

        self.destination = destination
        self.predicate = predicate

    def wants_opus(self) -> bool:
        return self.destination.wants_opus()

    def write(self, user: Optional[User], data: VoiceData):
        if self.predicate(user, data):
            self.destination.write(user, data)

    def write_rtcp(self, packet: RTCPPacket):
        self.destination.write_rtcp(packet)

    def cleanup(self):
        del self.predicate


class UserFilter(ConditionalFilter):
    """A convenience class for a User based ConditionalFilter."""

    def __init__(self, destination: AudioSink, user: User):
        super().__init__(destination, self._predicate)
        self.user = user

    def _predicate(self, user: Optional[User], data: VoiceData) -> bool:
        return user == self.user


class TimedFilter(ConditionalFilter):
    def __init__(self,
        destination: AudioSink,
        duration: int | float,
        *,
        start_on_init: bool=False
    ):
        super().__init__(destination, self.predicate)
        self.duration = duration

        if start_on_init:
            self.start_time = self.get_time()
        else:
            self.start_time = None
            self.write = self._write_once

    def _write_once(self, user: Optional[User], data: VoiceData):
        self.start_time = self.get_time()
        super().write(user, data)
        self.write = super().write

    def predicate(self, user: Optional[User], data: VoiceData) -> bool:
        return self.start_time is not None and self.get_time() - self.start_time < self.duration

    def get_time(self) -> int | float:
        """Function to generate a timestamp.  Defaults to `time.perf_counter()`.
        Can be overridden.
        """
        return time.perf_counter()
