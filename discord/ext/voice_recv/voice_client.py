# -*- coding: utf-8 -*-

import discord
import threading

from .gateway import hook
from .reader import AudioReader

class VoiceRecvClient(discord.VoiceClient):
    def __init__(self, client, channel):
        super().__init__(client, channel)

        self._connecting = threading.Condition()
        self._reader = None
        self._ssrcs = {} # TODO: switch to bidict(?)

    async def connect_websocket(self):
        ws = await discord.gateway.DiscordVoiceWebSocket.from_client(self, hook=hook)
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws

    async def on_voice_state_update(self, data):
        await super().on_voice_state_update(data)

        channel_id = data['channel_id']
        guild_id = int(data['guild_id'])
        user_id = int(data['user_id'])

        if channel_id and int(channel_id) != self.channel.id and self._reader:
            # someone moved channels
            if self._connection.user.id == user_id:
                # we moved channels
                # print("Resetting all decoders")
                self._reader._reset_decoders()

            # TODO: figure out how to check if either old/new channel
            #       is ours so we don't go around resetting decoders
            #       for irrelevant channel moving

            else:
                # someone else moved channels
                # print(f"ws: Attempting to reset decoder for {user_id}")
                ssrc, _ = self._get_ssrc_mapping(user_id=data['user_id'])
                self._reader._reset_decoders(ssrc)

    # async def on_voice_server_update(self, data):
    #     await super().on_voice_server_update(data)
    #     ...


    def cleanup(self):
        super().cleanup()
        self.stop()

    # TODO: copy over new functions
    # add/remove/get ssrc


    def listen(self, sink):
        """Receives audio into a :class:`AudioSink`. TODO: wording"""

        if not self.is_connected():
            raise ClientException('Not connected to voice.')

        if not isinstance(sink, AudioSink):
            raise TypeError('sink must be an AudioSink not {0.__class__.__name__}'.format(sink))

        if self.is_listening():
            raise ClientException('Already receiving audio.')

        self._reader = AudioReader(sink, self)
        self._reader.start()

    def is_listening(self):
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
    def sink(self):
        return self._reader.sink if self._reader else None

    @sink.setter
    def sink(self, value):
        if not isinstance(value, AudioSink):
            raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(value))

        if self._reader is None:
            raise ValueError('Not receiving anything.')

        self._reader._set_sink(sink)
