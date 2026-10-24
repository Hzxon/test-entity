"""Wire between the oracle (trusted) and the worker (untrusted). Imported by both sides.

Caller's view (the oracle; the worker mirrors it with REQUESTS and its own policies):

    wire.send(fd, {"op": "call", "n": 7, "id": 3,
                   "a": wire.encode([Decimal("100")], export), "k": wire.encode({}, export)})
    reply = wire.recv(fd, wire.REPLIES)    # checked envelope, or ProtocolViolation / EOFError
    value = wire.decode(reply["v"], resolve)   # resolve receives a parsed Handle

Frames: 4-byte big-endian length + UTF-8 JSON object, at most MAX_FRAME bytes. JSON only:
nothing here unpickles, evals, imports, or looks anything up by a received name.

Messages. "n" is a sequence id the worker echoes. It detects desync only. The worker
controls its replies, so it would echo a nonce too: there is none.

    oracle -> worker   imp{name}  get{id,name}  set{id,name,v}  call{id,a,k}  op{id,name,a}
                       fields{id}
    worker -> oracle   ret{v, wb?}  err{exc}

A frame from the worker is only ever a reply; a request shape there is a protocol violation.

Values. The "$" tag; bare JSON only for null, bool and str. Every value decodes only from
its canonical encoding, so encode(decode(x)) == x for anything decode accepts.

    by value, both ways   i f d q c b l t s fs m date datetime time td u
    oracle -> worker      r{id}                      a handle the worker issued, coming back
    worker -> oracle      mod{id, name, pkg}  cls{c}
                          o{id, c}                   opaque object or mutable record
                          o{id, c, base}             instance of a class anchored on a value
                          o{id, c, args, str}        exception
                          o{id, c, name, value}      plain Enum member
                          o{id, c, f}                frozen record, with its field snapshot
    class descriptor c    {anchor: "<module>.<qualname>"}
                          | {id, name, qualname, module, bases: [c...], ops: [...],
                             kind: "object" | "record" | "enum", frozen: bool}

decode() parses every handle and descriptor into the typed Handle union below, under the same
depth and item budget as the values around it, and checks that the payload matches the
descriptor. A side's resolve() only maps a well-formed Handle to its own objects.
"""

from __future__ import annotations

import base64
import builtins
import datetime as dt
import enum
import json
import os
import struct
import uuid
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

ENV_RPC = "TESTGATE_RPC"  # "<read fd>,<write fd>" of the worker's ends

MAX_FRAME = 16 << 20
MAX_DEPTH = 32  # containers, handles and class descriptors nested in one another
MAX_ITEMS = 1_000_000  # values and descriptors nested under one top-level value
MAX_DECIMAL_DIGITS = 4300  # digits of an int, a Decimal coefficient or |Decimal exponent|;
#                            matches CPython's int-str limit, so int(Decimal) stays small
MAX_NAME = 256  # attribute, class, qualname and module names

_MAX_ID = 1 << 63
_INT_BITS = (10**MAX_DECIMAL_DIGITS).bit_length()

# Builtin value classes a shadow class may inherit from. A package class with one of these in
# its MRO (IntEnum, StrEnum, NamedTuple, Decimal/str/int subclasses) behaves in the oracle as
# its builtin value: ==, hash, ordering, truthiness and arithmetic run on the copied base
# value, and no special method of the package class is forwarded. Builtin exception classes
# are anchors too (see anchor_names); any BaseException that is not an Exception anchors to
# Exception, so the worker can never make the oracle raise SystemExit, KeyboardInterrupt,
# GeneratorExit or a pytest outcome.
VALUE_ANCHORS: tuple[type, ...] = (
    int, float, str, bytes, tuple, list, dict, set, frozenset, Decimal,
)  # fmt: skip

