# -*- coding: utf-8 -*-

from __future__ import annotations

import io
import abc
import time
import wave
import shlex
import inspect
import audioop
import logging
import threading
import subprocess

from .opus import VoiceData
from .silence import SilenceGenerator

import discord

from discord.utils import MISSING, SequenceProxy
from discord.opus import Decoder as OpusDecoder

from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from typing import Callable, Optional, Any, IO, Sequence, Tuple, Generator, Union, Dict, List

    from .rtp import AudioPacket, RTCPPacket
    from .voice_client import VoiceRecvClient
    from .opus import VoiceData
    from .types import MemberOrUser as User

    BasicSinkWriteCB = Callable[[Optional[User], VoiceData], Any]
    BasicSinkWriteRTCPCB = Callable[[RTCPPacket], Any]
    ConditionalFilterFn = Callable[[Optional[User], VoiceData], bool]
    FFmpegErrorCB = Callable[['FFmpegSink', Exception, Optional[VoiceData]], Any]


log = logging.getLogger(__name__)

__all__ = [
    'AudioSink',
    'MultiAudioSink',
    'BasicSink',
    'WaveSink',
    'FFmpegSink',
    'PCMVolumeTransformer',
    'ConditionalFilter',
    'TimedFilter',
    'UserFilter',
    'SilenceGeneratorSink',
]


# TODO: use this in more places
class VoiceRecvException(discord.DiscordException):
    """Generic exception for voice recv related errors"""

    def __init__(self, message: str):
        self.message: str = message


class SinkMeta(abc.ABCMeta):
    __sink_listeners__: List[Tuple[str, str]]

    def __new__(cls, name: str, bases: Tuple[type, ...], attrs: Dict[str, Any], **kwargs):
        listeners: Dict[str, Any] = {}
        new_cls = super().__new__(cls, name, bases, attrs, **kwargs)

        for base in reversed(new_cls.__mro__):
            for elem, value in base.__dict__.items():
                # If it exists in a subclass, delete the higher level one
                if elem in listeners:
                    del listeners[elem]

                is_static_method = isinstance(value, staticmethod)
                if is_static_method:
                    value = value.__func__

                if not hasattr(value, '__sink_listener__'):
                    continue

                listeners[elem] = value

        listener_list = []
        for listener in listeners.values():
            for listener_name in listener.__sink_listener_names__:
                listener_list.append((listener_name, listener.__name__))

        new_cls.__sink_listeners__ = listener_list
        return new_cls


class SinkABC(metaclass=SinkMeta):
    __sink_listeners__: List[Tuple[str, str]]

    @property
    @abc.abstractmethod
    def root(self) -> AudioSink:
        raise NotImplementedError

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

    @property
    @abc.abstractmethod
    def voice_client(self) -> Optional[VoiceRecvClient]:
        raise NotImplementedError

    # handling opus vs pcm is not strictly mutually exclusive
    # a sink could handle both but idk about that pattern
    @abc.abstractmethod
    def wants_opus(self) -> bool:
        """If sink handles opus data"""
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, user: Optional[User], data: VoiceData):
        """Callback for when the sink receives data"""
        raise NotImplementedError

    @abc.abstractmethod
    def cleanup(self):
        raise NotImplementedError

    @abc.abstractmethod
    def _register_child(self, child: AudioSink) -> None:
        raise NotImplementedError


