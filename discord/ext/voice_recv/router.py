# -*- coding: utf-8 -*-

from __future__ import annotations

import queue
import logging
import threading

from collections import deque

from .utils import MultiDataEvent
from .opus import PacketDecoder

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Tuple, Dict, List, Callable, Any, Optional
    from .rtp import RTPPacket, RTCPPacket
    from .sinks import AudioSink
    from .reader import AudioReader

    EventCB = Callable[..., Any]
    EventData = Tuple[str, Tuple[Any, ...], Dict[str, Any]]

log = logging.getLogger(__name__)


class PacketRouter(threading.Thread):
    def __init__(self, sink: AudioSink, reader: AudioReader):
        super().__init__(daemon=True, name=f"packet-router-{id(self):x}")

        self.sink: AudioSink = sink
        self.decoders: Dict[int, PacketDecoder] = {}
        self.reader: AudioReader = reader
        self.waiter: MultiDataEvent[PacketDecoder] = MultiDataEvent()

        self._lock: threading.RLock = threading.RLock()
        self._end_thread: threading.Event = threading.Event()
        self._dropped_ssrcs: deque[int] = deque(maxlen=16)

    def feed_rtp(self, packet: RTPPacket) -> None:
        # TODO: stale packet check

        if packet.ssrc in self._dropped_ssrcs:
            log.debug("Ignoring packet from dropped ssrc %s", packet.ssrc)
            return

        with self._lock:
            decoder = self.get_decoder(packet.ssrc)
            if decoder is not None:
                decoder.push_packet(packet)

    def feed_rtcp(self, packet: RTCPPacket) -> None:
        guild = self.sink.voice_client.guild if self.sink.voice_client else None
        event_router = self.reader.event_router
        event_router.dispatch('rtcp_packet', packet, guild)

    def get_decoder(self, ssrc: int) -> Optional[PacketDecoder]:
        with self._lock:
            decoder = self.decoders.get(ssrc)
            if decoder is None:
                decoder = self.decoders[ssrc] = PacketDecoder(self, ssrc)

            return decoder

    def set_sink(self, sink: AudioSink) -> None:
        with self._lock:
            self.sink = sink

    def set_user_id(self, ssrc: int, user_id: int) -> None:
        with self._lock:
            if ssrc in self._dropped_ssrcs:
                self._dropped_ssrcs.remove(ssrc)

            decoder = self.decoders.get(ssrc)

            if decoder is not None:
                decoder.set_user_id(user_id)

    def destroy_decoder(self, ssrc: int) -> None:
        with self._lock:
            decoder = self.decoders.pop(ssrc, None)
            if decoder is not None:
                self._dropped_ssrcs.append(ssrc)
                decoder.destroy()

    def destroy_all_decoders(self) -> None:
        with self._lock:
            for ssrc in list(self.decoders.keys()):
                self.destroy_decoder(ssrc)

    def stop(self) -> None:
        self._end_thread.set()
        self.waiter.notify()

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as e:
            log.exception("Error in %s loop", self)
            self.reader.error = e
        finally:
            self.reader.voice_client.stop_listening()
            self.waiter.clear()

    def _do_run(self) -> None:
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    data = decoder.pop_data()
                    if data is not None:
                        self.sink.write(data.source, data)


class SinkEventRouter(threading.Thread):
    def __init__(self, sink: AudioSink, reader: AudioReader):
        super().__init__(daemon=True, name=f"sink-event-router-{id(self):x}")

        self.sink: AudioSink = sink
        self.reader: AudioReader = reader

        self._event_listeners: Dict[str, List[EventCB]] = {}
        self._buffer: queue.SimpleQueue[EventData] = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._end_thread: threading.Event = threading.Event()

        self.register_events()

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        log.debug("Dispatching voice_client event %s", event)
        self._buffer.put_nowait((event, args, kwargs))

    def set_sink(self, sink: AudioSink) -> None:
        with self._lock:
            self.unregister_events()
            self.sink = sink
            self.register_events()

    def register_events(self) -> None:
        with self._lock:
            self._register_listeners(self.sink)
            for child in self.sink.walk_children():
                self._register_listeners(child)

    def unregister_events(self) -> None:
        with self._lock:
            self._unregister_listeners(self.sink)
            for child in self.sink.walk_children():
                self._unregister_listeners(child)

    def _register_listeners(self, sink: AudioSink) -> None:
        log.debug("Registering events for %s: %s ", sink, sink.__sink_listeners__)

        for name, method_name in sink.__sink_listeners__:
            func = getattr(sink, method_name)

            log.debug("Registering event: %r, func: %r", name, method_name)
            if name in self._event_listeners:
                self._event_listeners[name].append(func)
            else:
                self._event_listeners[name] = [func]

    def _unregister_listeners(self, sink: AudioSink):
        for name, method_name in sink.__sink_listeners__:
            func = getattr(sink, method_name)

            if name in self._event_listeners:
                try:
                    self._event_listeners[name].remove(func)
                except ValueError:
                    pass

    def _dispatch_to_listeners(self, event: str, *args: Any, **kwargs: Any) -> None:
        for listener in self._event_listeners.get(f'on_{event}', []):
            try:
                listener(*args, **kwargs)
            except Exception:
                log.exception("Unhandled exception dispatching voice listener event %r", event)
                log.debug("event=%r, args=%r, kwargs=%r, listener=%r", event, args, kwargs, listener)

    def stop(self) -> None:
        self._end_thread.set()

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as e:
            log.exception("Error in %s", self.name)
            self.reader.error = e
            self.reader.voice_client.stop_listening()

    def _do_run(self) -> None:
        while not self._end_thread.is_set():
            try:
                event, args, kwargs = self._buffer.get(timeout=0.5)
            except queue.Empty:
                continue
            else:
                with self._lock:
                    # this looks dumb
                    with self.reader.packet_router._lock:
                        self._dispatch_to_listeners(event, *args, **kwargs)