# Special methods an opaque proxy may forward to the worker, when the remote class has them.
FORWARDED_OPS: tuple[str, ...] = (
    "__call__", "__len__", "__iter__", "__next__", "__getitem__", "__setitem__", "__delitem__",
    "__int__", "__float__", "__index__", "__enter__", "__exit__", "__repr__", "__str__",
    "__format__", "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
    "__truediv__", "__rtruediv__", "__floordiv__", "__mod__", "__pow__", "__neg__", "__pos__",
    "__abs__", "__round__",
)  # fmt: skip

# Binary arithmetic among FORWARDED_OPS. Forwarded only when every operand is a package
# handle; with a test value as an operand it is a VERDICT_OP violation. Otherwise
# `abs(obj - expected) < tol` would hand `expected` to package code whose __sub__ can answer 0.
OPERAND_OPS: frozenset[str] = frozenset({
    "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__", "__truediv__",
    "__rtruediv__", "__floordiv__", "__mod__", "__pow__",
})  # fmt: skip

# Ops whose answer would be a verdict. On an opaque proxy they are refused (a violation),
# including proxy-vs-proxy and `assert proxy`; they are never forwarded. Records compare
# fields in the oracle, enum members by identity, anchored classes as their builtin.
VERDICT_OPS: tuple[str, ...] = (
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__contains__", "__bool__",
    "__hash__",
)  # fmt: skip

# Dunder attributes the oracle forwards on proxies and remote modules. Every other dunder is a
# plain AttributeError: pytest and hypothesis probe dunders (__test__, __file__, __wrapped__)
# and expect that answer.
FORWARDED_DUNDERS: frozenset[str] = frozenset({"__name__", "__qualname__", "__doc__", "__all__"})

# How the oracle treats instances of a class that is not anchored. The worker reports it; a
# lie only changes how two package objects compare with each other.
#   object: opaque proxy (VERDICT_OPS refused)
#   record: dataclass with eq=True. == compares class identity and field values in the
#           oracle. Frozen: the snapshot `f` taken when it crossed (hashable). Mutable: a fresh
#           `fields{id}` snapshot at comparison time (unhashable, as in Python).
#   enum:   plain Enum member; crosses with name and value; == is identity
CLASS_KINDS: tuple[str, ...] = ("object", "record", "enum")


class Violation(str, enum.Enum):
    """Kinds carried by the oracle's end record. Closed set: no test or type names."""

    VERDICT_OP = "verdict-op"  # ==, <, in, bool, hash, or arithmetic with a test value,
    #                           on an opaque package object
    UNENCODABLE = "unencodable"  # a test passed a callable, Mock, Path or other non-value in
    ORACLE_PATH = "oracle-path"  # a str naming a path inside the oracle root
    SUBCLASS = "subclass"  # a test subclassed a package class
    WORKER_LOST = "worker-lost"  # the package process died, hung up, or died before EOF
    PROTOCOL = "protocol"  # malformed frame, cap exceeded, bad name or tag, desync
    LEAKED_MODULE = "leaked-module"  # package code was loaded inside the oracle


class ProtocolViolation(BaseException):
    """Every boundary refusal, unencodable value, protocol error, dead worker or leak.

    A BaseException, never an Exception or TypeError, so `pytest.raises(Exception)`,
    `except Exception` and hypothesis cannot swallow it. pytest still reports the test as
    failed. A test that catches BaseException still fails the run: the oracle's Link records
    every violation before raising, and the end record carries the list.
    """

    def __init__(self, kind: Violation, detail: str) -> None:
        super().__init__(f"testgate oracle: {kind.value}: {detail}")
        self.kind = kind


def _refuse(detail: str) -> NoReturn:
    raise ProtocolViolation(Violation.PROTOCOL, detail)


def anchor_names() -> dict[type, str]:
    """Real class -> "<module>.<qualname>", for VALUE_ANCHORS and every builtin exception
    class."""
    exceptions = [
        c
        for c in builtins.__dict__.values()
        if isinstance(c, type) and issubclass(c, BaseException)
    ]
    return {c: f"{c.__module__}.{c.__qualname__}" for c in (*VALUE_ANCHORS, *exceptions)}


