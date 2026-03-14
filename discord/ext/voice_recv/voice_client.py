# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import asyncio
import logging
import base64

import discord
from discord.voice_state import VoiceConnectionState
from discord.utils import MISSING

from typing import TYPE_CHECKING

from .gateway import hook, install_binary_ws_hook, DAVE_AND_MLS_OPCODES
from .reader import AudioReader
from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Optional, Dict, Any, Union, Set
    from discord.ext.commands._types import CoroFunc
    from .reader import AfterCB
    from .video import VoiceVideoStreams

from pprint import pformat

__all__ = [
    'VoiceRecvClient',
]

log = logging.getLogger(__name__)


class VoiceRecvClient(discord.VoiceClient):
    endpoint_ip: str
    voice_port: int

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
        super().__init__(client, channel)

        self._reader: AudioReader = MISSING
        self._ssrc_to_id: Dict[int, int] = {}
        self._id_to_ssrc: Dict[int, int] = {}
        self._ssrc_media_kind: Dict[int, str] = {}
        self._user_stream_ssrcs: Dict[int, Set[int]] = {}
        self._event_listeners: Dict[str, list] = {}
        self._voice_ws_recent_ops: list[int] = []
        self._voice_ws_last_payloads: Dict[int, Dict[str, Any]] = {}
        self._voice_ws_pending_events: list[Dict[str, Any]] = []
        self._dave_ws_recent_ops: list[int] = []
        self._dave_ws_last_payloads: Dict[int, Dict[str, Any]] = {}

    def create_connection_state(self) -> VoiceConnectionState:
        install_binary_ws_hook()
        return VoiceConnectionState(self, hook=hook)

    async def on_voice_state_update(self, data) -> None:
        old_channel_id = self.channel.id if self.channel else None

        await super().on_voice_state_update(data)

        log.debug("Got voice_client VSU: \n%s", pformat(data, compact=True))

        # this can be None
        try:
            channel_id = int(data['channel_id'])
        except TypeError:
            return

        # if we joined, left, or switched channels, reset the decoders
        if self._reader and channel_id != old_channel_id:
            log.debug("Destroying all decoders in guild %s", self.guild.id)
            self._reader.packet_router.destroy_all_decoders()

    def add_listener(self, func: CoroFunc, *, name: str = MISSING) -> None:
        name = func.__name__ if name is MISSING else name

        if not asyncio.iscoroutinefunction(func):
            raise TypeError('Listeners must be coroutines')

        if name in self._event_listeners:
            self._event_listeners[name].append(func)
        else:
            self._event_listeners[name] = [func]

    def remove_listener(self, func: CoroFunc, *, name: str = MISSING) -> None:
        name = func.__name__ if name is MISSING else name

        if name in self._event_listeners:
            try:
                self._event_listeners[name].remove(func)
            except ValueError:
                pass

    async def _run_event(self, coro: CoroFunc, event_name: str, *args: Any, **kwargs: Any) -> None:
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error calling %s", event_name)

    def _schedule_event(self, coro: CoroFunc, event_name: str, *args: Any, **kwargs: Any) -> asyncio.Task:
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        return self.client.loop.create_task(wrapped, name=f"ext.voice_recv: {event_name}")

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        log.debug("Dispatching voice_client event %s", event)

        event_name = f"on_{event}"
        for coro in self._event_listeners.get(event_name, []):
            self._schedule_event(coro, event_name, *args, **kwargs)

        self.dispatch_sink(event, *args, **kwargs)
        self.client.dispatch(event, *args, **kwargs)

    def dispatch_sink(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        if self._reader:
            self._reader.event_router.dispatch(event, *args, **kwargs)

    def _record_voice_ws_event(self, event: Dict[str, Any]) -> None:
        op = int(event.get('op', -1))
        data = event.get('d')
        payload = dict(data) if isinstance(data, dict) else {}

        if op >= 0:
            self._voice_ws_recent_ops.append(op)
            if len(self._voice_ws_recent_ops) > 512:
                self._voice_ws_recent_ops = self._voice_ws_recent_ops[-512:]
            self._voice_ws_last_payloads[op] = payload

            if op in DAVE_AND_MLS_OPCODES:
                self._dave_ws_recent_ops.append(op)
                if len(self._dave_ws_recent_ops) > 256:
                    self._dave_ws_recent_ops = self._dave_ws_recent_ops[-256:]
                self._dave_ws_last_payloads[op] = payload

        if self._reader and hasattr(self._reader, 'analysis_stats'):
            self._reader.analysis_stats.add_voice_ws_event(event)
        else:
            self._voice_ws_pending_events.append(event)
            if len(self._voice_ws_pending_events) > 1024:
                self._voice_ws_pending_events = self._voice_ws_pending_events[-1024:]

    def _update_voice_ws_state(self, op: int, data: Dict[str, Any], *, raw_message: Optional[Dict[str, Any]] = None) -> None:
        payload = dict(data)
        extra: Dict[str, Any] = {}
        if isinstance(raw_message, dict):
            for key, value in raw_message.items():
                if key in ('op', 'd'):
                    continue
                extra[key] = value

        event: Dict[str, Any] = {
            'transport': 'json',
            'op': int(op),
            'd': payload,
            'extra': extra,
            'ts_unix_ms': int(time.time() * 1000),
        }
        self._record_voice_ws_event(event)

    def _update_voice_ws_binary_state(self, op: int, payload: bytes, *, seq: Optional[int], raw_len: int) -> None:
        event: Dict[str, Any] = {
            'transport': 'binary',
            'op': int(op),
            'd': {
                '_binary_b64': base64.b64encode(payload).decode('ascii'),
            },
            'extra': {
                'seq': seq,
                'payload_len': len(payload),
                'raw_len': raw_len,
            },
            'ts_unix_ms': int(time.time() * 1000),
        }
        self._record_voice_ws_event(event)

    def _flush_voice_ws_pending_events(self) -> None:
        if not self._reader or not hasattr(self._reader, 'analysis_stats'):
            return
        if not self._voice_ws_pending_events:
            return

        for event in self._voice_ws_pending_events:
            self._reader.analysis_stats.add_voice_ws_event(event)
        self._voice_ws_pending_events.clear()

    def get_recv_diagnostics(self) -> Dict[str, Any]:
        if self._reader and hasattr(self._reader, 'analysis_stats'):
            return self._reader.analysis_stats.snapshot()
        return {}

    def cleanup(self) -> None:
        # TODO: Does the order here matter?
        super().cleanup()
        self._event_listeners.clear()
        self.stop()

    def _add_ssrc(self, user_id: int, ssrc: int, *, kind: str = 'audio') -> None:
        if not ssrc:
            return

        self._ssrc_to_id[ssrc] = user_id
        self._ssrc_media_kind[ssrc] = kind
        if kind == 'audio':
            self._id_to_ssrc[user_id] = ssrc

        if kind == 'audio' and self._reader:
            self._reader.packet_router.set_user_id(ssrc, user_id)
            self._reader.flush_pending_unknown_for_ssrc(ssrc)

    def _update_video_ssrcs(self, user_id: int, streams: 'VoiceVideoStreams') -> None:
        self._add_ssrc(user_id, streams.audio_ssrc, kind='audio')

        current: set[int] = set()

        if streams.video_ssrc:
            current.add(int(streams.video_ssrc))
            self._add_ssrc(user_id, int(streams.video_ssrc), kind='video')

        for stream in streams.streams:
            stream_ssrc = int(stream.ssrc)
            stream_kind = stream.type if stream.type in ('video', 'screen', 'test') else 'video'
            current.add(stream_ssrc)
            self._add_ssrc(user_id, stream_ssrc, kind=stream_kind)

            if stream.rtx_ssrc:
                rtx_ssrc = int(stream.rtx_ssrc)
                current.add(rtx_ssrc)
                self._add_ssrc(user_id, rtx_ssrc, kind='rtx')

        previous = self._user_stream_ssrcs.get(user_id, set())
        for stale_ssrc in previous - current:
            if stale_ssrc == self._id_to_ssrc.get(user_id):
                continue
            self._ssrc_to_id.pop(stale_ssrc, None)
            self._ssrc_media_kind.pop(stale_ssrc, None)

        self._user_stream_ssrcs[user_id] = current

    def _remove_ssrc(self, *, user_id: int) -> None:
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc:
            if self._reader:
                self._reader.speaking_timer.drop_ssrc(ssrc)
            self._ssrc_to_id.pop(ssrc, None)
            self._ssrc_media_kind.pop(ssrc, None)

        for stream_ssrc in self._user_stream_ssrcs.pop(user_id, set()):
            if stream_ssrc == ssrc:
                continue
            self._ssrc_to_id.pop(stream_ssrc, None)
            self._ssrc_media_kind.pop(stream_ssrc, None)

    def _get_ssrc_from_id(self, user_id: int) -> Optional[int]:
        return self._id_to_ssrc.get(user_id)

    def _get_id_from_ssrc(self, ssrc: int) -> Optional[int]:
        return self._ssrc_to_id.get(ssrc)

    def _get_ssrc_media_kind(self, ssrc: int) -> str:
        return self._ssrc_media_kind.get(ssrc, 'unknown')

    def listen(
        self,
        sink: AudioSink,
        *,
        after: Optional[AfterCB] = None,
        debug_ws_path: Optional[str] = None,
    ) -> None:
        """Receives audio into a :class:`AudioSink`.

        If ``debug_ws_path`` is provided, voice WS events are mirrored to JSONL.
        """
        # TODO: more info

        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')

        if not isinstance(sink, AudioSink):
            raise TypeError('sink must be an AudioSink not {0.__class__.__name__}'.format(sink))

        if self.is_listening():
            raise discord.ClientException('Already receiving audio.')

        self._reader = AudioReader(
            sink,
            self,
            after=after,
            ws_jsonl_path=debug_ws_path.strip() if isinstance(debug_ws_path, str) else "",
        )
        self._reader.start()
        self._flush_voice_ws_pending_events()

    def is_listening(self) -> bool:
        """Indicates if we're currently receiving audio."""
        return self._reader and self._reader.is_listening()

    def stop_listening(self) -> None:
        """Stops receiving audio."""
        if self._reader:
            self._reader.stop()
            self._reader = MISSING

    def stop_playing(self) -> None:
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def stop(self) -> None:
        """Stops playing and receiving audio."""
        self.stop_playing()
        self.stop_listening()

    @property
    def sink(self) -> Optional[AudioSink]:
        return self._reader.sink if self._reader else None

    @sink.setter
    def sink(self, sink: AudioSink) -> None:
        if not isinstance(sink, AudioSink):
            raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(sink))

        if not self._reader:
            raise ValueError('Not receiving anything.')

        self._reader.set_sink(sink)

    def get_speaking(self, member: Union[discord.Member, discord.User]) -> Optional[bool]:
        """Returns if a member is speaking (approximately), or None if not found."""

        ssrc = self._get_ssrc_from_id(member.id)
        if ssrc is None:
            return

        if self._reader:
            return self._reader.speaking_timer.get_speaking(ssrc)
