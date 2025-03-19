# -*- coding: utf-8 -*-

from __future__ import annotations

import struct
import logging

from math import ceil
from collections import namedtuple

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional, Literal, Union, Final, Dict, Any, Tuple

    AudioPacket = Union['RTPPacket', 'FakePacket', 'SilencePacket']
    RealPacket = Union['RTPPacket', 'RTCPPacket']
    Packet = Union[RealPacket, 'FakePacket', 'SilencePacket']

    PacketTypes = Union[
        'SenderReportPacket',
        'ReceiverReportPacket',
        'SDESPacket',
        'BYEPacket',
        'APPPacket',
    ]

log = logging.getLogger(__name__)

__all__ = [
    'RTPPacket',
    'RTCPPacket',
    'FakePacket',
    'SilencePacket',
    'ExtensionID',
]

OPUS_SILENCE: Final = b'\xf8\xff\xfe'


class ExtensionID:
    audio_power: Final = 1
    speaking_state: Final = 9


def decode(data: bytes) -> RealPacket:
    """Creates an :class:`RTPPacket` or an :class:`RTCPPacket`.

    Parameters
    -----------
    data : bytes
        The raw packet data.
    """

    # While technically unreliable, discord RTP packets (should)
    # always be distinguishable from RTCP packets.  RTCP packets
    # should always have 200-204 as their second byte, while RTP
    # packet are (probably) always 73 (or at least not 200-204).

    # check version bits
    if not data[0] >> 6 == 2:
        raise ValueError(f'Invalid packet header 0b{data[0]:0>8b}')
    return _rtcp_map.get(data[1], RTPPacket)(data)


def decode_rtp(data: bytes) -> RTPPacket:
    return decode(data)  # type: ignore


def decode_rtcp(data: bytes) -> RTCPPacket:
    return decode(data)  # type: ignore


def is_rtcp(data: bytes) -> bool:
    return 200 <= data[1] <= 204


def _parse_low(x: int, bitlen: int = 32) -> float:
    return x / 2.0**bitlen


def _into_low(x: float, bitlen: int = 32) -> int:
    return int(x * 2.0**bitlen)


class _PacketCmpMixin:
    __slots__ = ('ssrc', 'timestamp')

    def __lt__(self, other: _PacketCmpMixin) -> bool:
        if self.ssrc != other.ssrc:
            raise TypeError("packet ssrc mismatch (%s, %s)" % (self.ssrc, other.ssrc))
        return self.timestamp < other.timestamp

    def __gt__(self, other: _PacketCmpMixin) -> bool:
        if self.ssrc != other.ssrc:
            raise TypeError("packet ssrc mismatch (%s, %s)" % (self.ssrc, other.ssrc))
        return self.timestamp > other.timestamp

    def __eq__(self, other: _PacketCmpMixin) -> bool:
        if self.ssrc != other.ssrc:
            return False
        return self.timestamp == other.timestamp

    def is_silence(self) -> bool:
        data = getattr(self, 'decrypted_data', None)
        return data == OPUS_SILENCE


class FakePacket(_PacketCmpMixin):
    __slots__ = ('ssrc', 'sequence', 'timestamp')
    decrypted_data: bytes = b''
    extension_data: dict = {}

    def __init__(self, ssrc: int, sequence: int, timestamp: int):
        self.ssrc: int = ssrc
        self.sequence: int = sequence
        self.timestamp: int = timestamp

    def __repr__(self) -> str:
        return '<FakePacket ssrc={0.ssrc}, sequence={0.sequence}, timestamp={0.timestamp}>'.format(self)

    def __bool__(self) -> Literal[False]:
        return False


class SilencePacket(_PacketCmpMixin):
    __slots__ = ('ssrc', 'timestamp')
    decrypted_data: Final = OPUS_SILENCE
    extension_data: Final[Dict[int, Any]] = {}
    sequence: int = -1

    def __init__(self, ssrc: int, timestamp: int):
        self.ssrc: int = ssrc
        self.timestamp: int = timestamp

    def __repr__(self) -> str:
        return '<SilencePacket ssrc={0.ssrc}, timestamp={0.timestamp}>'.format(self)

    def is_silence(self) -> bool:
        return True