def anchor_table() -> dict[str, type]:
    """ "<module>.<qualname>" -> the class the oracle uses. A BaseException that is not an
    Exception maps to Exception."""
    return {
        name: Exception if issubclass(c, BaseException) and not issubclass(c, Exception) else c
        for c, name in anchor_names().items()
    }


_ANCHORS = anchor_table()


class ClassDesc(NamedTuple):
    """A package class, as the worker described it. Names are validated identifiers."""

    id: int
    name: str
    qualname: str
    module: str
    bases: tuple[ClassInfo, ...]
    ops: frozenset[str]  # subset of FORWARDED_OPS
    kind: str  # one of CLASS_KINDS
    frozen: bool


if TYPE_CHECKING:
    ClassInfo = type | ClassDesc  # an anchor (from anchor_table) or a package class


class Ref(NamedTuple):
    """r: a handle the worker issued, coming back to it."""

    id: int


class ModuleRef(NamedTuple):
    id: int
    name: str
    pkg: bool


class ClassRef(NamedTuple):
    cls: ClassInfo


class Obj(NamedTuple):
    """An opaque object, or a mutable record (cls.kind == "record")."""

    id: int
    cls: ClassDesc


class Anchored(NamedTuple):
    """An instance of a package class anchored on a builtin value; base is that value."""

    id: int
    cls: ClassDesc
    base: Any


class Raised(NamedTuple):
    """An exception. cls has a builtin exception class among its anchors."""

    id: int
    cls: ClassInfo
    args: tuple[Any, ...]
    text: str


class Member(NamedTuple):
    """A plain Enum member."""

    id: int
    cls: ClassDesc
    name: str
    value: Any


class Frozen(NamedTuple):
    """A frozen record with the field snapshot taken when it crossed."""

    id: int
    cls: ClassDesc
    fields: tuple[tuple[str, Any], ...]


if TYPE_CHECKING:
    Handle = Ref | ModuleRef | ClassRef | Obj | Anchored | Raised | Member | Frozen


def _dump_int(v: int) -> str:
    if v.bit_length() > _INT_BITS:
        _refuse("int over MAX_DECIMAL_DIGITS")
    try:
        text = str(v)
    except ValueError:
        _refuse("int over the int-str limit")
    if len(text) - (v < 0) > MAX_DECIMAL_DIGITS:
        _refuse("int over MAX_DECIMAL_DIGITS")
    return text


def _load_int(text: Any) -> int:
    if type(text) is not str:
        raise TypeError("int text")
    digits = text[1:] if text[:1] == "-" else text
    if not (0 < len(digits) <= MAX_DECIMAL_DIGITS and digits.isascii() and digits.isdigit()):
        raise ValueError("int text")
    return int(text)


def _dump_float(v: float) -> str:
    return v.hex()


def _load_float(text: Any) -> float:
    if type(text) is not str or len(text) > 32:
        raise ValueError("float text")
    return float.fromhex(text)


def _dump_decimal(v: Decimal) -> str:
    _, digits, exponent = v.as_tuple()
    if len(digits) > MAX_DECIMAL_DIGITS or (
        type(exponent) is int and abs(exponent) > MAX_DECIMAL_DIGITS
    ):
        _refuse("Decimal over MAX_DECIMAL_DIGITS")
    return str(v)


def _load_decimal(text: Any) -> Decimal:
    if type(text) is not str or len(text) > MAX_DECIMAL_DIGITS + 16 or not text.isascii():
        raise ValueError("Decimal text")
    return Decimal(text)


def _pair(data: Any) -> tuple[Any, Any]:
    if type(data) is not list or len(data) != 2:
        raise ValueError("expected a pair")
    return data[0], data[1]