class AudioSink(SinkABC):
    _voice_client: Optional[VoiceRecvClient]
    _parent: Optional[AudioSink] = None
    _child: Optional[AudioSink] = None

    def __init__(self, destination: Optional[AudioSink] = None, /):
        if destination is not None:
            self._register_child(destination)
        else:
            self._child = None

    def __del__(self):
        self.cleanup()

    def _register_child(self, child: AudioSink) -> None:
        if child in self.root.walk_children():
            raise RuntimeError('Sink is already registered.')

        self._child = child
        child._parent = self

    @property
    def root(self) -> AudioSink:
        if self.parent is None:
            return self

        return self.parent.root

    @property
    def parent(self) -> Optional[AudioSink]:
        return self._parent

    @property
    def child(self) -> Optional[AudioSink]:
        return self._child

    @property
    def children(self) -> Sequence[AudioSink]:
        return [self._child] if self._child else []

    @property
    def voice_client(self) -> Optional[VoiceRecvClient]:
        """Guaranteed to not be None inside write()"""

        if self.parent is not None:
            return self.parent.voice_client
        else:
            return self._voice_client

    @property
    def client(self) -> Optional[discord.Client]:
        """Guaranteed to not be None inside write()"""
        return self.voice_client and self.voice_client.client

    def walk_children(self, *, with_self: bool = False) -> Generator[AudioSink, None, None]:
        """Returns a generator of all the children of this sink, recursively, depth first."""

        if with_self:
            yield self

        for child in self.children:
            yield child
            yield from child.walk_children()

    @classmethod
    def listener(cls, name: str = MISSING):
        """Marks a function as an event listener."""

        if name is not MISSING and not isinstance(name, str):
            raise TypeError(f'AudioSink.listener expected str but received {type(name).__name__} instead.')

        def decorator(func):
            actual = func

            if isinstance(actual, staticmethod):
                actual = actual.__func__

            if inspect.iscoroutinefunction(actual):
                raise TypeError('Listener function must not be a coroutine function.')

            actual.__sink_listener__ = True
            to_assign = name or actual.__name__

            try:
                actual.__sink_listener_names__.append(to_assign)
            except AttributeError:
                actual.__sink_listener_names__ = [to_assign]

            return func

        return decorator


class MultiAudioSink(AudioSink):
    def __init__(self, destinations: Sequence[AudioSink], /):
        # Intentionally not calling super().__init__ here
        if destinations is not None:
            for dest in destinations:
                self._register_child(dest)

        self._children: List[AudioSink] = list(destinations)

    def _register_child(self, child: AudioSink) -> None:
        if child in self.root.walk_children():
            raise RuntimeError('Sink is already registered.')

        child._parent = self

    @property
    def child(self) -> Optional[AudioSink]:
        return self._children[0] if self._children else None

    @property
    def children(self) -> Sequence[AudioSink]:
        return SequenceProxy(self._children)

    # TODO: add functions to add/remove children?


class BasicSink(AudioSink):
    """Simple callback based sink."""

    def __init__(
        self,
        event: BasicSinkWriteCB,
        *,
        rtcp_event: Optional[BasicSinkWriteRTCPCB] = None,
        decode: bool = True,
    ):
        super().__init__()

        self.cb = event
        self.cb_rtcp = rtcp_event
        self.decode = decode

    def wants_opus(self) -> bool:
        return not self.decode

    def write(self, user: Optional[User], data: VoiceData) -> None:
        self.cb(user, data)

    @AudioSink.listener()
    def on_rtcp_packet(self, packet: RTCPPacket, guild: discord.Guild) -> None:
        self.cb_rtcp(packet) if self.cb_rtcp else None

    def cleanup(self) -> None:
        pass


class WaveSink(AudioSink):
    """Endpoint AudioSink that generates a wav file.
    Best used in conjunction with a silence generating sink. (TBD)
    """

    CHANNELS = OpusDecoder.CHANNELS
    SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE // OpusDecoder.CHANNELS
    SAMPLING_RATE = OpusDecoder.SAMPLING_RATE

    def __init__(self, destination: wave._File):
        super().__init__()

        self._file: wave.Wave_write = wave.open(destination, 'wb')
        self._file.setnchannels(self.CHANNELS)
        self._file.setsampwidth(self.SAMPLE_WIDTH)
        self._file.setframerate(self.SAMPLING_RATE)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[User], data: VoiceData) -> None:
        self._file.writeframes(data.pcm)

    def cleanup(self) -> None:
        try:
            self._file.close()
        except Exception:
            log.warning("WaveSink got error closing file on cleanup", exc_info=True)


