# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import (
        VoiceVideoPayload,
        VideoStream as VideoStreamPayload,
        VideoResolution as VideoResolutionPayload,
    )
    from .voice_client import VoiceRecvClient

__all__ = [
    'VoiceVideoStreams',
]


class VoiceVideoStreams:
    __slots__ = (
        'audio_ssrc',
        'video_ssrc',
        'member',
        'streams',
    )

    def __init__(self, *, data: VoiceVideoPayload, vc: VoiceRecvClient):
        self.audio_ssrc = data['audio_ssrc']
        self.video_ssrc = data['video_ssrc']
        self.member = vc.guild.get_member(int(data['user_id']))
        self.streams = self._get_streams(data['streams'])

    def __repr__(self) -> str:
        return f"<VoiceVideoStreams member={self.member!s} streams={self._minify_streams()}>"

    def _get_streams(self, data: list[VideoStreamPayload]) -> list[VideoStreamInfo]:
        return [VideoStreamInfo(data=stream) for stream in data]

    def _minify_streams(self) -> str:
        streams = [f"<rid={s.rid} active={s.active}>" for s in self.streams]
        return f"[{', '.join(streams)}]"


class VideoStreamInfo:
    __slots__ = (
        'active',
        'max_framerate',
        'max_resolution',
        'quality',
        'rid',
        'rtx_ssrc',
        'ssrc',
    )

    def __init__(self, *, data: VideoStreamPayload):
        self.active = data['active']
        self.max_framerate = data['max_framerate']
        self.max_resolution = VideoStreamResolution(data['max_resolution'])
        self.quality = data['quality']
        self.rid = data['rid']
        self.rtx_ssrc = data['rtx_ssrc']
        self.ssrc = data['ssrc']

    def __repr__(self) -> str:
        attrs = [
            ('ssrc', self.ssrc),
            ('active', self.active),
            ('quality', self.quality),
            ('max_framerate', self.max_framerate),
            ('max_resolution', self.max_resolution),
        ]
        inner = ' '.join('%s=%r' % t for t in attrs)
        return f'<{self.__class__.__name__} {inner}>'


class VideoStreamResolution:
    __slots__ = (
        'height',
        'width',
        'type',
    )

    def __init__(self, data: VideoResolutionPayload):
        self.height = data['height']
        self.width = data['width']
        self.type = data['type']

    def __repr__(self) -> str:
        return f"<VideoStreamResolution width={self.width!r} height={self.height!r} type={self.type!r}>"