def _load_fraction(data: Any) -> Fraction:
    num, den = _pair(data)
    return Fraction(_load_int(num), _load_int(den))


def _load_complex(data: Any) -> complex:
    real, imag = _pair(data)
    return complex(_load_float(real), _load_float(imag))


def _load_bytes(text: Any) -> bytes:
    if type(text) is not str or not text.isascii():
        raise ValueError("base64 text")
    return base64.b64decode(text, validate=True)


def _iso_loader(cls: Any) -> Callable[[Any], Any]:
    def load(text: Any) -> Any:
        if type(text) is not str or len(text) > 64:
            raise ValueError("isoformat text")
        value = cls.fromisoformat(text)
        if isinstance(value, _NAIVE_ONLY) and value.tzinfo is not None:
            raise ValueError("aware datetime or time")
        return value

    return load


def _load_timedelta(data: Any) -> dt.timedelta:
    if type(data) is not list or len(data) != 3 or any(type(x) is not int for x in data):
        raise ValueError("timedelta parts")
    return dt.timedelta(days=data[0], seconds=data[1], microseconds=data[2])


def _load_uuid(text: Any) -> uuid.UUID:
    if type(text) is not str or len(text) != 36:
        raise ValueError("uuid text")
    return uuid.UUID(text)


_NAIVE_ONLY = (dt.datetime, dt.time)


class _Scalar(NamedTuple):
    tag: str
    type: type
    dump: Callable[[Any], Any]
    load: Callable[[Any], Any]


_SCALARS: tuple[_Scalar, ...] = (
    _Scalar("i", int, _dump_int, _load_int),
    _Scalar("f", float, _dump_float, _load_float),
    _Scalar("d", Decimal, _dump_decimal, _load_decimal),
    _Scalar("q", Fraction, lambda v: [_dump_int(v.numerator), _dump_int(v.denominator)],
            _load_fraction),
    _Scalar("c", complex, lambda v: [v.real.hex(), v.imag.hex()], _load_complex),
    _Scalar("b", bytes, lambda v: base64.b64encode(v).decode("ascii"), _load_bytes),
    _Scalar("date", dt.date, lambda v: v.isoformat(), _iso_loader(dt.date)),
    _Scalar("datetime", dt.datetime, lambda v: v.isoformat(), _iso_loader(dt.datetime)),
    _Scalar("time", dt.time, lambda v: v.isoformat(), _iso_loader(dt.time)),
    _Scalar("td", dt.timedelta, lambda v: [v.days, v.seconds, v.microseconds], _load_timedelta),
    _Scalar("u", uuid.UUID, str, _load_uuid),
)  # fmt: skip
_SCALAR_BY_TYPE = {s.type: s for s in _SCALARS}
_SCALAR_BY_TAG = {s.tag: s for s in _SCALARS}

_CONTAINER_TAGS: dict[type, str] = {list: "l", tuple: "t", set: "s", frozenset: "fs", dict: "m"}
_SEQUENCES: dict[str, Callable[[list[Any]], Any]] = {
    "l": list, "t": tuple, "s": set, "fs": frozenset,
}  # fmt: skip
_UNHASHABLE_TAGS = frozenset({"l", "s", "m"})
_HANDLE_TAGS = frozenset({"r", "mod", "cls", "o"})

# Exact types copied by value in both directions; containers recurse. Encoding dispatches on
# `type(v) is T`, never isinstance: a subclass instance is not a value. It crosses as a
# handle, with its builtin value attached when it is anchored (see VALUE_ANCHORS).
VALUE_TYPES: tuple[type, ...] = (
    type(None), bool, str, *(s.type for s in _SCALARS), *_CONTAINER_TAGS,
)  # fmt: skip


if TYPE_CHECKING:
    Export = Callable[[Any, Callable[[Any], Any]], dict[str, Any]]
    Resolve = Callable[[Handle], Any]


