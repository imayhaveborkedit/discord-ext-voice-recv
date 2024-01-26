# -*- coding: utf-8 -*-

from __future__ import annotations

import time

from collections import defaultdict

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable

    TimeFunc = Callable[[], float]


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
