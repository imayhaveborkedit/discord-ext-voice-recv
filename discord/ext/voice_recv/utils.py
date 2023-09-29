# -*- coding: utf-8 -*-

# May not even be needed if i dont use the dict subclasses


from collections import defaultdict


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