class _Budget:
    def __init__(self) -> None:
        self.items = 0

    def count(self, n: int) -> None:
        self.items += n
        if self.items > MAX_ITEMS:
            _refuse(f"more than MAX_ITEMS ({MAX_ITEMS}) items")

    @staticmethod
    def enter(depth: int) -> int:
        if depth >= MAX_DEPTH:
            _refuse(f"nested deeper than MAX_DEPTH ({MAX_DEPTH})")
        return depth + 1


def encode(value: Any, export: Export) -> Any:
    """Exact VALUE_TYPES by value; everything else -> export(value, child).

    `export` is the side's policy and returns a handle dict. A handle opens one nesting level,
    as a container does. The worker issues o/cls/mod handles and passes each payload value
    (base, args, value, f) through `child`, which encodes it inside that level under the same
    budget. The oracle sends r{id} for its proxies and raises a violation for anything else.
    Raises ProtocolViolation(PROTOCOL) for a value over the caps. The encoder does not count
    class descriptors.
    """
    return _Encoder(export).value(value, 0)


class _Encoder(_Budget):
    def __init__(self, export: Export) -> None:
        super().__init__()
        self.export = export

    def value(self, value: Any, depth: int) -> Any:
        t = type(value)
        if value is None or t is bool or t is str:
            return value
        scalar = _SCALAR_BY_TYPE.get(t)
        if scalar is not None and not (t in _NAIVE_ONLY and value.tzinfo is not None):
            return {"$": scalar.tag, "v": scalar.dump(value)}
        tag = _CONTAINER_TAGS.get(t)
        inner = self.enter(depth)
        if tag is None:
            return self.export(value, lambda payload: self.child(payload, inner))
        if tag == "m":
            self.count(2 * len(value))
            return {
                "$": "m",
                "v": [[self.value(k, inner), self.value(v, inner)] for k, v in value.items()],
            }
        self.count(len(value))
        return {"$": tag, "v": [self.value(x, inner) for x in value]}

    def child(self, value: Any, depth: int) -> Any:
        self.count(1)
        return self.value(value, depth)


def decode(data: Any, resolve: Resolve) -> Any:
    """Inverse of encode for the closed value set; every handle -> resolve(parsed Handle).

    Builds only VALUE_TYPES and the Handle records. Enforces MAX_DEPTH, MAX_ITEMS and
    MAX_DECIMAL_DIGITS, canonical encodings, descriptor names, and payload-versus-descriptor
    consistency, and raises ProtocolViolation(PROTOCOL) on any breach, unknown tag or
    malformed payload. Set members and dict keys must hash in the oracle without package
    code: an opaque object, a mutable record or an exception there is refused.
    """
    return _Decoder(resolve).value(data, 0, False)


_O_PAYLOADS: dict[frozenset[str], str] = {
    frozenset(): "obj",
    frozenset({"base"}): "anchored",
    frozenset({"args", "str"}): "raised",
    frozenset({"name", "value"}): "member",
    frozenset({"f"}): "frozen",
}
_O_KEYS = frozenset({"$", "id", "c"})
_DESC_KEYS = frozenset({"id", "name", "qualname", "module", "bases", "ops", "kind", "frozen"})
_VALUE_KEYS = frozenset({"$", "v"})


def _keys(data: dict[str, Any], keys: frozenset[str], what: str) -> None:
    if data.keys() != keys:
        _refuse(f"{what}: unexpected fields")


