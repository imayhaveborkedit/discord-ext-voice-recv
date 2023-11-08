# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    import speech_recognition as sr  # type: ignore
except ImportError:
    log.debug('speech_recognition module not found, not generating SpeechRecognitionSink')
    # TODO: generate stub?
else:
    import time
    import array
    import asyncio
    import audioop
    import threading

    from collections import defaultdict

    from ..sinks import AudioSink
    from ..rtp import SilencePacket

    from typing import TYPE_CHECKING, TypedDict

    if TYPE_CHECKING:
        from concurrent.futures import Future as CFuture
        from typing import Literal, Callable, Optional, Any, Final, Protocol, Awaitable, TypeVar

        from ..opus import VoiceData
        from ..types import MemberOrUser as User

        T = TypeVar('T')

        SRRecognizerMethod = Literal[
            'sphinx',
            'google',
            'google_cloud',
            'wit',
            'azure',
            'bing',
            'lex',
            'houndify',
            'amazon',
            'assemblyai',
            'ibm',
            'tensorflow',
            'whisper',
            'vosk',
        ]

        class SRStopper(Protocol):
            def __call__(self, wait: bool = True, /) -> None:
                ...

        SRProcessDataCB = Callable[[sr.Recognizer, sr.AudioData, User], Optional[str]]
        SRTextCB = Callable[[User, str], Any]

    __all__ = [
        'SpeechRecognitionSink',
    ]

    class _StreamData(TypedDict):
        stopper: Optional[SRStopper]
        recognizer: sr.Recognizer
        buffer: array.array[int]

    class SpeechRecognitionSink(AudioSink):
        def __init__(
            self,
            *,
            process_cb: Optional[SRProcessDataCB] = None,
            text_cb: Optional[SRTextCB] = None,
            default_recognizer: SRRecognizerMethod = 'google',
            phrase_time_limit: int = 10,
        ):
            super().__init__(None)
            self.process_cb: Optional[SRProcessDataCB] = process_cb
            self.text_cb: Optional[SRTextCB] = text_cb
            self.phrase_time_limmit: int = phrase_time_limit

            self.default_recognizer: SRRecognizerMethod = default_recognizer
            self.stream_data: defaultdict[int, _StreamData] = defaultdict(
                lambda: _StreamData(stopper=None, recognizer=sr.Recognizer(), buffer=array.array('B'))
            )

        def _await(self, coro: Awaitable[T]) -> CFuture[T]:
            assert self.client is not None
            return asyncio.run_coroutine_threadsafe(coro, self.client.loop)

        def wants_opus(self) -> bool:
            return False

        def write(self, user: Optional[User], data: VoiceData):
            # if isinstance(data.packet, SilencePacket):
            #     return

            if user is None:
                return

            # log.debug("Adding data for user %r", user)
            sdata = self.stream_data[user.id]
            # TODO: lock?  Do I even need to?
            sdata['buffer'].extend(data.pcm)

            if not sdata['stopper']:
                sdata['stopper'] = sdata['recognizer'].listen_in_background(
                    DiscordAudio(sdata['buffer']), self.background_listener(user), self.phrase_time_limmit
                )

        def background_listener(self, user: User):
            process_cb = self.process_cb or self.get_default_process_callback()
            text_cb = self.text_cb or self.get_default_text_callback()

            def callback(_recognizer: sr.Recognizer, _audio: sr.AudioData):
                output = process_cb(_recognizer, _audio, user)
                if output is not None:
                    text_cb(user, output)

            return callback

        def get_default_process_callback(self) -> SRProcessDataCB:
            def cb(recognizer: sr.Recognizer, audio: sr.AudioData, user: Optional[User]) -> Optional[str]:
                log.debug("Got %s, %s, %s", audio, audio.sample_rate, audio.sample_width)
                text: Optional[str] = None
                try:
                    func = getattr(recognizer, 'recognize_' + self.default_recognizer, recognizer.recognize_google)
                    text = func(audio)  # type: ignore
                except sr.UnknownValueError:
                    log.debug("bad speech chunk")
                    # self._debug_audio_chunk(audio)

                return text

            return cb

        def get_default_text_callback(self) -> SRTextCB:
            def cb(user: Optional[User], text: Optional[str]) -> Any:
                log.info("%s said: %s", user.display_name if user else 'Someone', text)

            return cb

        def cleanup(self):
            for sd in tuple(self.stream_data.values()):
                stop = sd.get('stopper')
                if stop:
                    stop()

                buf = sd.get('buffer')
                if buf is not None:
                    buf.clear()

        def _debug_audio_chunk(self, audio: sr.AudioData) -> None:
            import io, wave, discord

            with io.BytesIO() as b:
                with wave.open(b, 'wb') as writer:
                    writer.setframerate(48000)
                    writer.setsampwidth(2)
                    writer.setnchannels(2)
                    writer.writeframes(audio.get_wav_data())

                b.seek(0)
                f = discord.File(b, 'sound.wav')
                self._await(self.voice_client.channel.send(file=f))  # type: ignore

    class DiscordAudio(sr.AudioSource):
        little_endian: Final[bool] = True
        SAMPLE_RATE: Final[int] = 48_000
        SAMPLE_WIDTH: Final[int] = 2
        CHANNELS: Final[int] = 2
        CHUNK: Final[int] = 960

        def __init__(self, buffer: array.array[int]):
            self.buffer = buffer
            self._entered: bool = False

        @property
        def stream(self):
            return self

        def __enter__(self):
            if self._entered:
                log.warning('Already entered sr audio source')
            self._entered = True
            return self

        def __exit__(self, *exc):
            self._entered = False
            if any(exc):
                log.exception('Error closing audio source')

        def read(self, size: int) -> bytes:
            log.debug("Buffer size: %s", len(self.buffer))

            # TODO: make this timeout configurable
            for _ in range(10):
                if len(self.buffer) < size * self.CHANNELS:
                    time.sleep(0.1)
                else:
                    break
            else:
                if len(self.buffer) == 0:
                    return b''

            chunksize = size * self.CHANNELS

            audiochunk = self.buffer[:chunksize].tobytes()
            del self.buffer[: min(chunksize, len(audiochunk))]

            audiochunk = audioop.tomono(audiochunk, 2, 1, 1)

            log.debug("Returning chunk of size: %s", len(audiochunk))
            return audiochunk

        def close(self) -> None:
            self.buffer.clear()
