# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Literal, Optional, TypedDict

from discord.types.snowflake import Snowflake


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
    platform: Optional[str | int] # unknown because ive never seen it
    user_id: Snowflake
