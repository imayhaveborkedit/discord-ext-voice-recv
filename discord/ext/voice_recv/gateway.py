# -*- coding: utf-8 -*-

# https://cdn.discordapp.com/attachments/381887113391505410/1094473412623204533/image.png

# IDENTIFY            = 0
# SELECT_PROTOCOL     = 1
# READY               = 2
# HEARTBEAT           = 3
# SESSION_DESCRIPTION = 4  or SELECT_PROTOCOL_ACK
# SPEAKING            = 5
# HEARTBEAT_ACK       = 6
# RESUME              = 7
# HELLO               = 8
# RESUMED             = 9
# CLIENT_CONNECT      = 12 or VIDEO
# CLIENT_DISCONNECT   = 13

# VOICE_BACKEND_VERSION = 16
# CHANNEL_OPTIONS_UPDATE = 17

import logging

import asyncio
from typing import TYPE_CHECKING

from discord.gateway import DiscordVoiceWebSocket

if TYPE_CHECKING:
    from .voice_client import VoiceRecvClient

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

async def hook(self: DiscordVoiceWebSocket, msg: dict):
    op: int = msg['op']
    data: dict = msg.get('d') # type: ignore
    vc: VoiceRecvClient = self._connection # type: ignore

    if op == self.SESSION_DESCRIPTION:
        # log.info("Doing voice hacks")
        # await _do_hacks(self)

        if vc._reader:
            vc._reader.update_secret_box()

    elif op == self.SPEAKING:
        # SPEAKING is not actually speaking anymore but it still has the ssrc
        vc._add_ssrc(int(data['user_id']), data['ssrc'])

    elif op == self.CLIENT_CONNECT:
        vc._add_ssrc(int(data['user_id']), data['audio_ssrc'])

    elif op == self.CLIENT_DISCONNECT:
        vc._remove_ssrc(user_id=int(data['user_id']))

async def _do_hacks(self):
    # Everything below this is a hack because discord keeps breaking things

    # hack #1
    # speaking needs to be set otherwise reconnecting makes you forget that the
    # bot is playing audio and you wont hear it until the bot sets speaking again
    await self.speak()

    # hack #3:
    # you need to wait for some indeterminate amount of time before sending silence
    await asyncio.sleep(0.5)

    # hack #2:
    # sending a silence packet is required to be able to read from the socket
    self._connection.send_audio_packet(b'\xF8\xFF\xFE', encode=False)

    # just so we don't have the speaking circle when we're not actually speaking
    await self.speak(False)
