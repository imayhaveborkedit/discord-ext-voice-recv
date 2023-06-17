# -*- coding: utf-8 -*-

from .voice_client import *
from .reader import *
from .sinks import *
from .opus import *
from .rtp import *

from . import (
    rtp as rtp,
)

__title__ = 'discord.ext.voice_recv'
__author__ = 'Imayhaveborkedit'
__license__ = 'MIT'
__copyright__ = 'Copyright 2021-present Imayhaveborkedit'
__version__ = '0.1.6'


# TODO:
#       Design reader protocol
#       re-add all the client functions and state
#       figure out the 'state' of speakingstate
#       test
