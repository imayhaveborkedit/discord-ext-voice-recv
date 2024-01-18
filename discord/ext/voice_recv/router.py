# -*- coding: utf-8 -*-

from __future__ import annotations

import queue
import logging
import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Tuple, Dict, List, Callable, Any
    from .rtp import RTPPacket, RTCPPacket, AudioPacket
    from .sinks import AudioSink
    from .opus import PacketDecoder
    from .types import MemberOrUser as User

    EventCB = Callable[..., Any]
    EventData = Tuple[str, Tuple[Any, ...], Dict[str, Any]]

log = logging.getLogger(__name__)


class PacketRouter:
    def __init__(self, sink: AudioSink):
        self.sink: AudioSink = sink
        self.decoders: Dict[int, PacketDecoder] = {}

    def feed_rtp(self, packet: RTPPacket) -> None:
        ...

    def feed_rtcp(self, packet: RTCPPacket) -> None:
        ...

    def set_sink(self, sink: AudioSink) -> None:
        ...

    def set_user_id(self, ssrc: int, user_id: int) -> None:
        decoder = self.decoders.get(ssrc)

        if decoder is not None:
            decoder.set_user_id(user_id)

    def destroy_decoder(self, ssrc: int) -> None:
        ...

    def destroy_all_decoders(self) -> None:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


class SinkEventRouter(threading.Thread):
    def __init__(self, sink: AudioSink):
        super().__init__(daemon=True, name=f"sink-event-router-{id(self):x}")

        self.sink: AudioSink = sink
        self._event_listeners: Dict[str, List[EventCB]] = {}
        self._buffer: queue.SimpleQueue[EventData] = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._end_thread: threading.Event = threading.Event()

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
            listener(*args, **kwargs)

    def _do_run(self) -> None:
        while not self._end_thread.is_set():
            try:
                event, args, kwargs = self._buffer.get(timeout=0.5)
            except queue.Empty:
                continue
            else:
                with self._lock:
                    self._dispatch_to_listeners(event, *args, **kwargs)

    def start(self) -> None:
        try:
            self._do_run()
        except Exception:
            log.exception("Error in %s", self.name)

    def stop(self) -> None:
        self._end_thread.set()
