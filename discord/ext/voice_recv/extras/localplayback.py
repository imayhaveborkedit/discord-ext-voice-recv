# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

from ..sinks import AudioSink

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..opus import VoiceData
    from ..types import MemberOrUser


__all__ = [
    'LocalPlaybackSink',
    'SimpleLocalPlaybackSink',
]

log = logging.getLogger(__name__)

try:
    import pyaudio
except ImportError:

    def __getattr__(name: str):
        if name in __all__:
            raise RuntimeError('The pyaudio module is required to use this sink.')

else:
    if TYPE_CHECKING:
        from typing import Optional, Dict

        from discord import Member

        PyAudioStream = pyaudio._Stream

    class _BaseLocalPlaybackSink(AudioSink):
        pa: pyaudio.PyAudio = None  # type: ignore

        def __init__(self, output_device_id: Optional[int] = None, *, py_audio: Optional[pyaudio.PyAudio] = None):
            self._init_pa(py_audio)

            if output_device_id is None:
                output_device_id = self.pa.get_default_output_device_info().get("index")  # type: ignore
            self.output_device_id = output_device_id

        @classmethod
        def _init_pa(cls, pa: Optional[pyaudio.PyAudio]) -> None:
            if pa is None:
                if cls.pa is None:
                    cls.pa = pyaudio.PyAudio()
            else:
                if cls.pa is None:
                    cls.pa = pa
                elif cls.pa is not pa:
                    raise RuntimeError("Conflicting PyAudio objects")

        def write(self, user: Optional[MemberOrUser], data: VoiceData) -> None:
            raise NotImplementedError

        def wants_opus(self) -> bool:
            return False

        @classmethod
        def terminate_pyaudio(cls):
            """Call this when you are completely done using all instances of LocalPlayback sinks."""

            cls.pa.terminate()
            cls.pa = None  # type: ignore

    class SimpleLocalPlaybackSink(_BaseLocalPlaybackSink):
        """
        A simplified version of LocalPlaybackSink that only supports one stream of audio.
        Convenient for when you have already isolated a single member's audio.
        """

        def __init__(self, output_device_id: Optional[int] = None, *, py_audio: Optional[pyaudio.PyAudio] = None):
            super().__init__(output_device_id, py_audio=py_audio)
            self._stream: PyAudioStream = self.pa.open(
                rate=48000,
                channels=2,
                format=pyaudio.paInt16,
                output=True,
                output_device_index=output_device_id,
            )

        def write(self, user: Optional[MemberOrUser], data: VoiceData) -> None:
            self._stream.write(data.pcm)

        def cleanup(self) -> None:
            self._stream.close()

    class LocalPlaybackSink(_BaseLocalPlaybackSink):
        """
        An AudioSink for playing received audio directly to one of the system's audio output devices using PyAudio.
        This sink can handle playback of multiple users' audio without additional stream mixing beforehand.

        The `output_device_id` parameter defaults to the system's default audio device, and can otherwise be
        acquired via PyAudio functions.  A specific `PyAudio` instance can also be passed to use a specific instance.
        """

        def __init__(self, output_device_id: Optional[int] = None, *, py_audio: Optional[pyaudio.PyAudio] = None):
            super().__init__(output_device_id, py_audio=py_audio)
            self._streams: Dict[int, PyAudioStream] = {}

        def _get_stream(self, user: MemberOrUser) -> PyAudioStream:
            stream = self._streams.get(user.id)
            if stream is None:
                stream = self._streams[user.id] = self.pa.open(
                    rate=48000,
                    channels=2,
                    format=pyaudio.paInt16,
                    output=True,
                    output_device_index=self.output_device_id,
                )
            return stream

        def write(self, user: Optional[MemberOrUser], data: VoiceData) -> None:
            if user:
                self._get_stream(user).write(data.pcm)

        def cleanup(self) -> None:
            for stream in tuple(self._streams.values()):
                stream.close()

        @AudioSink.listener()
        def on_voice_member_disconnect(self, member: Member, ssrc: Optional[int]) -> None:
            stream = self._streams.pop(member.id, None)
            if stream:
                stream.close()
