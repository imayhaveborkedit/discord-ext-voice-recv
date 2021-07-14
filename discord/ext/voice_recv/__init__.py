# -*- coding: utf-8 -*-



# from .gateway_monkeypatch import patch as _patch_gw
from .voice_client import VoiceRecvClient
from .reader import *
from .common import *

# def patch():
#     """Call this function after you import discord."""

#     _patch_gw()

# TODO:
#       Design reader protocol
#       re-add all the client functions and state
#       figure out the 'state' of speakingstate
#       test

__title__ = 'discord.ext.voice_recv'
__author__ = 'Imayhaveborkedit'
__license__ = 'MIT'
__copyright__ = 'Copyright 2021 Imayhaveborkedit'
__version__ = '0.0.1'
