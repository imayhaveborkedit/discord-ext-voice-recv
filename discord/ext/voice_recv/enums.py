from discord.flags import BaseFlags, fill_with_flags, flag_value
from discord.enums import Enum

__all__ = (
    'VoiceFlags',
    'VoicePlatform',
)

@fill_with_flags()
class VoiceFlags(BaseFlags):
    __slots__ = ()

    @flag_value
    def clips_enabled(self):
        return 1 << 0

    @flag_value
    def allow_voice_recording(self):
        return 1 << 1

    @flag_value
    def allow_any_viewer_clips(self):
        return 1 << 2


class VoicePlatform(Enum):
    desktop = 0
    mobile = 1
    xbox = 2
    playstation = 3
