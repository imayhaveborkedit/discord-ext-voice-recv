# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional, TypedDict

from discord.types.snowflake import Snowflake

if TYPE_CHECKING:
    from typing import Union
    import discord

    MemberOrUser = Union[discord.Member, discord.User]

ResolutionTypes = Literal['fixed']


class VideoResolution(TypedDict):
    height: int
    width: int
    type: ResolutionTypes


class VideoStream(TypedDict):
    active: bool
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


class VoiceClientDisconnectPayload(TypedDict):
    user_id: Snowflake


class VoiceFlagsPayload(TypedDict):
    flags: int
    user_id: Snowflake


class VoicePlatformPayload(TypedDict):
    platform: Optional[Union[str, int]]  # unknown because ive never actually seen it
    user_id: Snowflake