class RTPPacket(_PacketCmpMixin):
    __slots__ = (
        'version',
        'padding',
        'extended',
        'cc',
        'marker',
        'payload',
        'sequence',
        'timestamp',
        'ssrc',
        'csrcs',
        'header',
        'data',
        'decrypted_data',
        'nonce',
        'extension',
        'extension_data',
        '_rtpsize',
    )

    _hstruct = struct.Struct('>xxHII')
    _ext_header = namedtuple("Extension", 'profile length values')
    _ext_magic = b'\xbe\xde'

    def __init__(self, data: bytes):
        data = bytearray(data)  # type: ignore

        # fmt: off
        self.version: int   =      data[0] >> 6
        self.padding: bool  = bool(data[0] & 0b00100000)
        self.extended: bool = bool(data[0] & 0b00010000)
        self.cc: int        =      data[0] & 0b00001111

        self.marker: bool   = bool(data[1] & 0b10000000)
        self.payload: int   =      data[1] & 0b01111111
        # fmt: on

        sequence, timestamp, ssrc = self._hstruct.unpack_from(data)
        self.sequence: int = sequence
        self.timestamp: int = timestamp
        self.ssrc: int = ssrc

        self.csrcs: Tuple[int, ...] = ()
        self.extension = None
        self.extension_data: Dict[int, bytes] = {}

        self.header = data[:12]
        self.data = data[12:]
        self.decrypted_data: Optional[bytes] = None

        self.nonce: bytes = b''
        self._rtpsize: bool = False

        if self.cc:
            fmt = '>%sI' % self.cc
            offset = struct.calcsize(fmt) + 12
            self.csrcs = struct.unpack(fmt, data[12:offset])
            self.data = data[offset:]

        # TODO?: impl padding calculations (though discord doesn't seem to use that bit)

    def adjust_rtpsize(self):
        """Adjusts the packet header and data based on the rtpsize format."""

        self._rtpsize = True
        self.nonce = self.data[-4:]

        if not self.extended:
            self.data = self.data[:-4]
            return

        # rtpsize based formats are laid out similarly to SRTP packets, which includes the ext header now
        # the nonce also needs to be removed from the end
        self.header += self.data[:4]
        self.data = self.data[4:-4]

    def update_ext_headers(self, data: bytes) -> int:
        """Adds extended header data to this packet, returns payload offset"""

        if not self.extended:
            return 0

        # rtpsize formats have the extension header in the rtp header instead of payload
        if self._rtpsize:
            data = self.header[-4:] + data

        # data is the decrypted packet payload containing the extension header and opus data
        profile, length = struct.unpack_from('>2sH', data)

        if profile == self._ext_magic:
            self._parse_bede_header(data, length)

        values = struct.unpack('>%sI' % length, data[4 : 4 + length * 4])
        self.extension = self._ext_header(profile, length, values)

        offset = 4 + length * 4
        if self._rtpsize:
            # remove the extra offset from adding the header in
            offset -= 4

        return offset

    # https://www.rfcreader.com/#rfc5285_line186
    def _parse_bede_header(self, data: bytes, length: int) -> None:
        offset = 4
        n = 0

        while n < length:
            next_byte = data[offset : offset + 1]

            if next_byte == b'\x00':
                offset += 1
                continue

            header = struct.unpack('>B', next_byte)[0]

            element_id = header >> 4
            element_len = 1 + (header & 0b0000_1111)

            self.extension_data[element_id] = data[offset + 1 : offset + 1 + element_len]
            offset += 1 + element_len
            n += 1

    def _dump_info(self) -> str:
        attrs = {name: getattr(self, name) for name in self.__slots__}
        return ''.join(("<RTPPacket ", *['{}={}, '.format(n, v) for n, v in attrs.items()], '>'))

    def __repr__(self) -> str:
        return (
            '<RTPPacket '
            'ssrc={0.ssrc}, '
            'sequence={0.sequence}, '
            'timestamp={0.timestamp}, '
            'size={1}, '
            'ext={2}'
            '>'.format(self, len(self.data), set(self.extension_data))
        )


# http://www.rfcreader.com/#rfc3550_line855
class RTCPPacket:
    __slots__ = ('version', 'padding', 'length')
    _header = struct.Struct('>BBH')
    _ssrc_fmt = struct.Struct('>I')
    type = None

    def __init__(self, data: bytes):
        self.length: int
        head, _, self.length = self._header.unpack_from(data)
        self.version: int = head >> 6
        self.padding: bool = bool(head & 0b00100000)
        # dubious, yet devious
        setattr(self, self.__slots__[0], head & 0b00011111)

    def __repr__(self) -> str:
        content = ', '.join("{}: {}".format(k, getattr(self, k, None)) for k in self.__slots__)
        return "<{} {}>".format(self.__class__.__name__, content)

    @classmethod
    def from_data(cls, data: bytes) -> PacketTypes:
        _, ptype, _ = cls._header.unpack_from(data)
        return _rtcp_map[ptype](data)


# TODO?: consider moving repeated code to a ReportPacket type
# http://www.rfcreader.com/#rfc3550_line1614
class SenderReportPacket(RTCPPacket):
    __slots__ = ('report_count', 'ssrc', 'info', 'reports', 'extension')
    _info_fmt = struct.Struct('>5I')
    _report_fmt = struct.Struct('>IB3x4I')
    _24bit_int_fmt = struct.Struct('>4xI')
    _info = namedtuple('RRSenderInfo', 'ntp_ts rtp_ts packet_count octet_count')
    _report = namedtuple("RReport", 'ssrc perc_loss total_lost last_seq jitter lsr dlsr')
    type = 200

    def __init__(self, data):
        super().__init__(data)
        self.ssrc = self._ssrc_fmt.unpack_from(data, 4)[0]
        self.info = self._read_sender_info(data, 8)

        reports = []
        for x in range(self.report_count):
            offset = 28 + 24 * x
            reports.append(self._read_report(data, offset))

        self.reports = tuple(reports)

        self.extension = None
        if len(data) > 28 + 24 * self.report_count:
            self.extension = data[28 + 24 * self.report_count :]

    def _read_sender_info(self, data, offset):
        nhigh, nlow, rtp_ts, pcount, ocount = self._info_fmt.unpack_from(data, offset)
        ntotal = nhigh + _parse_low(nlow)
        return self._info(ntotal, rtp_ts, pcount, ocount)

    def _read_report(self, data, offset):
        ssrc, flost, seq, jit, lsr, dlsr = self._report_fmt.unpack_from(data, offset)
        clost = self._24bit_int_fmt.unpack_from(data, offset)[0] & 0xFFFFFF
        return self._report(ssrc, flost, clost, seq, jit, lsr, dlsr)


