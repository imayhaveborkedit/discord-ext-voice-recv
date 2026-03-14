# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
import struct

from discord.enums import SpeakingState, try_enum

from .enums import VoiceFlags, VoicePlatform
from .video import VoiceVideoStreams

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import Dict, Any

    from discord.gateway import DiscordVoiceWebSocket
    from .voice_client import VoiceRecvClient
    from .video import VoiceVideoPayload

log = logging.getLogger(__name__)


# https://docs.discord.com/developers/topics/opcodes-and-status-codes#voice
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
CLIENT_CONNECT            = 11
VIDEO                     = 12
CLIENT_DISCONNECT         = 13
SESSION_UPDATE            = 14 # (useless)
MEDIA_SINK_WANTS          = 15 # (useless)
VOICE_BACKEND_VERSION     = 16 # (useless)
CHANNEL_OPTIONS_UPDATE    = 17 # (dead)
FLAGS                     = 18
SPEED_TEST                = 19 # (dead)
PLATFORM                  = 20
DAVE_PREPARE_TRANSITION        = 21
DAVE_EXECUTE_TRANSITION        = 22
DAVE_TRANSITION_READY          = 23
DAVE_PREPARE_EPOCH             = 24
MLS_EXTERNAL_SENDER            = 25
MLS_KEY_PACKAGE                = 26
MLS_PROPOSALS                  = 27
MLS_COMMIT_WELCOME             = 28
MLS_ANNOUNCE_COMMIT_TRANSITION = 29
MLS_WELCOME                    = 30
MLS_INVALID_COMMIT_WELCOME     = 31
# fmt: on

DAVE_AND_MLS_OPCODES = frozenset(
    {
        DAVE_PREPARE_TRANSITION,
        DAVE_EXECUTE_TRANSITION,
        DAVE_TRANSITION_READY,
        DAVE_PREPARE_EPOCH,
        MLS_EXTERNAL_SENDER,
        MLS_KEY_PACKAGE,
        MLS_PROPOSALS,
        MLS_COMMIT_WELCOME,
        MLS_ANNOUNCE_COMMIT_TRANSITION,
        MLS_WELCOME,
        MLS_INVALID_COMMIT_WELCOME,
    }
)

_BINARY_HOOK_PATCHED = False


def install_binary_ws_hook() -> None:
    global _BINARY_HOOK_PATCHED
    if _BINARY_HOOK_PATCHED:
        return

    from discord.gateway import DiscordVoiceWebSocket

    original = DiscordVoiceWebSocket.received_binary_message
    if getattr(original, "__voice_recv_binary_hook__", False):
        _BINARY_HOOK_PATCHED = True
        return

    async def wrapped(self: DiscordVoiceWebSocket, msg: bytes):  # type: ignore[misc]
        op = -1
        seq = None
        body = b""

        if len(msg) >= 3:
            seq = struct.unpack_from(">H", msg, 0)[0]
            op = msg[2]
            body = msg[3:]

        vc = getattr(getattr(self, "_connection", None), "voice_client", None)
        cb = getattr(vc, "_update_voice_ws_binary_state", None) if vc is not None else None
        if op >= 0 and callable(cb):
            try:
                cb(op, body, seq=seq, raw_len=len(msg))
            except Exception:
                log.exception("Failed to capture voice websocket binary event: op=%s", op)

        return await original(self, msg)

    setattr(wrapped, "__voice_recv_binary_hook__", True)
    DiscordVoiceWebSocket.received_binary_message = wrapped
    _BINARY_HOOK_PATCHED = True


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

    vc._update_voice_ws_state(op, data, raw_message=msg)

    if op == self.READY:
        vc._add_ssrc(vc.guild.me.id, data['ssrc'])

    elif op == self.SESSION_DESCRIPTION:
        if vc._reader:
            # TODO: remove bytes cast once type is fixed in dpy
            vc._reader.update_secret_key(bytes(self.secret_key))  # type: ignore

    elif op == self.SPEAKING:
        # this event refers to the speaking MODE, e.g. priority speaker
        # it also sends the user's ssrc
        uid = int(data['user_id'])
        ssrc = data['ssrc']
        vc._add_ssrc(uid, ssrc)
        member = vc.guild.get_member(uid)
        state = try_enum(SpeakingState, data['speaking'])
        vc.dispatch("voice_member_speaking_state", member, ssrc, state)

    elif op == CLIENT_CONNECT:
        uids = [int(uid) for uid in data['user_ids']]

        # Multiple user IDs means this is the initial member list
        for uid in uids:
            member = vc.guild.get_member(uid)
            vc.dispatch("voice_member_connect", member)

    elif op == VIDEO:
        uid = int(data['user_id'])
        vc._add_ssrc(uid, data['audio_ssrc'])
        member = vc.guild.get_member(uid)
        streams = VoiceVideoStreams(data=cast('VoiceVideoPayload', data), vc=vc)
        vc._update_video_ssrcs(uid, streams)
        vc.dispatch("voice_member_video", member, streams)

    elif op == CLIENT_DISCONNECT:
        uid = int(data['user_id'])
        ssrc = vc._get_ssrc_from_id(uid)

        if vc._reader and ssrc is not None:
            log.debug("Destroying decoder for %s, ssrc=%s", uid, ssrc)
            vc._reader.packet_router.destroy_decoder(ssrc)

        vc._remove_ssrc(user_id=uid)
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_disconnect", member, ssrc)

    elif op == FLAGS:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch("voice_member_flags", member, VoiceFlags._from_value(data['flags'] or 0))

    elif op == PLATFORM:
        uid = int(data['user_id'])
        member = vc.guild.get_member(uid)
        vc.dispatch(
            "voice_member_platform",
            member,
            try_enum(VoicePlatform, data['platform']) if data['platform'] is not None else None,
        )

    elif op in DAVE_AND_MLS_OPCODES:
        vc.dispatch("voice_dave_opcode", op, data)
