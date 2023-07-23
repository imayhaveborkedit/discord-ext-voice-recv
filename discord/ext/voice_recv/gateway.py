# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from .video import VoiceVideoStreams

if TYPE_CHECKING:
    from discord.gateway import DiscordVoiceWebSocket
    from .voice_client import VoiceRecvClient

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


# https://cdn.discordapp.com/attachments/381887113391505410/1094473412623204533/image.png

IDENTIFY                  = 0
SELECT_PROTOCOL           = 1
READY                     = 2
HEARTBEAT                 = 3
SESSION_DESCRIPTION       = 4  # (aka SELECT_PROTOCOL_ACK)
SPEAKING                  = 5
HEARTBEAT_ACK             = 6
RESUME                    = 7
HELLO                     = 8
RESUMED                   = 9
CLIENT_CONNECT            = 12 # (aka VIDEO)
CLIENT_DISCONNECT         = 13
SESSION_UPDATE            = 14 # (useless)
VIDEO_SINK_WANTS          = 15 # (useless)
VOICE_BACKEND_VERSION     = 16 # (useless)
CHANNEL_OPTIONS_UPDATE    = 17 # (useless)
FLAGS                     = 18 # (???)
PLATFORM                  = 20 # (unpopulated)


async def hook(self: DiscordVoiceWebSocket, msg: dict):
    op: int = msg['op']
    data: dict = msg.get('d') # type: ignore
    vc: VoiceRecvClient = self._connection # type: ignore

    if op == self.READY:
        self.ssrc: int = data['ssrc'] # type: ignore
        vc._add_ssrc(vc.client.user.id, data['ssrc']) # type: ignore

    elif op == self.SESSION_DESCRIPTION:
        # log.info("Doing voice hacks")
        # await _do_hacks(self)

        if vc._reader:
            vc._reader.update_secret_box()

    elif op == self.SPEAKING:
        # SPEAKING is not actually speaking anymore but it still has the ssrc
        uid = int(data['user_id'])
        ssrc = data['ssrc']
        vc._add_ssrc(uid, ssrc)
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_speak", member, ssrc)

    # aka VIDEO
    elif op == self.CLIENT_CONNECT:
        uid = int(data['user_id'])
        vc._add_ssrc(uid, data['audio_ssrc'])
        member = vc.guild.get_member(uid)
        streams = VoiceVideoStreams(data=data, vc=vc) # type: ignore
        vc.dispatch("voice_member_video", member, streams)

    elif op == self.CLIENT_DISCONNECT:
        uid = int(data['user_id'])
        ssrc = vc._get_ssrc_from_id(uid)

        if vc._reader is not None and ssrc is not None:
            log.debug("Destroying decoder for %s, ssrc=%s", uid, ssrc)
            vc._reader.router.destroy_decoder(ssrc)

        vc._remove_ssrc(user_id=uid)
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_disconnect", member, ssrc)

    elif op == FLAGS:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_flags", member, data['flags'])

    elif op == PLATFORM:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_platform", member, data['platform'])