# http://www.rfcreader.com/#rfc3550_line1879
class ReceiverReportPacket(RTCPPacket):
    __slots__ = ('report_count', 'ssrc', 'reports', 'extension')
    _report_fmt = struct.Struct('>IB3x4I')
    _24bit_int_fmt = struct.Struct('>4xI')
    _report = namedtuple("RReport", 'ssrc perc_loss total_lost last_seq jitter lsr dlsr')
    type = 201

    reports: Tuple[_report, ...]

    def __init__(self, data: bytes):
        super().__init__(data)
        self.ssrc: int = self._ssrc_fmt.unpack_from(data, 4)[0]

        reports = []
        for x in range(self.report_count):
            offset = 8 + 24 * x
            reports.append(self._read_report(data, offset))

        self.reports = tuple(reports)

        self.extension: Optional[bytes] = None
        if len(data) > 8 + 24 * self.report_count:
            self.extension = data[8 + 24 * self.report_count :]

    def _read_report(self, data: bytes, offset: int) -> _report:
        ssrc, flost, seq, jit, lsr, dlsr = self._report_fmt.unpack_from(data, offset)
        clost = self._24bit_int_fmt.unpack_from(data, offset)[0] & 0xFFFFFF
        return self._report(ssrc, flost, clost, seq, jit, lsr, dlsr)


# UNFORTUNATELY it seems discord only uses the above ~~two packet types~~ packet type.
# Good thing I knew that when I made the rest of these. Haha yes.


# http://www.rfcreader.com/#rfc3550_line2024
class SDESPacket(RTCPPacket):
    __slots__ = ('source_count', 'chunks', '_pos')
    _item_header = struct.Struct('>BB')
    _chunk = namedtuple("SDESChunk", 'ssrc items')
    _item = namedtuple("SDESItem", 'type size length text')
    type = 202

    def __init__(self, data):
        super().__init__(data)
        _chunks = []
        self._pos = 4

        for _ in range(self.source_count):
            _chunks.append(self._read_chunk(data))

        self.chunks = tuple(_chunks)

    def _read_chunk(self, data):
        ssrc = self._ssrc_fmt.unpack_from(data, self._pos)[0]
        self._pos += 4

        # check for chunk with no items
        if data[self._pos : self._pos + 4] == b'\x00\x00\x00\x00':
            self._pos += 4
            return self._chunk(ssrc, ())

        items = [self._read_item(data)]

        # Read items until END type is found
        while items[-1].type != 0:
            items.append(self._read_item(data))

        # pad chunk to 4 bytes
        if self._pos % 4:
            self._pos = ceil(self._pos / 4) * 4

        return self._chunk(ssrc, items)

    def _read_item(self, data):
        itype, ilen = self._item_header.unpack_from(data, self._pos)
        self._pos += 2
        text = None

        if ilen:
            text = data[self._pos : self._pos + ilen].decode()
            self._pos += ilen

        return self._item(itype, ilen + 2, ilen, text)

    def _get_chunk_size(self, chunk):
        return 4 + max(4, sum(i.size for i in chunk.items))  # + padding?


# http://www.rfcreader.com/#rfc3550_line2311
class BYEPacket(RTCPPacket):
    __slots__ = ('source_count', 'ssrcs', 'reason')
    type = 203

    def __init__(self, data):
        super().__init__(data)
        self.ssrcs = struct.unpack_from('>%sI' % self.source_count, data, 4)
        self.reason = None

        body_length = 4 + len(self.ssrcs) * 4
        if len(data) > body_length:
            extra_len = struct.unpack_from('B', data, body_length)[0]
            reason = struct.unpack_from('%ss' % extra_len, data, body_length + 1)
            self.reason = reason.decode()


# http://www.rfcreader.com/#rfc3550_line2353
class APPPacket(RTCPPacket):
    __slots__ = ('subtype', 'ssrc', 'name', 'data')
    _packet_info = struct.Struct('>I4s')
    type = 204

    def __init__(self, data):
        super().__init__(data)
        self.ssrc, name = self._packet_info.unpack_from(data, 4)
        self.name = name.decode('ascii')
        self.data = data[12:]  # should be a multiple of 32 bits but idc


_rtcp_map = {
    200: SenderReportPacket,
    201: ReceiverReportPacket,
    202: SDESPacket,
    203: BYEPacket,
    204: APPPacket,
}