class _Decoder(_Budget):
    def __init__(self, resolve: Resolve) -> None:
        super().__init__()
        self.resolve = resolve

    def value(self, data: Any, depth: int, key: bool) -> Any:
        if data is None or type(data) is bool or type(data) is str:
            return data
        if type(data) is not dict:
            _refuse(f"untagged {type(data).__name__}")
        tag = data.get("$")
        if type(tag) is not str:
            _refuse("missing tag")
        scalar = _SCALAR_BY_TAG.get(tag)
        if scalar is not None:
            _keys(data, _VALUE_KEYS, tag)
            return self.scalar(scalar, data["v"])
        if tag in _SEQUENCES or tag == "m":
            _keys(data, _VALUE_KEYS, tag)
            if key and tag in _UNHASHABLE_TAGS:
                _refuse(f"unhashable {tag} as a set member or dict key")
            return self.container(tag, data["v"], depth, key)
        if tag in _HANDLE_TAGS:
            return self.resolve(self.handle(tag, data, depth, key))
        _refuse("unknown tag")

    @staticmethod
    def scalar(codec: _Scalar, raw: Any) -> Any:
        try:
            value = codec.load(raw)
        except (ValueError, TypeError, ArithmeticError):
            _refuse(f"malformed {codec.tag}")
        if codec.dump(value) != raw:
            _refuse(f"non-canonical {codec.tag}")
        return value

    def container(self, tag: str, raw: Any, depth: int, key: bool) -> Any:
        if type(raw) is not list:
            _refuse(f"{tag}: items are not a list")
        inner = self.enter(depth)
        if tag == "m":
            self.count(2 * len(raw))
            pairs = [self.entry(pair, inner) for pair in raw]
            return self.unique(dict, pairs, len(raw))
        self.count(len(raw))
        member_key = key or tag in ("s", "fs")
        items = [self.value(x, inner, member_key) for x in raw]
        if tag in ("s", "fs"):
            return self.unique(_SEQUENCES[tag], items, len(raw))
        return _SEQUENCES[tag](items)

    def entry(self, pair: Any, depth: int) -> tuple[Any, Any]:
        if type(pair) is not list or len(pair) != 2:
            _refuse("m: entry is not a pair")
        return self.value(pair[0], depth, True), self.value(pair[1], depth, False)

    @staticmethod
    def unique(build: Callable[[Any], Any], items: list[Any], n: int) -> Any:
        try:
            out = build(items)
        except (TypeError, ValueError):  # a value that does not hash, e.g. Decimal("sNaN")
            _refuse("unhashable set member or dict key")
        if len(out) != n:
            _refuse("duplicate set member or dict key")
        return out

    def handle(self, tag: str, data: dict[str, Any], depth: int, key: bool) -> Handle:
        inner = self.enter(depth)
        if tag == "r":
            _keys(data, frozenset({"$", "id"}), "r")
            return Ref(_ref_id(data["id"]))
        if tag == "mod":
            _keys(data, frozenset({"$", "id", "name", "pkg"}), "mod")
            if not _module_name(data["name"]) or type(data["pkg"]) is not bool:
                _refuse("mod: bad name or pkg")
            return ModuleRef(_ref_id(data["id"]), data["name"], data["pkg"])
        if tag == "cls":
            _keys(data, frozenset({"$", "c"}), "cls")
            return ClassRef(self.cls(data["c"], inner))
        if not _O_KEYS <= data.keys():
            _refuse("o: missing id or c")
        shape = _O_PAYLOADS.get(frozenset(data.keys() - _O_KEYS))
        if shape is None:
            _refuse("o: unexpected payload")
        ref = _ref_id(data["id"])
        cls = self.cls(data["c"], inner)
        anchors = _anchors(cls)
        if shape == "raised":
            args = self.payload(data["args"], inner, False)
            if key or type(args) is not tuple or type(data["str"]) is not str:
                _refuse("o: bad exception payload")
            if not any(issubclass(a, BaseException) for a in anchors):
                _refuse("o: exception without an exception anchor")
            return Raised(ref, cls, args, data["str"])
        if not isinstance(cls, ClassDesc):
            _refuse("o: an anchor class without a payload")
        if shape == "anchored":
            base = self.payload(data["base"], inner, key)
            if type(base) not in VALUE_ANCHORS or type(base) not in anchors:
                _refuse("o: base is not a value of the class's anchor")
            return Anchored(ref, cls, base)
        if anchors:
            _refuse("o: anchored class without a base")
        if shape == "member":
            name = data["name"]
            if cls.kind != "enum" or type(name) is not str or len(name) > MAX_NAME:
                _refuse("o: bad enum member")
            return Member(ref, cls, name, self.payload(data["value"], inner, False))
        if shape == "frozen":
            if cls.kind != "record" or not cls.frozen:
                _refuse("o: field snapshot for a class that is not a frozen record")
            fields = self.payload(data["f"], inner, key)
            if type(fields) is not tuple or not all(_field(f) for f in fields):
                _refuse("o: bad field snapshot")
            return Frozen(ref, cls, fields)
        if key or cls.kind == "enum" or (cls.kind == "record" and cls.frozen):
            _refuse("o: payload missing, or an object that cannot hash in the oracle")
        return Obj(ref, cls)

    def payload(self, raw: Any, depth: int, key: bool) -> Any:
        self.count(1)
        return self.value(raw, depth, key)

    def cls(self, data: Any, depth: int) -> ClassInfo:
        self.count(1)
        if type(data) is not dict:
            _refuse("class descriptor is not an object")
        if data.keys() == {"anchor"}:
            anchor = _ANCHORS.get(data["anchor"]) if type(data["anchor"]) is str else None
            if anchor is None:
                _refuse("unknown anchor")
            return anchor
        _keys(data, _DESC_KEYS, "class descriptor")
        name, qualname, module = data["name"], data["qualname"], data["module"]
        if not (_identifier(name) and _qualname(qualname) and _module_name(module)):
            _refuse("class descriptor: bad name")
        ops, kind, frozen, bases = data["ops"], data["kind"], data["frozen"], data["bases"]
        if type(ops) is not list or not all(type(o) is str and o in FORWARDED_OPS for o in ops):
            _refuse("class descriptor: ops outside FORWARDED_OPS")
        if len(set(ops)) != len(ops) or kind not in CLASS_KINDS or type(frozen) is not bool:
            _refuse("class descriptor: bad ops, kind or frozen")
        if type(bases) is not list:
            _refuse("class descriptor: bases are not a list")
        inner = self.enter(depth)
        parsed = tuple(self.cls(b, inner) for b in bases)
        return ClassDesc(
            _ref_id(data["id"]), name, qualname, module, parsed, frozenset(ops), kind, frozen
        )


