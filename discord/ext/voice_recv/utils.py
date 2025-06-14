# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import threading

from collections import defaultdict

from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from typing import Callable, Sequence

    TimeFunc = Callable[[], float]

_dataT = TypeVar("_dataT")


def gap_wrapped(a: int, b: int, *, wrap: int = 65536) -> int:
    """
    Returns the gap between two numbers, acounting for unsigned integer wraparound.
    """
    return (b - (a + 1) + wrap) % wrap


def add_wrapped(a: int, b: int, *, wrap: int = 65536) -> int:
    """
    Returns the sum of two numbers, accounting for unsigned integer wraparound.
    """
    return (a + b) % wrap


# May not even be needed if i dont use the dict subclasses
class Bidict(dict):
    """A bi-directional dict"""

    _None = object()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        super().update({v: k for k, v in self.items()})

    def __setitem__(self, key, value):
        # Delete related mappings
        # if we have 1 <-> 2 and we set 2 <-> 3, 2 is now unrelated to 1

        if key in self:
            del self[key]
        if value in self:
            del self[value]

        super().__setitem__(key, value)
        super().__setitem__(value, key)

    def __delitem__(self, key):
        value = super().__getitem__(key)
        super().__delitem__(value)

        if key == value:
            return

        super().__delitem__(key)

    def to_dict(self):
        return super().copy()

    def pop(self, k, d=_None):
        try:
            v = super().pop(k)
            super().pop(v, d)
            return v
        except KeyError:
            if d is not self._None:
                return d
            raise

    def popitem(self):
        item = super().popitem()
        super().__delitem__(item[1])
        return item

    def setdefault(self, k, d=None):
        try:
            return self[k]
        except KeyError:
            if d in self:
                return d

        self[k] = d
        return d

    def update(self, *args, **F):
        try:
            E = args[0]
            if callable(getattr(E, 'keys', None)):
                for k in E:
                    self[k] = E[k]
            else:
                for k, v in E:
                    self[k] = v
        except IndexError:
            pass
        finally:
            for k in F:
                self[k] = F[k]

    def copy(self):
        return self.__class__(super().copy())

    # incompatible
    # https://docs.python.org/3/library/exceptions.html#NotImplementedError, Note 1
    fromkeys = None  # type: ignore


class Defaultdict(defaultdict):
    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError((key,))

        self[key] = value = self.default_factory(key)  # type: ignore
        return value


class LoopTimer:
    def __init__(self, delay: float, *, timefunc: TimeFunc = time.perf_counter):
        self._delay: float = delay
        self._time: TimeFunc = timefunc
        self._start: float = 0
        self._loops: int = 0

    @property
    def delay(self) -> float:
        return self._delay

    @property
    def loops(self) -> int:
        return self._loops

    @property
    def start_time(self) -> float:
        return self._start

    @property
    def remaining_time(self) -> float:
        next_time = self._start + self._delay * self._loops
        return self._delay + (next_time - self._time())

    def start(self) -> None:
        self._loops = 0
        self._start = self._time()

    def mark(self) -> None:
        self._loops += 1

    def sleep(self) -> None:
        time.sleep(max(0, self.remaining_time))


class MultiDataEvent(Generic[_dataT]):
    """
    Something like the inverse of a Condition.  A 1-waiting-on-N type of object,
    with accompanying data object for convenience.
    """

    def __init__(self):
        self._items: list[_dataT] = []
        self._ready: threading.Event = threading.Event()

    @property
    def items(self) -> list[_dataT]:
        """A shallow copy of the currently ready objects."""
        return self._items.copy()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def _check_ready(self) -> None:
        if self._items:
            self._ready.set()
        else:
            self._ready.clear()

    def notify(self) -> None:
        self._ready.set()
        self._check_ready()

    def wait(self, timeout: float | None = None) -> bool:
        self._check_ready()
        return self._ready.wait(timeout)

    def register(self, item: _dataT) -> None:
        self._items.append(item)
        self._ready.set()

    def unregister(self, item: _dataT) -> None:
        try:
            self._items.remove(item)
        except ValueError:
            pass
        self._check_ready()

    def clear(self) -> None:
        self._items.clear()
        self._ready.clear()
