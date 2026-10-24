"""Worker (untrusted process): `python -m _testgate_worker <package>`.

Caller's view (the oracle's Link, through the wire):

    imp{name: "discount"}                  -> ret{v: mod{id: 1, name: "discount", pkg: true}}
    get{id: 1, name: "final_price"}        -> ret{v: o{id: 2, c: {...function...}}}
    call{id: 2, a: {"$": "l", "v": [{"$": "d", "v": "100.00"}]}, k: {"$": "m", "v": []}}
                                           -> ret{v: {"$": "d", "v": "90.00"}}

Imports nothing but the standard library, this runtime and, on request, the package named on
its command line or one of its submodules.

Serves until EOF on the request pipe, then exits 0. Package code that raises, BaseException
included, gets an err reply. A frame the worker cannot parse, an id it never issued, or a reply
over the wire caps is a ProtocolViolation that ends the process with a traceback on stderr; the
oracle records WORKER_LOST.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import os
import sys
import types
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import _testgate_wire as wire
else:
    import _testgate_wire as wire

_COPY_IGNORING_OVERRIDES: dict[type, Callable[[Any], Any]] = {
    int: int.__int__,
    float: float.__float__,
    str: str.__str__,
    bytes: lambda v: bytes.__getitem__(v, slice(None)),
    tuple: lambda v: tuple.__getitem__(v, slice(None)),
    list: lambda v: list.__getitem__(v, slice(None)),
    dict: lambda v: dict(dict.items(v)),
    set: lambda v: set(set.__iter__(v)),
    frozenset: lambda v: frozenset(frozenset.__iter__(v)),
    Decimal: Decimal,
}
_ANCHOR_VALUE = {anchor: _COPY_IGNORING_OVERRIDES[anchor] for anchor in wire.VALUE_ANCHORS}
_ANCHOR_NAMES = wire.anchor_names()
_EXC_ARGS = vars(BaseException)["args"]
_WRITEBACK_TYPES = (list, dict, set)


class Worker:
    def __init__(self, rfd: int, wfd: int, package: str) -> None:
        self.rfd = rfd
        self.wfd = wfd
        self.package = package
        self.held: dict[int, Any] = {}
        self.ref_by_id: dict[int, int] = {}
        self.ops: dict[str, Callable[[dict[str, Any]], tuple[Any, tuple[Any, ...]]]] = {
            "imp": self.imp,
            "get": self.get,
            "set": self.set,
            "call": self.call,
            "op": self.op,
            "fields": self.fields,
        }

    def serve(self) -> None:
        while True:
            try:
                msg = wire.recv(self.rfd, wire.REQUESTS)
            except EOFError:
                return
            wire.send(self.wfd, self.handle(msg))

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        n = msg["n"]
        try:
            value, writeback = self.ops[msg["op"]](msg)
            reply = {"op": "ret", "n": n, "v": self.encode(value)}
            if writeback:
                reply["wb"] = self.encode(writeback)
            return reply
        except wire.ProtocolViolation:
            raise
        except BaseException as exc:  # noqa: BLE001
            return {"op": "err", "n": n, "exc": self.encode(exc)}

    def imp(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        name = msg["name"]
        if name != self.package and not name.startswith(self.package + "."):
            raise ImportError(f"testgate worker serves only {self.package}")
        return importlib.import_module(name), ()

    def get(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        return getattr(self.target(msg), msg["name"]), ()

    def set(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        setattr(self.target(msg), msg["name"], self.decode(msg["v"]))
        return None, ()

    def call(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        target = self.target(msg)
        args, kwargs = self.decode(msg["a"]), self.decode(msg["k"])
        if type(args) is not list or type(kwargs) is not dict:
            raise wire.ProtocolViolation(wire.Violation.PROTOCOL, "call: a or k has a bad type")
        if any(type(k) is not str for k in kwargs):
            raise wire.ProtocolViolation(wire.Violation.PROTOCOL, "call: a keyword is not a str")
        value = target(*args, **kwargs)
        writeback = tuple((i, a) for i, a in enumerate(args) if type(a) in _WRITEBACK_TYPES)
        return value, writeback

    def op(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        target = self.target(msg)
        args = self.decode(msg["a"])
        if type(args) is not list:
            raise wire.ProtocolViolation(wire.Violation.PROTOCOL, "op: a is not a list")
        return getattr(type(target), msg["name"])(target, *args), ()

    def fields(self, msg: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
        target = self.target(msg)
        if isinstance(target, type) or not dataclasses.is_dataclass(target):
            raise TypeError("fields: not a dataclass instance")
        return _snapshot(target), ()

    def target(self, msg: dict[str, Any]) -> Any:
        if msg["id"] not in self.held:
            raise wire.ProtocolViolation(wire.Violation.PROTOCOL, "unknown handle id")
        return self.held[msg["id"]]

    def decode(self, data: Any) -> Any:
        return wire.decode(data, self.resolve)

    def resolve(self, handle: wire.Handle) -> Any:
        if type(handle) is not wire.Ref or handle.id not in self.held:
            raise wire.ProtocolViolation(wire.Violation.PROTOCOL, "not a handle this worker issued")
        return self.held[handle.id]

    def encode(self, value: Any) -> Any:
        return wire.encode(value, self.export)

    def ref(self, obj: Any) -> int:
        ref = self.ref_by_id.get(id(obj))
        if ref is None:
            ref = len(self.held) + 1
            self.held[ref] = obj
            self.ref_by_id[id(obj)] = ref
        return ref

    def export(self, value: Any, child: Callable[[Any], Any]) -> dict[str, Any]:
        t = type(value)
        if issubclass(t, types.ModuleType):
            pkg = hasattr(value, "__path__")
            return {"$": "mod", "id": self.ref(value), "name": str(value.__name__), "pkg": pkg}
        if issubclass(t, type):
            return {"$": "cls", "c": self.describe(value)}
        out: dict[str, Any] = {"$": "o", "id": self.ref(value), "c": self.describe(t)}
        if issubclass(t, BaseException):
            out["args"] = child(_EXC_ARGS.__get__(value))
            out["str"] = _text(value)
            return out
        anchor = next((a for a in t.__mro__ if a in _ANCHOR_VALUE), None)
        if anchor is not None:
            out["base"] = child(_ANCHOR_VALUE[anchor](value))
        elif _kind(t) == "enum":
            out["name"] = str(value._name_)
            out["value"] = child(value._value_)
        elif _kind(t) == "record" and _frozen(t):
            out["f"] = child(_snapshot(value))
        return out

    def describe(self, cls: type) -> dict[str, Any]:
        anchor = _ANCHOR_NAMES.get(cls)
        if anchor is not None:
            return {"anchor": anchor}
        return {
            "id": self.ref(cls),
            "name": cls.__name__,
            "qualname": cls.__qualname__,
            "module": str(cls.__module__),
            "bases": [self.describe(b) for b in cls.__bases__ if b is not object],
            "ops": [name for name in wire.FORWARDED_OPS if callable(_lookup(cls, name))],
            "kind": _kind(cls),
            "frozen": _frozen(cls),
        }


def _lookup(cls: type, name: str) -> Any:
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return klass.__dict__[name]
    return None


def _kind(cls: type) -> str:
    if isinstance(cls, enum.EnumMeta):
        return "enum"
    params = _lookup(cls, "__dataclass_params__")
    return "record" if params is not None and params.eq else "object"


def _frozen(cls: type) -> bool:
    params = _lookup(cls, "__dataclass_params__")
    return params is not None and bool(params.frozen)


def _snapshot(record: Any) -> tuple[tuple[str, Any], ...]:
    return tuple((f.name, getattr(record, f.name)) for f in dataclasses.fields(record) if f.compare)


def _text(exc: BaseException) -> str:
    try:
        return str(exc)
    except BaseException:  # noqa: BLE001
        return "<exception str() failed>"


def main() -> None:
    if len(sys.argv) != 2 or not all(p.isidentifier() for p in sys.argv[1].split(".")):
        raise SystemExit("usage: python -m _testgate_worker <package>")
    rfd, wfd = (int(fd) for fd in os.environ.pop(wire.ENV_RPC).split(","))
    Worker(rfd, wfd, sys.argv[1]).serve()


if __name__ == "__main__":
    main()
