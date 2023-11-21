# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import asyncio
import logging

import discord
from discord.voice_state import VoiceConnectionState
from discord.utils import MISSING

from typing import TYPE_CHECKING

from .gateway import hook
from .reader import AudioReader
from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Optional, Dict, Any, Union
    from discord.ext.commands._types import CoroFunc
    from .reader import AfterCB

from pprint import pformat

__all__ = [
    'VoiceRecvClient',
]

log = logging.getLogger(__name__)


class VoiceRecvClient(discord.VoiceClient):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
        super().__init__(client, channel)

        self._reader: Optional[AudioReader] = None
        self._ssrc_to_id: Dict[int, int] = {}
        self._id_to_ssrc: Dict[int, int] = {}
        self._event_listeners: Dict[str, list] = {}
        self._speaking_cache: Dict[int, float] = {}

    def create_connection_state(self) -> VoiceConnectionState:
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
            self._reader.router.destroy_all_decoders()

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

        if self._reader:
            self._reader.router.dispatch(event, *args, **kwargs)

        self.client.dispatch(event, *args, **kwargs)

    def cleanup(self) -> None:
        super().cleanup()
        self._event_listeners.clear()
        self.stop()

    def _add_ssrc(self, user_id: int, ssrc: int) -> None:
        self._ssrc_to_id[ssrc] = user_id
        self._id_to_ssrc[user_id] = ssrc

        if self._reader:
            self._reader.router.notify(ssrc, user_id)

    def _remove_ssrc(self, *, user_id: int) -> None:
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc:
            self._ssrc_to_id.pop(ssrc, None)

    def _get_ssrc_from_id(self, user_id: int) -> Optional[int]:
        return self._id_to_ssrc.get(user_id)

    def _get_id_from_ssrc(self, ssrc: int) -> Optional[int]:
        return self._ssrc_to_id.get(ssrc)

    def listen(self, sink: AudioSink, *, after: Optional[AfterCB] = None) -> None:
        """Receives audio into a :class:`AudioSink`."""
        # TODO: more info

        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')

        if not isinstance(sink, AudioSink):
            raise TypeError('sink must be an AudioSink not {0.__class__.__name__}'.format(sink))

        if self.is_listening():
            raise discord.ClientException('Already receiving audio.')

        self._reader = AudioReader(sink, self, after=after)
        self._reader.start()

    def is_listening(self) -> bool:
        """Indicates if we're currently receiving audio."""
        return self._reader is not None and self._reader.is_listening()

    def stop_listening(self) -> None:
        """Stops receiving audio."""
        if self._reader:
            self._reader.stop()
            self._reader = None

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

        if self._reader is None:
            raise ValueError('Not receiving anything.')

        self._reader.set_sink(sink)

    def get_speaking(self, member: Union[discord.Member, discord.User]) -> Optional[bool]:
        """Returns if a member is speaking (approximately), or None if not found."""

        ssrc = self._get_ssrc_from_id(member.id)
        if ssrc is None:
            return

        last_packet_time = self._speaking_cache.get(ssrc)
        if last_packet_time is None:
            return

        return time.perf_counter() - last_packet_time < 0.02
