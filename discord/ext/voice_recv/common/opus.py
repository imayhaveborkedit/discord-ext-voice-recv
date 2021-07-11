# -*- coding: utf-8 -*-

import threading

from .rtp import *

from discord.opus import Decoder

class BufferedDecoder(threading.Thread):
    DELAY = Decoder.FRAME_LENGTH / 1000.0

    def __init__(self, ssrc, output_func, *, buffer=200):
        super().__init__(daemon=True, name='ssrc-%s' % ssrc)

        if buffer < 40: # technically 20 works but then FEC is useless
            raise ValueError("buffer size of %s is invalid; cannot be lower than 40" % buffer)

        self.ssrc = ssrc
        self.output_func = output_func

        self._decoder = Decoder()
        self._buffer = []
        self._last_seq = 0
        self._last_ts = 0
        self._loops = 0

        # Optional diagnostic state stuff
        self._overflow_mult = self._overflow_base = 2.0
        self._overflow_incr = 0.5

        # minimum (lower bound) size of the jitter buffer (n * 20ms per packet)
        self.buffer_size = buffer // self._decoder.FRAME_LENGTH

        self._finalizing = False
        self._end_thread = threading.Event()
        self._end_main_loop = threading.Event()
        self._primed = threading.Event()
        self._lock = threading.RLock()

        # TODO: Add RTCP queue
        self._rtcp_buffer = []

        self.start()

    def feed_rtp(self, packet):
        if self._last_ts < packet.timestamp:
            self._push(packet)
        elif self._end_thread.is_set():
            return

    def feed_rtcp(self, packet):
        ... # TODO: rotating buffer of Nones or something
        #           or I can store (last_seq + buffer_size, packet)
        # print(f"[router:feed] Got rtcp packet {packet}")
        # print(f"[router:feed] Other timestamps: {[p.timestamp for p in self._buffer]}")
        # print(f"[router:feed] Other timestamps: {self._buffer}")

    def truncate(self, *, size=None):
        """Discards old data to shrink buffer back down to ``size`` (default: buffer_size).
        TODO: doc
        """

        size = self.buffer_size if size is None else size
        with self._lock:
            self._buffer = self._buffer[-size:]

    def stop(self, **kwargs):
        """
        drain=True: continue to write out the remainder of the buffer at the standard rate
        flush=False: write the remainder of the buffer with no delay
        TODO: doc
        """

        with self._lock:
            self._end_thread.set()
            self._end_main_loop.set()

            if any(isinstance(p, RTPPacket) for p in self._buffer) or True:
                if kwargs.pop('flush', False):
                    self._finalizing = True
                    self.DELAY = 0
                elif not kwargs.pop('drain', True):
                    with self._lock:
                        self._finalizing = True
                        self._buffer.clear()

    def reset(self):
        with self._lock:
            self._decoder = Decoder() # TODO: Add a reset function to Decoder itself
            self._last_seq = self._last_ts = 0
            self._buffer.clear()
            self._primed.clear()
            self._end_main_loop.set() # XXX: racy with _push?
            self.DELAY = self.__class__.DELAY

    def _push(self, item):
        if not isinstance(item, (RTPPacket, SilencePacket)):
            raise TypeError(f"item should be an RTPPacket, not {item.__class__.__name__}")

        # XXX: racy with reset?
        if self._end_main_loop.is_set() and not self._end_thread.is_set():
            self._end_main_loop.clear()

        if not self._primed.is_set():
            self._primed.set()

        # Fake packet loss
        # import random
        # if random.randint(1, 100) <= 10 and isinstance(item, RTPPacket):
        #     return

        with self._lock:
            existing_packet = utils.get(self._buffer, timestamp=item.timestamp)
            if isinstance(existing_packet, SilencePacket):
                # Replace silence packets with rtp packets
                self._buffer[self._buffer.index(existing_packet)] = item
                return
            elif isinstance(existing_packet, RTPPacket):
                return # duplicate packet

            bisect.insort(self._buffer, item)

        # Optional diagnostics, will probably remove later
            bufsize = len(self._buffer) # indent intentional
        if bufsize >= self.buffer_size * self._overflow_mult:
            print(f"[router:push] Warning: rtp heap size has grown to {bufsize}")
            self._overflow_mult += self._overflow_incr

        elif bufsize <= self.buffer_size * (self._overflow_mult - self._overflow_incr) \
            and self._overflow_mult > self._overflow_base:

            print(f"[router:push] Info: rtp heap size has shrunk to {bufsize}")
            self._overflow_mult = max(self._overflow_base, self._overflow_mult - self._overflow_incr)

    def _pop(self):
        packet = nextpacket = None
        with self._lock:
            try:
                if not self._finalizing:
                    self._buffer.append(SilencePacket(self.ssrc, self._buffer[-1].timestamp + Decoder.SAMPLES_PER_FRAME))
                packet = self._buffer.pop(0)
                nextpacket = self._buffer[0]
            except IndexError:
                pass # empty buffer

        return packet, nextpacket

    def _initial_fill(self):
        """Artisanal hand-crafted function for buffering packets and clearing discord's stupid fucking rtp buffer."""

        if self._end_main_loop.is_set():
            return

        # Very small sleep to check if there's buffered packets
        time.sleep(0.001)
        if len(self._buffer) > 3:
            # looks like there's some old packets in the buffer
            # we need to figure out where the old packets stop and where the fresh ones begin
            # for that we need to see when we return to the normal packet accumulation rate

            last_size = len(self._buffer)

            # wait until we have the correct rate of packet ingress
            while len(self._buffer) - last_size > 1:
                last_size = len(self._buffer)
                time.sleep(0.001)

            # collect some fresh packets
            time.sleep(0.06)

            # generate list of differences between packet sequences
            with self._lock:
                diffs = [self._buffer[i+1].sequence-self._buffer[i].sequence for i in range(len(self._buffer)-1)]
            sdiffs = sorted(diffs, reverse=True)

            # decide if there's a jump
            jump1, jump2 = sdiffs[:2]
            if jump1 > jump2 * 3:
                # remove the stale packets and keep the fresh ones
                self.truncate(size=len(self._buffer[diffs.index(jump1)+1:]))
            else:
                # otherwise they're all stale, dump 'em (does this ever happen?)
                with self._lock:
                    self._buffer.clear()

        # fill buffer to at least half full
        while len(self._buffer) < self.buffer_size // 2:
            time.sleep(0.001)

        # fill the buffer with silence aligned with the first packet
        # if an rtp packet already exists for the given silence packet ts, the silence packet is ignored
        with self._lock:
            start_ts = self._buffer[0].timestamp
            for x in range(1, 1 + self.buffer_size - len(self._buffer)):
                self._push(SilencePacket(self.ssrc, start_ts + x * Decoder.SAMPLES_PER_FRAME))

        # now fill the rest
        while len(self._buffer) < self.buffer_size:
            time.sleep(0.001)
            # TODO: Maybe only wait at most for about as long we we're supposed to?
            #       0.02 * (buffersize - len(buffer))

    def _packet_gen(self):
        while True:
            packet, nextpacket = self._pop()
            self._last_ts = getattr(packet, 'timestamp', self._last_ts + Decoder.SAMPLES_PER_FRAME)
            self._last_seq += 1 # self._last_seq = packet.sequence?

            if isinstance(packet, RTPPacket):
                pcm = self._decoder.decode(packet.decrypted_data)

            elif isinstance(nextpacket, RTPPacket):
                pcm = self._decoder.decode(packet.decrypted_data, fec=True)
                fec_packet = FECPacket(self.ssrc, nextpacket.sequence - 1, nextpacket.timestamp - Decoder.SAMPLES_PER_FRAME)
                yield fec_packet, pcm

                packet, _ = self._pop()
                self._last_ts += Decoder.SAMPLES_PER_FRAME
                self._last_seq += 1

                pcm = self._decoder.decode(packet.decrypted_data)

            elif packet is None:
                self._finalizing = False
                break
            else:
                pcm = self._decoder.decode(None)

            yield packet, pcm

    def _do_run(self):
        self._primed.wait()
        self._initial_fill()

        self._loops = 0
        packet_gen = self._packet_gen()
        start_time = time.perf_counter()
        try:
            while not self._end_main_loop.is_set() or self._finalizing:
                packet, pcm = next(packet_gen)
                try:
                    self.output_func(pcm, packet.decrypted_data, packet)
                except:
                    log.exception("Sink raised exception")
                    traceback.print_exc()

                next_time = start_time + self.DELAY * self._loops
                self._loops += 1

                time.sleep(max(0, self.DELAY + (next_time - time.perf_counter())))
        except StopIteration:
            time.sleep(0.001) # just in case, so we don't slam the cpu
        finally:
            packet_gen.close()

    def run(self):
        try:
            while not self._end_thread.is_set():
                self._do_run()
        except Exception as e:
            log.exception("Error in decoder %s", self.name)
            traceback.print_exc()


