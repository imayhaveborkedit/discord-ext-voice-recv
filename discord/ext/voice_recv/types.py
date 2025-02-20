# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import TYPE_CHECKING, List, Literal, Optional, TypedDict

from discord.types.snowflake import Snowflake

if TYPE_CHECKING:
    from typing import Union
    import discord

    MemberOrUser = Union[discord.Member, discord.User]

ResolutionTypes = Literal['fixed', 'source']
StreamTypes = Literal['audio', 'video', 'screen', 'test']  # only video appears to be used


class VideoResolution(TypedDict):
    height: int
    width: int
    type: ResolutionTypes


class VideoStream(TypedDict):
    type: StreamTypes
    active: bool
    max_bitrate: int
    max_framerate: int
    max_resolution: VideoResolution
    quality: int
    rid: int
    rtx_ssrc: int
    ssrc: int


class VoiceVideoPayload(TypedDict):
    audio_ssrc: int
    video_ssrc: int
    user_id: Snowflake
    streams: list[VideoStream]


class VoiceClientConnectPayload(TypedDict):
    user_ids: List[Snowflake]


class VoiceClientDisconnectPayload(TypedDict):
    user_id: Snowflake


class VoiceFlagsPayload(TypedDict):
    flags: Optional[int]
    user_id: Snowflake


class VoicePlatformPayload(TypedDict):
    platform: Optional[Literal[0, 1, 2, 3]]
    user_id: Snowflake