class FFmpegSink(AudioSink):
    @overload
    def __init__(
        self,
        *,
        filename: str,
        executable: str = 'ffmpeg',
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        on_error: Optional[FFmpegErrorCB] = None,
    ):
        ...

    @overload
    def __init__(
        self,
        *,
        buffer: IO[bytes],
        executable: str = 'ffmpeg',
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        on_error: Optional[FFmpegErrorCB] = None,
    ):
        ...

    def __init__(
        self,
        *,
        filename: str = MISSING,
        buffer: IO[bytes] = MISSING,
        executable: str = 'ffmpeg',
        stderr: Optional[IO[bytes]] = None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        on_error: Optional[FFmpegErrorCB] = None,
    ):
        super().__init__()

        self.filename: str = filename or 'pipe:1'
        self.buffer: IO[bytes] = buffer
        self.on_error: FFmpegErrorCB = on_error or self._on_error

        args = [executable, '-hide_banner']
        subprocess_kwargs: Dict[str, Any] = {'stdin': subprocess.PIPE}
        if self.buffer is not MISSING:
            subprocess_kwargs['stdout'] = subprocess.PIPE

        piping_stderr = False
        if stderr is not None:
            try:
                stderr.fileno()
            except Exception:
                piping_stderr = True
                subprocess_kwargs['stderr'] = subprocess.PIPE

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        # fmt: off
        args.extend((
            '-f', 's16le',
            '-ar', '48000',
            '-ac', '2',
            '-i', 'pipe:0',
            '-loglevel', 'warning',
            '-blocksize', str(discord.FFmpegAudio.BLOCKSIZE)
        ))
        # fmt: on

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append(self.filename)

        self._process: subprocess.Popen = MISSING
        self._process = self._spawn_process(args, **subprocess_kwargs)

        self._stdin: IO[bytes] = self._process.stdin  # type: ignore
        self._stdout: Optional[IO[bytes]] = None
        self._stderr: Optional[IO[bytes]] = None
        self._stdout_reader_thread: Optional[threading.Thread] = None
        self._stderr_reader_thread: Optional[threading.Thread] = None

        if self.buffer:
            n = f'popen-stout-reader:pid-{self._process.pid}'
            self._stdout = self._process.stdout
            _args = (self._stdout, self.buffer)
            self._stdout_reader_thread = threading.Thread(target=self._pipe_reader, args=_args, daemon=True, name=n)
            self._stdout_reader_thread.start()

        if piping_stderr:
            n = f'popen-stderr-reader:pid-{self._process.pid}'
            self._stderr = self._process.stderr
            _args = (self._stderr, stderr)
            self._stderr_reader_thread = threading.Thread(target=self._pipe_reader, args=_args, daemon=True, name=n)
            self._stderr_reader_thread.start()

    @staticmethod
    def _on_error(_self: FFmpegSink, error: Exception, data: Optional[VoiceData]) -> None:
        _self.voice_client.stop_listening()  # type: ignore

    def wants_opus(self) -> bool:
        return False

    def cleanup(self):
        self._kill_process()
        self._process = self._stdout = self._stdin = self._stderr = MISSING

    def write(self, user: Optional[User], data: VoiceData):
        if self._process and not self._stdin.closed:
            audio = data.opus if self.wants_opus() else data.pcm
            assert audio is not None
            try:
                self._stdin.write(audio)
            except Exception as e:
                log.exception('Error writing data to ffmpeg')
                self._kill_process()
                self.on_error(self, e, data)

    def _spawn_process(self, args: Any, **subprocess_kwargs: Any) -> subprocess.Popen:
        log.debug('Spawning ffmpeg process with command: %s, kwargs: %s', args, subprocess_kwargs)
        process = None
        try:
            process = subprocess.Popen(args, creationflags=discord.player.CREATE_NO_WINDOW, **subprocess_kwargs)
        except FileNotFoundError:
            executable = args.partition(' ')[0] if isinstance(args, str) else args[0]
            raise Exception(executable + ' was not found.') from None
        except subprocess.SubprocessError as exc:
            raise Exception(f'Popen failed: {exc.__class__.__name__}: {exc}') from exc
        else:
            return process

    def _kill_process(self) -> None:
        # this function gets called in __del__ so instance attributes might not even exist
        proc: subprocess.Popen = getattr(self, '_process', MISSING)
        if proc is MISSING:
            return

        log.debug('Terminating ffmpeg process %s.', proc.pid)

        try:
            self._stdin.close()
        except Exception:
            pass

        # TODO: extract wait time
        log.debug('Waiting for ffmpeg process %s for up to 5 seconds.', proc.pid)
        try:
            proc.wait(5)
        except Exception:
            pass

        try:
            proc.kill()
        except Exception:
            log.exception('Ignoring error attempting to kill ffmpeg process %s', proc.pid)

        if proc.poll() is None:
            log.info('ffmpeg process %s has not terminated. Waiting to terminate...', proc.pid)
            proc.communicate()
            log.info('ffmpeg process %s should have terminated with a return code of %s.', proc.pid, proc.returncode)
        else:
            log.info('ffmpeg process %s successfully terminated with return code of %s.', proc.pid, proc.returncode)

        self._process = MISSING

    def _pipe_reader(self, source: IO[bytes], dest: IO[bytes]) -> None:
        while self._process:
            if source.closed:
                return
            try:
                data = source.read(discord.FFmpegAudio.BLOCKSIZE)
            except (OSError, ValueError) as e:
                log.debug('FFmpeg stdin pipe closed: %s', e)
                return
            except Exception:
                log.debug('Read error for %s, this is probably not a problem', self, exc_info=True)
                return
            if data is None:
                return
            try:
                dest.write(data)
            except Exception as e:
                log.exception('Write error for %s', self)
                self._kill_process()
                self.on_error(self, e, None)
                return