class BasePacketDecoder:
    DELAY = Decoder.FRAME_LENGTH / 1000.0

    def feed_rtp(self, packet):
        raise NotImplementedError
    def feed_rtcp(self, packet):
        raise NotImplementedError
    def truncate(self, *, size=None):
        raise NotImplementedError
    def reset(self):
        raise NotImplementedError

class BufferedPacketDecoder(BasePacketDecoder):
    """Buffers and decodes packets from a single ssrc"""

    def __init__(self, ssrc, *, buffer=200):
        if buffer < 40: # technically 20 works but then FEC is useless
            raise ValueError("buffer size of %s is invalid; cannot be lower than 40" % buffer)

        self.ssrc = ssrc
        self._decoder = Decoder()
        self._buffer = []
        self._rtcp_buffer = {} # TODO: Add RTCP queue
        self._last_seq = self._last_ts = 0

        # Optional diagnostic state stuff
        self._overflow_mult = self._overflow_base = 2.0
        self._overflow_incr = 0.5

        # minimum (lower bound) size of the jitter buffer (n * 20ms per packet)
        self.buffer_size = buffer // self._decoder.FRAME_LENGTH
        self._lock = threading.RLock()

        self._gen = None

    def __iter__(self):
        if self._gen is None:
            self._gen = self._packet_gen()
        return self._gen

    def __next__(self):
        return next(iter(self))

    def feed_rtp(self, packet):
        if self._last_ts < packet.timestamp:
            self._push(packet)

    def feed_rtcp(self, packet):
        with self._lock:
            if not self._buffer:
                return # ignore for now, handle properly later
            self._rtcp_buffer[self._buffer[-1]] = packet

    def truncate(self, *, size=None):
        size = self.buffer_size if size is None else size
        with self._lock:
            self._buffer = self._buffer[-size:]

    def reset(self):
        with self._lock:
            self._decoder = Decoder() # TODO: Add a reset function to Decoder itself
            self.DELAY = self.__class__.DELAY
            self._last_seq = self._last_ts = 0
            self._buffer.clear()
            self._rtcp_buffer.clear()
            self._gen.close()
            self._gen = None

    def _push(self, item):
        if not isinstance(item, (RTPPacket, SilencePacket)):
            raise TypeError(f"item should be an RTPPacket, not {item.__class__.__name__}")

        # Fake packet loss
        # import random
        # if random.randint(1, 100) <= 10 and isinstance(item, RTPPacket):
        #     return

        with self._lock:
            existing_packet = utils.get(self._buffer, timestamp=item.timestamp)
            if isinstance(existing_packet, SilencePacket):
                # Replace silence packets with rtp packets
                self._buffer[self._buffer.index(existing_packet)] = item
                return
            elif isinstance(existing_packet, RTPPacket):
                return # duplicate packet

            bisect.insort(self._buffer, item)

        # Optional diagnostics, will probably remove later
            bufsize = len(self._buffer) # indent intentional
        if bufsize >= self.buffer_size * self._overflow_mult:
            print(f"[router:push] Warning: rtp heap size has grown to {bufsize}")
            self._overflow_mult += self._overflow_incr

        elif bufsize <= self.buffer_size * (self._overflow_mult - self._overflow_incr) \
            and self._overflow_mult > self._overflow_base:

            print(f"[router:push] Info: rtp heap size has shrunk to {bufsize}")
            self._overflow_mult = max(self._overflow_base, self._overflow_mult - self._overflow_incr)

    def _pop(self):
        packet = nextpacket = None
        with self._lock:
            try:
                self._buffer.append(SilencePacket(self.ssrc, self._buffer[-1].timestamp + Decoder.SAMPLES_PER_FRAME))
                packet = self._buffer.pop(0)
                nextpacket = self._buffer[0]
            except IndexError:
                pass # empty buffer

        return packet, nextpacket # return rtcp packets as well?

    def _packet_gen(self):
        # Buffer packets
        # do I care about dumping buffered packets on reset?

        # Ok yes this section is going to look weird.  To keep everything consistant I need to
        # wait for a specific number of iterations instead of on the actual buffer size.  These
        # objects are supposed to be time naive.  The class handling these is responsible for
        # keeping the time synchronization.

        # How many packets we already have
        pre_fill = len(self._buffer)
        # How many packets we need to get to half full
        half_fill = max(0, self.buffer_size//2 - 1 - pre_fill)
        # How many packets we need to get to full
        full_fill = self.buffer_size - half_fill

        print(f"Starting with {pre_fill}, collecting {half_fill}, then {full_fill}")

        while not self._buffer:
            yield None, None

        for x in range(half_fill-1):
            yield None, None

        with self._lock:
            start_ts = self._buffer[0].timestamp
            for x in range(1, 1 + self.buffer_size - len(self._buffer)):
                self._push(SilencePacket(self.ssrc, start_ts + x * Decoder.SAMPLES_PER_FRAME))

        for x in range(full_fill):
            yield None, None

        while True:
            packet, nextpacket = self._pop()
            self._last_ts = getattr(packet, 'timestamp', self._last_ts + Decoder.SAMPLES_PER_FRAME)
            self._last_seq += 1 # self._last_seq = packet.sequence?

            if isinstance(packet, RTPPacket):
                pcm = self._decoder.decode(packet.decrypted_data)

            elif isinstance(nextpacket, RTPPacket):
                pcm = self._decoder.decode(packet.decrypted_data, fec=True)
                fec_packet = FECPacket(self.ssrc, nextpacket.sequence - 1, nextpacket.timestamp - Decoder.SAMPLES_PER_FRAME)
                yield fec_packet, pcm

                packet, _ = self._pop()
                self._last_ts += Decoder.SAMPLES_PER_FRAME
                self._last_seq += 1

                pcm = self._decoder.decode(packet.decrypted_data)

            elif packet is None:
                break
            else:
                pcm = self._decoder.decode(None)

            yield packet, pcm


class BufferedDecoder2(threading.Thread):
    """Ingests rtp packets and dispatches to decoders and sink output function."""

    def __init__(self, reader, *, decodercls=BufferedPacketDecoder):
        super().__init__(daemon=True, name='DecoderBuffer')
        self.reader = reader
        self.decodercls = decodercls

        self.output_func = reader._write_to_sink
        self.decoders = {}
        self.initial_buffer = []
        self.queue = deque()

        self._end_thread = threading.Event()
        self._has_decoder = threading.Event()
        self._lock = threading.Lock()

    def _get_decoder(self, ssrc):
        dec = self.decoders.get(ssrc)

        if not dec and self.reader.client._get_ssrc_mapping(ssrc=ssrc)[1]: # and get_user(ssrc)?
            dec = self.decoders[ssrc] = self.decodercls(ssrc)
            dec.start_time = time.perf_counter() # :thinking:
            dec.loops = 0                        # :thinking::thinking::thinking:
            self.queue.append((dec.start_time, dec))
            self._has_decoder.set()

        return dec

    def _feed_rtp_initial(self, packet):
        with self._lock:
            self.initial_buffer.append(packet)

    def feed_rtp(self, packet):
        dec = self._get_decoder(packet.ssrc)
        if dec:
            return dec.feed_rtp(packet)

    def feed_rtcp(self, packet):
        # RTCP packets themselves don't really belong to a decoder
        # I could split the reports up or send to all idk its weird

        dec = self._get_decoder(packet.ssrc)
        if dec:
            print(f"RTCP packet: {packet}")
            return dec.feed_rtcp(packet)

    def drop_ssrc(self, ssrc):
        dec = self.decoders.pop(ssrc, None)
        if dec:
            # dec/self.flush()?
            dec.reset()

            if not self.decoders:
                self._has_decoder.clear()

    def reset(self, *ssrcs):
        with self._lock:
            if not ssrcs:
                ssrcs = tuple(self.decoders.keys())

            for ssrc in ssrcs:
                dec = self.decoders.get(ssrc)
                if dec:
                    dec.reset()

    def flush(self, *ssrcs):
        ...
        # The new idea is to call a special flush event function on the sink with the
        # rest of the audio buffer when exiting so the user can use or ignore it

    def stop(self, **kwargs):
        for decoder in tuple(self.decoders.values()):
            # decoder.stop(**kwargs)
            decoder.reset()

    def _initial_fill(self):
        # Fill a single buffer first then dispense into the actual buffers
        try:
            normal_feed_rtp = self.feed_rtp
            self.feed_rtp = self._feed_rtp_initial

            buff = self.initial_buffer

            # Very small sleep to check if there's buffered packets
            time.sleep(0.002)
            if len(buff) > 3:
                # looks like there's some old packets in the buffer
                # we need to figure out where the old packets stop and where the fresh ones begin
                # for that we need to see when we return to the normal packet accumulation rate

                last_size = len(buff)

                # wait until we have the correct rate of packet ingress
                while len(buff) - last_size > 1:
                    last_size = len(buff)
                    time.sleep(0.001)

                # collect some fresh packets
                time.sleep(0.06)

                # generate list of differences between packet sequences
                with self._lock:
                    diffs = [buff[i+1].sequence - buff[i].sequence for i in range(len(buff)-1)]
                sdiffs = sorted(diffs, reverse=True)

                # decide if there's a jump
                jump1, jump2 = sdiffs[:2]
                if jump1 > jump2 * 3:
                    # remove the stale packets and keep the fresh ones
                    with self._lock:
                        size = len(buff[diffs.index(jump1)+1:])
                        buff = buff[-size:]
                else:
                    # otherwise they're all stale, dump 'em (does this ever happen?)
                    with self._lock:
                        buff.clear()

            # The old version of this code backfilled buffers based on the buffer size.
            # We dont have that here but we can just have the individual buffer objects
            # backfill themselves.

            # Dump initial buffer into actual buffers
            with self._lock:
                for packet in buff:
                    normal_feed_rtp(packet)

                self.feed_rtp = normal_feed_rtp
        finally:
            self.feed_rtp = normal_feed_rtp

    def decode(self, decoder):
        data = next(decoder)
        if any(data):
            packet, pcm = data
            try:
                self.output_func(pcm, packet.decrypted_data, packet)
            except:
                log.exception("Sink raised exception")
                traceback.print_exc()

        decoder.loops += 1
        decoder.next_time = decoder.start_time + decoder.DELAY * decoder.loops
        self.queue.append((decoder.next_time, decoder))

    def _do_run(self):
        while not self._end_thread.is_set():
            self._has_decoder.wait()

            next_time, decoder = self.queue.popleft()
            remaining = next_time - time.perf_counter()

            if remaining >= 0:
                insort(self.queue, (next_time, decoder))
                time.sleep(max(0.002, remaining/2)) # sleep accuracy tm
                continue

            self.decode(decoder)

    def run(self):
        try:
            self._do_run()
        except Exception as e:
            log.exception("Error in decoder %s", self.name)
            traceback.print_exc()



# basically, separate everything
# the jitter buffer is ONLY for jitter, not an actual buffer
# all it does is buffer when out of order packets are received
# if you want smoother packet flow add another buffer
