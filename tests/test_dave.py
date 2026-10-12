"""Standalone checks for the DAVE (E2EE) receive path.  Run with:  python tests/test_dave.py"""
import importlib.util
import os
import struct
import sys
from types import SimpleNamespace

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _load_repo_voice_recv():
    """Load discord.ext.voice_recv from this checkout, ignoring any installed copy."""
    import discord.ext  # noqa: F401

    base = os.path.join(REPO, 'discord', 'ext', 'voice_recv')
    spec = importlib.util.spec_from_file_location(
        'discord.ext.voice_recv', os.path.join(base, '__init__.py'), submodule_search_locations=[base]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules['discord.ext.voice_recv'] = mod
    spec.loader.exec_module(mod)


_load_repo_voice_recv()

import discord
from discord import opus as dopus
from discord.ext.voice_recv import opus as vropus
from discord.ext.voice_recv.rtp import RTPPacket, FakePacket

USER_ID = 1234


def make_packet(seq: int, payload: bytes) -> RTPPacket:
    raw = struct.pack('>BBHII', 0x80, 0x78, seq, seq * 960, 42) + b'\x00'
    p = RTPPacket(raw)
    p.decrypted_data = payload
    return p


class FakeSession:
    """Mimics davey.DaveSession: 'E2EE' frames are b'DV' + opus."""

    def __init__(self, ready=True):
        self.ready = ready
        self.calls = 0

    def decrypt(self, user_id, media_type, data):
        self.calls += 1
        if user_id != USER_ID or not data.startswith(b'DV'):
            raise ValueError('Failed to decrypt: NoDecryptorForUser')
        return data[2:]


def make_decoder(session, version=1, wants_opus=False, user=USER_ID):
    conn = SimpleNamespace(dave_session=session, dave_protocol_version=version)
    vc = SimpleNamespace(
        _connection=conn,
        _get_id_from_ssrc=lambda ssrc: user,
        guild=SimpleNamespace(get_member=lambda i: None),
        client=SimpleNamespace(get_user=lambda i: None),
    )
    sink = SimpleNamespace(voice_client=vc, wants_opus=lambda: wants_opus)
    router = SimpleNamespace(sink=sink, waiter=SimpleNamespace(register=lambda d: None, unregister=lambda d: None))
    return vropus.PacketDecoder(router, 42), session


def real_opus_frame() -> bytes:
    enc = dopus.Encoder()
    return enc.encode(b'\x00\x01' * (dopus.Encoder.FRAME_SIZE // 2), dopus.Encoder.SAMPLES_PER_FRAME)


def main():
    print('testing', vropus.__file__)
    frame = real_opus_frame()
    failures = []

    def check(name, cond):
        print(('PASS ' if cond else 'FAIL ') + name)
        if not cond:
            failures.append(name)

    # 1. encrypted frame -> decrypted + decoded
    dec, _ = make_decoder(FakeSession())
    data = dec._process_packet(make_packet(1, b'DV' + frame))
    check('E2EE frame is decrypted and decoded', data is not None and len(data.pcm) == dopus.Encoder.FRAME_SIZE)

    # 2. un-decryptable frame is dropped, never reaches opus, never raises
    dec, _ = make_decoder(FakeSession())
    data = dec._process_packet(make_packet(2, b'garbage-not-dave'))
    check('undecryptable frame is dropped (returns None)', data is None)
    check('bookkeeping still advances after a drop', dec._last_seq == 2)

    # 3. session not ready yet -> drop (previously: corrupted stream)
    dec, _ = make_decoder(FakeSession(ready=False))
    check('frame dropped while DAVE session not ready', dec._process_packet(make_packet(3, b'DV' + frame)) is None)

    # 4. ssrc not mapped to a user yet -> drop
    dec, _ = make_decoder(FakeSession(), user=None)
    check('frame dropped while ssrc unmapped', dec._process_packet(make_packet(4, b'DV' + frame)) is None)

    # 5. protocol 0 (non-E2EE call) -> plain opus untouched
    dec, sess = make_decoder(FakeSession(), version=0)
    data = dec._process_packet(make_packet(5, frame))
    check('non-E2EE call decodes plain opus without touching davey', data is not None and sess.calls == 0 and len(data.pcm) > 0)

    # 6. silence frames are plaintext even in E2EE calls
    dec, sess = make_decoder(FakeSession())
    data = dec._process_packet(make_packet(6, b'\xf8\xff\xfe'))
    check('silence frame passes through in an E2EE call', data is not None and sess.calls == 0)

    # 7. garbage that slips through to opus must not raise
    dec, _ = make_decoder(FakeSession(), version=0)
    try:
        data = dec._process_packet(make_packet(7, b'\xff' * 7))
        check('corrupt opus data does not raise', data is not None and len(data.pcm) > 0)
    except Exception as e:  # noqa
        check('corrupt opus data does not raise (%r)' % e, False)

    # 8. FEC path: the *next* (still encrypted) packet must be decrypted before use, exactly once
    dec, sess = make_decoder(FakeSession())
    nxt = make_packet(10, b'DV' + frame)
    dec._buffer.push(nxt)
    pcm_pkt, pcm = dec._decode_packet(FakePacket(42, 9, 9 * 960))
    check('FEC decodes the decrypted next packet', len(pcm) == dopus.Encoder.FRAME_SIZE and nxt.decrypted_data == frame)
    calls_before = sess.calls
    data = dec._process_packet(nxt)
    check('next packet is not decrypted a second time', sess.calls == calls_before and data is not None and len(data.pcm) > 0)

    print()
    print('ALL OK' if not failures else 'FAILED: %s' % failures)
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
