# -*- coding: utf-8 -*-

from __future__ import annotations

import logging

from discord.enums import SpeakingState

from .video import VoiceVideoStreams

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Dict, Any

    from discord.gateway import DiscordVoiceWebSocket
    from .voice_client import VoiceRecvClient

log = logging.getLogger(__name__)


# https://cdn.discordapp.com/attachments/381887113391505410/1094473412623204533/image.png
# fmt: off
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
# fmt: on


async def hook(self: DiscordVoiceWebSocket, msg: Dict[str, Any]):
    op: int = msg['op']
    data: Dict[str, Any] = msg.get('d', {})
    vc: VoiceRecvClient = self._connection.voice_client  # type: ignore

    if op not in (3, 6):
        from pprint import pformat

        log.debug("Received op %s: \n%s", op, pformat(data, compact=True))

        if len(msg.keys()) > 2:
            m = msg.copy()
            m.pop('op')
            m.pop('d')
            log.info("WS payload has extra keys: %s", m)

    if op == self.READY:
        # why do i assign this
        # self.ssrc: int = data['ssrc']  # type#: ignore
        vc._add_ssrc(vc.client.user.id, data['ssrc'])  # type: ignore

    elif op == self.SESSION_DESCRIPTION:
        if vc._reader:
            # This needs a typing fix in dpy
            vc._reader.update_secret_key(bytes(self.secret_key)) # type: ignore

    elif op == self.SPEAKING:
        # this event refers to the speaking MODE, e.g. priority speaker
        # it also sends the user's ssrc
        uid = int(data['user_id'])
        ssrc = data['ssrc']
        vc._add_ssrc(uid, ssrc)
        member = vc.guild.get_member(uid)
        state = SpeakingState.try_value(data['speaking'])  # type: ignore
        vc.dispatch("voice_member_speaking_state", member, ssrc, state)

    # aka VIDEO
    elif op == self.CLIENT_CONNECT:
        uid = int(data['user_id'])
        vc._add_ssrc(uid, data['audio_ssrc'])
        member = vc.guild.get_member(uid)
        streams = VoiceVideoStreams(data=data, vc=vc)  # type: ignore
        vc.dispatch("voice_member_video", member, streams)

    elif op == self.CLIENT_DISCONNECT:
        uid = int(data['user_id'])
        ssrc = vc._get_ssrc_from_id(uid)

        if vc._reader is not None and ssrc is not None:
            log.debug("Destroying decoder for %s, ssrc=%s", uid, ssrc)
            vc._reader.packet_router.destroy_decoder(ssrc)

        vc._remove_ssrc(user_id=uid)
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_disconnect", member, ssrc)
        vc._speaking_cache.pop(ssrc, None)  # type: ignore

    elif op == FLAGS:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_flags", member, data['flags'])

    elif op == PLATFORM:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_platform", member, data['platform'])