class PCMVolumeTransformer(AudioSink):
    """AudioSink used to change the volume of PCM data, just like
    :class:`discord.PCMVolumeTransformer`.
    """

    def __init__(self, destination: AudioSink, volume: float = 1.0):
        if not isinstance(destination, AudioSink):
            raise TypeError(f'expected AudioSink not {type(destination).__name__}')

        if destination.wants_opus():
            raise VoiceRecvException('AudioSink must not request Opus encoding.')

        super().__init__(destination)

        self.destination: AudioSink = destination
        self._volume: float = volume

    def wants_opus(self) -> bool:
        return False

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = max(value, 0.0)

    def write(self, user: Optional[User], data: VoiceData) -> None:
        data.pcm = audioop.mul(data.pcm, 2, min(self._volume, 2.0))
        self.destination.write(user, data)

    def cleanup(self) -> None:
        pass


class ConditionalFilter(AudioSink):
    """AudioSink for filtering packets based on an arbitrary predicate function."""

    def __init__(self, destination: AudioSink, predicate: ConditionalFilterFn):
        super().__init__(destination)

        self.destination: AudioSink = destination
        self.predicate: ConditionalFilterFn = predicate

    def wants_opus(self) -> bool:
        return self.destination.wants_opus()

    def write(self, user: Optional[User], data: VoiceData) -> None:
        if self.predicate(user, data):
            self.destination.write(user, data)

    def cleanup(self) -> None:
        del self.predicate


class UserFilter(ConditionalFilter):
    """A convenience class for a User based ConditionalFilter."""

    def __init__(self, destination: AudioSink, user: User):
        super().__init__(destination, self._predicate)
        self.user: User = user

    def _predicate(self, user: Optional[User], data: VoiceData) -> bool:
        return user == self.user


class TimedFilter(ConditionalFilter):
    """A convenience class for a timed ConditionalFilter."""

    def __init__(self, destination: AudioSink, duration: float, *, start_on_init: bool = False):
        super().__init__(destination, self.predicate)
        self.duration: float = duration
        self.start_time: Optional[float]

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

    def get_time(self) -> float:
        """Function to generate a timestamp.  Defaults to `time.perf_counter()`.
        Can be overridden.
        """
        return time.perf_counter()


class SilenceGeneratorSink(AudioSink):
    """Generates intermittent silence packets during transmission downtime."""

    def __init__(self, destination: AudioSink):
        super().__init__(destination)

        self.destination: AudioSink = destination
        self.silencegen: SilenceGenerator = SilenceGenerator(self.destination.write)
        self.silencegen.start()

    def wants_opus(self) -> bool:
        return self.destination.wants_opus()

    def write(self, user: Optional[User], data: VoiceData) -> None:
        self.silencegen.push(user, data.packet)
        self.destination.write(user, data)

    @AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        self.silencegen.drop(ssrc=ssrc, user=member)

    def cleanup(self) -> None:
        self.silencegen.stop()