def _anchors(cls: ClassInfo) -> set[type]:
    if isinstance(cls, type):
        return {cls}
    found: set[type] = set()
    for base in cls.bases:
        found |= _anchors(base)
    return found


def _field(item: Any) -> bool:
    return type(item) is tuple and len(item) == 2 and _identifier(item[0])


def _valid_id(value: Any) -> bool:
    return type(value) is int and 0 < value < _MAX_ID


def _ref_id(value: Any) -> int:
    if not _valid_id(value):
        _refuse("bad handle id")
    return int(value)


def _identifier(name: Any) -> bool:
    return type(name) is str and len(name) <= MAX_NAME and name.isidentifier()


def _module_name(name: Any) -> bool:
    return (
        type(name) is str
        and len(name) <= MAX_NAME
        and all(part.isidentifier() for part in name.split("."))
    )


def _qualname(name: Any) -> bool:
    return (
        type(name) is str
        and len(name) <= MAX_NAME
        and all(part.isidentifier() or part == "<locals>" for part in name.split("."))
    )


def _attr_name(name: Any) -> bool:
    if not _identifier(name):
        return False
    return not (name.startswith("__") and name.endswith("__")) or name in FORWARDED_DUNDERS


def _op_name(name: Any) -> bool:
    return type(name) is str and name in FORWARDED_OPS


def _anything(_: Any) -> bool:
    return True


