# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import logging
import threading

import discord
from discord.gateway import DiscordVoiceWebSocket
from discord.utils import MISSING

from typing import TYPE_CHECKING

from .gateway import hook
from .reader import AudioReader
from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Optional, Dict, Any
    from discord.ext.commands._types import CoroFunc
    from .reader import AfterCB

from pprint import pformat

__all__ = [
    "VoiceRecvClient"
]

log = logging.getLogger(__name__)

class VoiceRecvClient(discord.VoiceClient):
    def __init__(self, client, channel):
        super().__init__(client, channel)

        self._connecting = threading.Condition()
        self._reader: Optional[AudioReader] = None
        self._ssrc_to_id: Dict[int, int] = {}
        self._id_to_ssrc: Dict[int, int] = {}
        self._event_listeners: dict[str, list] = {}

    async def connect_websocket(self):
        ws = await DiscordVoiceWebSocket.from_client(self, hook=hook)
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws

    async def on_voice_state_update(self, data):
        old_channel_id = self.channel.id if self.channel else None

        await super().on_voice_state_update(data)

        log.debug("Got voice_client VSU: \n%s", pformat(data, compact=True))

        channel_id = int(data['channel_id'])

        # if we joined, left, or switched channels, reset the decoders
        if self._reader and channel_id != old_channel_id:
            log.debug("Destroying all decoders in guild %s", self.guild.id)
            self._reader.router.destroy_all_decoders()

    def add_listener(self, func: CoroFunc, *, name: str=MISSING):
        name = func.__name__ if name is MISSING else name

        if not asyncio.iscoroutinefunction(func):
            raise TypeError('Listeners must be coroutines')

        if name in self._event_listeners:
            self._event_listeners[name].append(func)
        else:
            self._event_listeners[name] = [func]

    def remove_listener(self, func: CoroFunc, *, name: str=MISSING):
        name = func.__name__ if name is MISSING else name

        if name in self._event_listeners:
            try:
                self._event_listeners[name].remove(func)
            except ValueError:
                pass

    async def _run_event(self, coro, event_name, *args, **kwargs):
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error calling %s", event_name)

    def _schedule_event(self, coro, event_name, *args, **kwargs):
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        return self.client.loop.create_task(wrapped, name=f"ext.voice_recv: {event_name}")

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any):
        log.debug("Dispatching voice_client event %s", event)

        event_name = f"on_{event}"
        for coro in self._event_listeners.get(event_name, []):
            self._schedule_event(coro, event_name, *args, **kwargs)

        if self._reader:
            self._reader.router.dispatch(event, *args, **kwargs)

        self.client.dispatch(event, *args, **kwargs)

    def cleanup(self):
        super().cleanup()
        self._event_listeners.clear()
        self.stop()

    def _add_ssrc(self, user_id: int, ssrc: int):
        self._ssrc_to_id[ssrc] = user_id
        self._id_to_ssrc[user_id] = ssrc

        if self._reader:
            self._reader.router.notify(ssrc, user_id)

    def _remove_ssrc(self, *, user_id: int):
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc:
            self._ssrc_to_id.pop(ssrc, None)

    def _get_ssrc_from_id(self, user_id: int) -> Optional[int]:
        return self._id_to_ssrc.get(user_id)

    def _get_id_from_ssrc(self, ssrc: int) -> Optional[int]:
        return self._ssrc_to_id.get(ssrc)

    def listen(self, sink: AudioSink, *, after: Optional[AfterCB]=None):
        """Receives audio into a :class:`AudioSink`. TODO: more info"""

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

    def stop_listening(self):
        """Stops receiving audio."""
        if self._reader:
            self._reader.stop()
            self._reader = None

    def stop_playing(self):
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def stop(self):
        """Stops playing and receiving audio."""
        self.stop_playing()
        self.stop_listening()

    @property
    def sink(self) -> Optional[AudioSink]:
        return self._reader.sink if self._reader else None

    @sink.setter
    def sink(self, sink: AudioSink):
        if not isinstance(sink, AudioSink):
            raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(sink))

        if self._reader is None:
            raise ValueError('Not receiving anything.')

        self._reader.set_sink(sink)