if TYPE_CHECKING:
    _Check = Callable[[Any], bool]

REQUESTS: dict[str, dict[str, _Check]] = {
    "imp": {"name": _module_name},
    "get": {"id": _valid_id, "name": _attr_name},
    "set": {"id": _valid_id, "name": _attr_name, "v": _anything},
    "call": {"id": _valid_id, "a": _anything, "k": _anything},
    "op": {"id": _valid_id, "name": _op_name, "a": _anything},
    "fields": {"id": _valid_id},
}
REPLIES: dict[str, dict[str, _Check]] = {
    "ret": {"v": _anything, "wb": _anything},
    "err": {"exc": _anything},
}
_OPTIONAL = frozenset({"wb"})


def dumps(msg: dict[str, Any]) -> bytes:
    """One frame body. ProtocolViolation(PROTOCOL) when it is over MAX_FRAME."""
    body = json.dumps(msg, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(body) > MAX_FRAME:
        _refuse(f"frame over MAX_FRAME ({MAX_FRAME} bytes)")
    return body


def loads(body: bytes, schema: dict[str, dict[str, _Check]]) -> dict[str, Any]:
    """Parse one frame body against REQUESTS or REPLIES.

    ProtocolViolation(PROTOCOL) when the bytes are not UTF-8 JSON, a key repeats, a JSON
    number is a float or longer than MAX_DECIMAL_DIGITS, a NaN/Infinity literal appears, the
    top level is not an object, the op is not in `schema`, "n" is missing or not an int, or a
    field is missing, unknown or fails its check. Value fields stay encoded.
    """
    try:
        msg = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_int=_json_int,
            parse_float=_json_float,
            parse_constant=_json_constant,
        )
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        _refuse(f"not a JSON frame ({type(exc).__name__})")
    if type(msg) is not dict:
        _refuse("frame is not an object")
    op = msg.get("op")
    fields = schema.get(op) if type(op) is str else None
    if fields is None:
        _refuse("unexpected op")
    if not (type(msg.get("n")) is int and 0 <= msg["n"] < _MAX_ID):
        _refuse("missing or bad sequence id")
    for name, value in msg.items():
        if name in ("op", "n"):
            continue
        check = fields.get(name)
        if check is None or not check(value):
            _refuse(f"{op}: unexpected or bad field")
    if fields.keys() - msg.keys() - _OPTIONAL:
        _refuse(f"{op}: missing field")
    return msg


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        _refuse("repeated JSON key")
    return out


def _json_int(text: str) -> int:
    if len(text) - text.startswith("-") > MAX_DECIMAL_DIGITS:
        _refuse("JSON int over MAX_DECIMAL_DIGITS")
    return int(text)


def _json_float(_: str) -> NoReturn:
    _refuse("JSON float literal")


def _json_constant(_: str) -> NoReturn:
    _refuse("NaN or Infinity literal")


def send(fd: int, msg: dict[str, Any]) -> None:
    body = dumps(msg)
    view = memoryview(struct.pack(">I", len(body)) + body)
    while view:
        view = view[os.write(fd, view) :]


def recv(fd: int, schema: dict[str, dict[str, _Check]]) -> dict[str, Any]:
    """Read one frame and parse it with loads().

    EOFError on a clean EOF before the first byte. ProtocolViolation(PROTOCOL) when the
    length exceeds MAX_FRAME (before reading the body) or the frame is cut short.
    """
    (size,) = struct.unpack(">I", _read(fd, 4, True))
    if size > MAX_FRAME:
        _refuse(f"frame over MAX_FRAME ({MAX_FRAME} bytes)")
    return loads(_read(fd, size, False), schema)


def _read(fd: int, n: int, first: bool) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = os.read(fd, min(n - len(buf), 1 << 20))
        if not chunk:
            if first and not buf:
                raise EOFError
            _refuse("truncated frame")
        buf += chunk
    return bytes(buf)
