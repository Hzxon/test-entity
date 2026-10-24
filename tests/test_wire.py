from __future__ import annotations

import ast
import datetime as dt
import os
import struct
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from testgate.remote import _testgate_wire as wire

REMOTE = Path(wire.__file__).parent
DISCOUNT_SRC = Path(__file__).resolve().parents[1] / "examples" / "discount" / "src"
PROTOCOL = wire.Violation.PROTOCOL


def values_only(value: Any, child: Callable[[Any], Any]) -> dict[str, Any]:
    raise AssertionError(f"not a value: {value!r}")


def keep(handle: wire.Handle) -> Any:
    return handle


def round_trip(value: Any) -> Any:
    body = wire.dumps({"op": "ret", "n": 1, "v": wire.encode(value, values_only)})
    return wire.decode(wire.loads(body, wire.REPLIES)["v"], keep)


def refused(fn: Callable[[], Any]) -> wire.ProtocolViolation:
    with pytest.raises(wire.ProtocolViolation) as info:
        fn()
    return info.value


def recv_bytes(data: bytes) -> dict[str, Any]:
    r, w = os.pipe()

    def feed() -> None:
        with suppress(BrokenPipeError):
            os.write(w, data)
        os.close(w)

    writer = threading.Thread(target=feed)
    writer.start()
    try:
        return wire.recv(r, wire.REPLIES)
    finally:
        os.close(r)
        writer.join()


def raw_frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


def frame_of_size(size: int) -> bytes:
    empty = len(b'{"op":"ret","n":1,"v":""}')
    return raw_frame(b'{"op":"ret","n":1,"v":"' + b"x" * (size - empty) + b'"}')


def exactly_equal(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if type(a) is float:
        return a.hex() == b.hex()
    if type(a) is complex:
        return exactly_equal(a.real, b.real) and exactly_equal(a.imag, b.imag)
    if type(a) is Decimal:
        return str(a) == str(b)
    if type(a) in (list, tuple):
        return len(a) == len(b) and all(map(exactly_equal, a, b))
    if type(a) is dict:
        pairs = zip(a.items(), b.items())
        return len(a) == len(b) and all(
            exactly_equal(ka, kb) and exactly_equal(va, vb) for (ka, va), (kb, vb) in pairs
        )
    if type(a) in (set, frozenset):
        index = {x: x for x in b}
        return len(a) == len(b) and all(x in index and exactly_equal(x, index[x]) for x in a)
    return bool(a == b)


def within_caps(d: Decimal) -> bool:
    _, digits, exponent = d.as_tuple()
    limit = wire.MAX_DECIMAL_DIGITS
    return len(digits) <= limit and (type(exponent) is not int or abs(exponent) <= limit)


def scalars(hashable: bool) -> st.SearchStrategy[Any]:
    up_to_the_cap = rf"-?[1-9][0-9]{{0,{wire.MAX_DECIMAL_DIGITS - 1}}}"
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.from_regex(up_to_the_cap, fullmatch=True).map(int),
        st.floats(allow_nan=not hashable),
        st.complex_numbers(allow_nan=not hashable),
        st.text(st.characters(exclude_categories=())),
        st.binary(),
        st.decimals(allow_nan=not hashable).filter(within_caps),
        st.fractions(),
        st.dates(),
        st.datetimes(),
        st.times(),
        st.timedeltas(),
        st.uuids(),
    )


HASHABLE = st.recursive(
    scalars(hashable=True),
    lambda inner: st.lists(inner, max_size=3).map(tuple) | st.frozensets(inner, max_size=3),
    max_leaves=8,
)
VALUES = st.recursive(
    scalars(hashable=False),
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.lists(inner, max_size=4).map(tuple)
        | st.dictionaries(HASHABLE, inner, max_size=4)
        | st.sets(HASHABLE, max_size=4)
        | st.frozensets(HASHABLE, max_size=4)
    ),
    max_leaves=24,
)


@settings(max_examples=300, deadline=None)
@given(VALUES)
def test_round_trip(value: Any) -> None:
    out = round_trip(value)
    assert type(out) is type(value)
    assert exactly_equal(out, value)


def test_every_value_type_round_trips() -> None:
    samples = [
        None, True, "x\ud800", 7, 1.5, -0.0, float("nan"), 1 + 2j, b"\x00", Decimal("-0.10"),
        Decimal("sNaN"), Fraction(1, 3), dt.date(2020, 1, 2), dt.datetime(2020, 1, 2, 3, 4),
        dt.time(23, 59, 59, 1), dt.timedelta(days=-1, microseconds=1),
        uuid.UUID(int=1), [], (), set(), frozenset(), {},
    ]  # fmt: skip
    assert {type(v) for v in samples} == set(wire.VALUE_TYPES)
    for value in samples:
        assert exactly_equal(round_trip(value), value), value


class IntSub(int):
    pass


class TupleSub(tuple[int, ...]):
    pass


@pytest.mark.parametrize(
    "value",
    [
        IntSub(1),
        TupleSub(),
        len,
        lambda: 1,
        Path("/"),
        object(),
        dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    ],
    ids=["int subclass", "tuple subclass", "builtin callable", "lambda", "Path", "object",
         "aware datetime"],
)  # fmt: skip
def test_non_values_go_to_export(value: Any) -> None:
    seen: list[Any] = []

    def export(v: Any, child: Callable[[Any], Any]) -> dict[str, Any]:
        seen.append(v)
        return {"$": "r", "id": 1}

    assert wire.encode([value], export) == {"$": "l", "v": [{"$": "r", "id": 1}]}
    assert len(seen) == 1 and seen[0] is value


def nested(depth: int) -> dict[str, Any]:
    data: dict[str, Any] = {"$": "l", "v": []}
    for _ in range(depth - 1):
        data = {"$": "l", "v": [data]}
    return data


def wrapped(value: list[Any], times: int) -> list[Any]:
    for _ in range(times):
        value = [value]
    return value


CAP_BREACHES: dict[str, Callable[[], Any]] = {
    "depth 33": lambda: wire.decode(nested(33), keep),
    "1,000,001 items": lambda: wire.decode({"$": "l", "v": [None] * 1_000_001}, keep),
    "4301-digit int": lambda: wire.decode({"$": "i", "v": "9" * 4301}, keep),
    'Decimal("1e4301")': lambda: wire.decode({"$": "d", "v": str(Decimal("1e4301"))}, keep),
    "frame over 16 MiB": lambda: recv_bytes(frame_of_size(16 * 1024 * 1024 + 1)),
    "NaN literal": lambda: wire.loads(b'{"op":"ret","n":1,"v":NaN}', wire.REPLIES),
    "unknown tag": lambda: wire.decode({"$": "zz", "v": 1}, keep),
}


@pytest.mark.parametrize("breach", CAP_BREACHES.values(), ids=CAP_BREACHES.keys())
def test_decoder_caps(breach: Callable[[], Any]) -> None:
    assert refused(breach).kind is PROTOCOL


AT_THE_CAPS: dict[str, Callable[[], Any]] = {
    "depth 32": lambda: wire.decode(nested(32), keep),
    "1,000,000 items": lambda: wire.decode({"$": "l", "v": [None] * 1_000_000}, keep),
    "4300-digit int": lambda: wire.decode({"$": "i", "v": "9" * 4300}, keep),
    'Decimal("1e4300")': lambda: wire.decode({"$": "d", "v": str(Decimal("1e4300"))}, keep),
}


@pytest.mark.parametrize("at_cap", AT_THE_CAPS.values(), ids=AT_THE_CAPS.keys())
def test_decoder_accepts_values_at_the_caps(at_cap: Callable[[], Any]) -> None:
    at_cap()


SENDER_BREACHES: dict[str, Callable[[], Any]] = {
    "depth 33": lambda: wire.encode(wrapped([], 32), values_only),
    "1,000,001 items": lambda: wire.encode([None] * 1_000_001, values_only),
    "4301-digit int": lambda: wire.encode(10**4300, values_only),
    'Decimal("1e4301")': lambda: wire.encode(Decimal("1e4301"), values_only),
    "frame over 16 MiB": lambda: wire.dumps({"op": "ret", "n": 1, "v": "x" * wire.MAX_FRAME}),
}


@pytest.mark.parametrize("breach", SENDER_BREACHES.values(), ids=SENDER_BREACHES.keys())
def test_encoder_refuses_what_the_decoder_would(breach: Callable[[], Any]) -> None:
    assert refused(breach).kind is PROTOCOL


def test_a_handle_is_one_nesting_level() -> None:
    def as_ref(value: Any, child: Callable[[Any], Any]) -> dict[str, Any]:
        return {"$": "r", "id": 1}

    at_cap = wire.encode(wrapped([object()], wire.MAX_DEPTH - 2), as_ref)
    assert wire.decode(at_cap, keep) == wrapped([wire.Ref(1)], wire.MAX_DEPTH - 2)
    over = wrapped([object()], wire.MAX_DEPTH - 1)
    assert refused(lambda: wire.encode(over, as_ref)).kind is PROTOCOL
    assert refused(lambda: wire.decode({"$": "l", "v": [at_cap]}, keep)).kind is PROTOCOL


def test_a_frame_of_exactly_max_frame_bytes_is_read() -> None:
    reply = recv_bytes(frame_of_size(16 * 1024 * 1024))
    assert len(reply["v"]) > 16_000_000


def test_truncated_frames_are_violations_and_clean_eof_is_eof() -> None:
    with pytest.raises(EOFError):
        recv_bytes(b"")
    assert refused(lambda: recv_bytes(b"\x00\x00")).kind is PROTOCOL
    assert refused(lambda: recv_bytes(raw_frame(b"{}")[:-1])).kind is PROTOCOL


PV_PROBE = """
import pytest
from hypothesis import given, settings, strategies as st
from _testgate_wire import ProtocolViolation, Violation

def boundary():
    raise ProtocolViolation(Violation.VERDICT_OP, "probe")

def test_plain():
    boundary()

def test_raises_exception_does_not_catch():
    with pytest.raises(Exception):
        boundary()

def test_except_exception_does_not_catch():
    try:
        boundary()
    except Exception:
        pass

@settings(max_examples=20, deadline=None)
@given(st.integers())
def test_hypothesis(x):
    if x > 3:
        boundary()

def test_after_still_runs():
    assert True
"""


def test_protocol_violation_is_not_an_exception(tmp_path: Path) -> None:
    with pytest.raises(wire.ProtocolViolation):
        try:
            wire.decode({"$": "zz"}, keep)
        except Exception:  # noqa: BLE001
            pytest.fail("except Exception caught a ProtocolViolation")

    (tmp_path / "test_pv.py").write_text(PV_PROBE)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_pv.py"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REMOTE)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "4 failed, 1 passed" in proc.stdout, proc.stdout + proc.stderr


def test_forwarded_and_verdict_ops_are_disjoint() -> None:
    assert not set(wire.FORWARDED_OPS) & set(wire.VERDICT_OPS)


def test_operand_ops_are_forwarded_ops() -> None:
    assert wire.OPERAND_OPS <= set(wire.FORWARDED_OPS)


def test_anchor_names_and_table_agree() -> None:
    names, table = wire.anchor_names(), wire.anchor_table()
    assert set(names.values()) == set(table)
    assert table["builtins.ValueError"] is ValueError
    assert table["decimal.Decimal"] is Decimal
    for demoted in ("SystemExit", "KeyboardInterrupt", "GeneratorExit", "BaseException"):
        assert table[f"builtins.{demoted}"] is Exception


BANNED_MODULES = {"pickle", "_pickle", "marshal", "shelve", "importlib", "runpy", "code"}
BANNED_NAMES = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr"}


def dynamic_code(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BANNED_MODULES:
                found.append(str(node.module))
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            found.append(node.attr)
    return found


def test_wire_has_no_dynamic_code() -> None:
    probe = "import importlib\nfrom pickle import loads\nf = getattr(o, name)\nbuiltins.eval(s)\n"
    assert dynamic_code(probe) == ["importlib", "pickle", "getattr", "eval"]
    assert dynamic_code((REMOTE / "_testgate_wire.py").read_text()) == []


def test_runtime_modules_import_with_the_stdlib_only() -> None:
    code = (
        "import sys, _testgate_wire, _testgate_worker\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('testgate', 'pytest', 'hypothesis', 'coverage')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],
        env={"PYTHONPATH": str(REMOTE)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


BAD_REPLIES: dict[str, bytes] = {
    "not JSON": b"{",
    "not UTF-8": b'{"op":"ret","n":1,"v":"\xff"}',
    "top level is a list": b"[]",
    "repeated key": b'{"op":"ret","op":"err","n":1,"v":null}',
    "float literal": b'{"op":"ret","n":1.0,"v":null}',
    "Infinity literal": b'{"op":"ret","n":1,"v":-Infinity}',
    "4301-digit JSON int": b'{"op":"ret","n":' + b"9" * 4301 + b',"v":null}',
    "no sequence id": b'{"op":"ret","v":null}',
    "bool sequence id": b'{"op":"ret","n":true,"v":null}',
    "negative sequence id": b'{"op":"ret","n":-1,"v":null}',
    "callback request": b'{"op":"cb","n":1,"id":1,"a":null}',
    "a request where a reply belongs": b'{"op":"call","n":1,"id":1,"a":null,"k":null}',
    "missing field": b'{"op":"err","n":1}',
    "unknown field": b'{"op":"ret","n":1,"v":null,"nonce":"x"}',
    "nested past the parser": b'{"op":"ret","n":1,"v":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
}

BAD_REQUESTS: dict[str, bytes] = {
    "id 0": b'{"op":"get","n":1,"id":0,"name":"x"}',
    "bool id": b'{"op":"get","n":1,"id":true,"name":"x"}',
    "dunder that is not forwarded": b'{"op":"get","n":1,"id":1,"name":"__class__"}',
    "attribute that is not an identifier": b'{"op":"get","n":1,"id":1,"name":"a.b"}',
    "op that is a verdict": b'{"op":"op","n":1,"id":1,"name":"__eq__","a":null}',
    "relative module": b'{"op":"imp","n":1,"name":".x"}',
    "a reply where a request belongs": b'{"op":"ret","n":1,"v":null}',
}


@pytest.mark.parametrize("body", BAD_REPLIES.values(), ids=BAD_REPLIES.keys())
def test_hostile_reply_envelopes_are_refused(body: bytes) -> None:
    assert refused(lambda: wire.loads(body, wire.REPLIES)).kind is PROTOCOL


@pytest.mark.parametrize("body", BAD_REQUESTS.values(), ids=BAD_REQUESTS.keys())
def test_bad_request_envelopes_are_refused(body: bytes) -> None:
    assert refused(lambda: wire.loads(body, wire.REQUESTS)).kind is PROTOCOL


def test_well_formed_envelopes_parse() -> None:
    assert wire.loads(b'{"op":"get","n":4,"id":2,"name":"__doc__"}', wire.REQUESTS)["n"] == 4
    reply = wire.loads(b'{"op":"ret","n":4,"v":null,"wb":{"$":"t","v":[]}}', wire.REPLIES)
    assert reply == {"op": "ret", "n": 4, "v": None, "wb": {"$": "t", "v": []}}


NON_CANONICAL: dict[str, Any] = {
    "int with a leading zero": {"$": "i", "v": "01"},
    "negative zero int": {"$": "i", "v": "-0"},
    "int with an underscore": {"$": "i", "v": "1_000"},
    "int with a plus": {"$": "i", "v": "+1"},
    "int in another script": {"$": "i", "v": "\u0661"},
    "int as a JSON number": {"$": "i", "v": 1},
    "Decimal with spaces": {"$": "d", "v": " 1"},
    "Decimal with an underscore": {"$": "d", "v": "1_0"},
    "float as decimal text": {"$": "f", "v": "1.5"},
    "negative NaN float": {"$": "f", "v": "-nan"},
    "base64 without padding": {"$": "b", "v": "YQ"},
    "upper-case uuid": {"$": "u", "v": "0000000A-0000-0000-0000-000000000000"},
    "compact date": {"$": "date", "v": "20200101"},
    "aware datetime": {"$": "datetime", "v": "2020-01-01T00:00:00+00:00"},
    "timedelta out of range": {"$": "td", "v": [0, 86400, 0]},
    "fraction not reduced": {"$": "q", "v": ["2", "4"]},
    "fraction over zero": {"$": "q", "v": ["1", "0"]},
    "duplicate set member": {"$": "s", "v": [{"$": "i", "v": "1"}, {"$": "i", "v": "1"}]},
    "duplicate dict key": {"$": "m", "v": [["a", None], ["a", None]]},
    "signalling NaN as a key": {"$": "fs", "v": [{"$": "d", "v": "sNaN"}]},
    "list as a set member": {"$": "s", "v": [{"$": "l", "v": []}]},
    "extra field on a value": {"$": "i", "v": "1", "x": 1},
    "bare int": 5,
    "bare list": [],
}


@pytest.mark.parametrize("data", NON_CANONICAL.values(), ids=NON_CANONICAL.keys())
def test_non_canonical_values_are_refused(data: Any) -> None:
    assert refused(lambda: wire.decode(data, keep)).kind is PROTOCOL


DESC: dict[str, Any] = {
    "id": 2,
    "name": "Thing",
    "qualname": "make.<locals>.Thing",
    "module": "pkg.mod",
    "bases": [],
    "ops": ["__call__"],
    "kind": "object",
    "frozen": False,
}
INT_DESC = {**DESC, "name": "Color", "qualname": "Color", "bases": [{"anchor": "builtins.int"}]}
RECORD_DESC = {**DESC, "kind": "record", "frozen": True}
NO_ARGS = {"$": "t", "v": []}


def obj(**fields: Any) -> dict[str, Any]:
    return {"$": "o", "id": 1, "c": DESC, **fields}


def deep_desc(depth: int) -> dict[str, Any]:
    desc: dict[str, Any] = {"anchor": "builtins.ValueError"}
    for _ in range(depth):
        desc = {**DESC, "bases": [desc]}
    return desc


GOOD_HANDLES: dict[str, tuple[Any, Callable[[Any], bool]]] = {
    "ref": ({"$": "r", "id": 3}, lambda h: h == wire.Ref(3)),
    "module": (
        {"$": "mod", "id": 1, "name": "pkg.mod", "pkg": False},
        lambda h: h == wire.ModuleRef(1, "pkg.mod", False),
    ),
    "class": ({"$": "cls", "c": DESC}, lambda h: h.cls.qualname == "make.<locals>.Thing"),
    "opaque object": (obj(), lambda h: type(h) is wire.Obj and h.cls.ops == {"__call__"}),
    "anchored on int": (
        obj(c=INT_DESC, base={"$": "i", "v": "1"}),
        lambda h: type(h) is wire.Anchored and type(h.base) is int and h.cls.bases == (int,),
    ),
    "exception": (
        obj(c={"anchor": "builtins.ValueError"}, args={"$": "t", "v": ["neg"]}, str="neg"),
        lambda h: h == wire.Raised(1, ValueError, ("neg",), "neg"),
    ),
    "SystemExit anchors to Exception": (
        obj(c={"anchor": "builtins.SystemExit"}, args=NO_ARGS, str=""),
        lambda h: h.cls is Exception,
    ),
    "enum member": (
        obj(c={**DESC, "kind": "enum"}, name="PAID", value="paid"),
        lambda h: type(h) is wire.Member and (h.name, h.value) == ("PAID", "paid"),
    ),
    "frozen record": (
        obj(c=RECORD_DESC, f={"$": "t", "v": [{"$": "t", "v": ["x", {"$": "i", "v": "1"}]}]}),
        lambda h: type(h) is wire.Frozen and h.fields == (("x", 1),),
    ),
    "frozen record as a set member": (
        {"$": "fs", "v": [obj(c=RECORD_DESC, f=NO_ARGS)]},
        lambda h: type(h) is frozenset and len(h) == 1,
    ),
}


@pytest.mark.parametrize("data, check", GOOD_HANDLES.values(), ids=GOOD_HANDLES.keys())
def test_handles_parse_into_typed_records(data: Any, check: Callable[[Any], bool]) -> None:
    assert check(wire.decode(data, keep))


BAD_HANDLES: dict[str, Any] = {
    "class name 'a b'": obj(c={**DESC, "name": "a b"}),
    "qualname with '..'": obj(c={**DESC, "qualname": "a..b"}),
    "module with a slash": obj(c={**DESC, "module": "../x"}),
    "name over MAX_NAME": obj(c={**DESC, "name": "a" * 257}),
    "unknown anchor": obj(c={"anchor": "os.system"}, args=NO_ARGS, str=""),
    "op outside FORWARDED_OPS": obj(c={**DESC, "ops": ["__eq__"]}),
    "repeated op": obj(c={**DESC, "ops": ["__call__", "__call__"]}),
    "unknown kind": obj(c={**DESC, "kind": "callback"}),
    "extra descriptor field": obj(c={**DESC, "source": "x"}),
    "id 0": obj(id=0),
    "bool id": obj(id=True),
    "two payloads": obj(c=RECORD_DESC, base={"$": "i", "v": "1"}, f=NO_ARGS),
    "enum payload on an object class": obj(name="A", value=1),
    "snapshot of a mutable record": obj(c={**RECORD_DESC, "frozen": False}, f=NO_ARGS),
    "frozen record without its snapshot": obj(c=RECORD_DESC),
    "base of the wrong anchor": obj(c=INT_DESC, base={"$": "t", "v": []}),
    "anchored class without its base": obj(c=INT_DESC),
    "base on an anchor class itself": obj(c={"anchor": "builtins.int"}, base={"$": "i", "v": "1"}),
    "exception without an exception anchor": obj(args=NO_ARGS, str=""),
    "exception args that are a list": obj(
        c={"anchor": "builtins.ValueError"}, args={"$": "l", "v": []}, str=""
    ),
    "field snapshot with a bad name": obj(
        c=RECORD_DESC, f={"$": "t", "v": [{"$": "t", "v": ["a b", None]}]}
    ),
    "opaque object as a set member": {"$": "s", "v": [obj()]},
    "opaque object as a dict key": {"$": "m", "v": [[obj(), None]]},
    "exception as a dict key": {
        "$": "m",
        "v": [[obj(c={"anchor": "builtins.ValueError"}, args=NO_ARGS, str=""), None]],
    },
    "callback tag": {"$": "cb", "id": 1},
    "extra field on a ref": {"$": "r", "id": 1, "x": 1},
    "descriptor nested past MAX_DEPTH": obj(c=deep_desc(40), args=NO_ARGS, str=""),
}


@pytest.mark.parametrize("data", BAD_HANDLES.values(), ids=BAD_HANDLES.keys())
def test_hostile_handles_are_refused(data: Any) -> None:
    assert refused(lambda: wire.decode(data, keep)).kind is PROTOCOL


TAGS = ["i", "f", "d", "q", "c", "b", "l", "t", "s", "fs", "m", "date", "datetime", "time"]
TAGS += ["td", "u", "r", "mod", "cls", "o", "zz", "cb", "ni", ""]
KEYS = ["$", "v", "id", "c", "base", "args", "str", "name", "value", "f", "pkg", "anchor"]
KEYS += ["qualname", "module", "bases", "ops", "kind", "frozen"]
JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(max_size=6),
    st.sampled_from(["0", "-1", "nan", "0x1p0", "1E+4301", "sNaN", "YQ==", "2020-01-01"]),
    st.sampled_from(["__call__", "builtins.int", "builtins.SystemExit", "record", "enum"]),
)


def json_children(inner: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    tagged = st.builds(
        lambda tag, rest: {**rest, "$": tag},
        st.sampled_from(TAGS),
        st.dictionaries(st.sampled_from(KEYS), inner, max_size=4),
    )
    keys = st.sampled_from(KEYS) | st.text(max_size=3)
    return st.lists(inner, max_size=4) | st.dictionaries(keys, inner, max_size=5) | tagged


JSON_TREES = st.recursive(JSON_LEAVES, json_children, max_leaves=30)


def only_protocol_violations(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except wire.ProtocolViolation as exc:
        assert exc.kind is PROTOCOL


@settings(max_examples=300, deadline=None)
@given(JSON_TREES)
def test_fuzz_hostile_json_values(data: Any) -> None:
    only_protocol_violations(lambda: wire.decode(data, keep))


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=512))
def test_fuzz_hostile_bytes(body: bytes) -> None:
    only_protocol_violations(lambda: wire.loads(body, wire.REPLIES))
    only_protocol_violations(lambda: wire.loads(body, wire.REQUESTS))
    if body:
        only_protocol_violations(lambda: recv_bytes(body))


@settings(max_examples=300, deadline=None)
@given(VALUES, st.lists(st.tuples(st.integers(min_value=0), st.integers(0, 255)), max_size=6))
def test_fuzz_mutated_frames(value: Any, flips: list[tuple[int, int]]) -> None:
    body = bytearray(wire.dumps({"op": "ret", "n": 1, "v": wire.encode(value, values_only)}))
    for index, byte in flips:
        body[index % len(body)] = byte
    only_protocol_violations(lambda: wire.decode(wire.loads(bytes(body), wire.REPLIES)["v"], keep))


class Worker:
    def __init__(self, log: Path, path: Path, package: str) -> None:
        to_worker, self.requests = os.pipe()
        self.replies, from_worker = os.pipe()
        self.log = log
        env = {
            "PYTHONPATH": f"{REMOTE}{os.pathsep}{path}",
            wire.ENV_RPC: f"{to_worker},{from_worker}",
        }
        with log.open("wb") as out:
            self.proc = subprocess.Popen(
                [sys.executable, "-S", "-m", "_testgate_worker", package],
                env=env,
                pass_fds=(to_worker, from_worker),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
        os.close(to_worker)
        os.close(from_worker)
        self.n = 0

    def ask(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        wire.send(self.requests, {**msg, "n": self.n})
        reply = wire.recv(self.replies, wire.REPLIES)
        assert reply["n"] == self.n
        return reply

    def value(self, msg: dict[str, Any]) -> Any:
        reply = self.ask(msg)
        assert reply["op"] == "ret", (reply, self.log.read_text())
        return wire.decode(reply["v"], keep)

    def finish(self) -> int:
        os.close(self.requests)
        code = self.proc.wait(timeout=30)
        os.close(self.replies)
        return code


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Callable[[Path, str], Worker]]:
    started: list[Worker] = []

    def start(path: Path, package: str) -> Worker:
        started.append(Worker(tmp_path / f"worker{len(started)}.log", path, package))
        return started[-1]

    yield start
    for worker in started:
        if worker.proc.poll() is None:
            worker.proc.kill()
            worker.proc.wait()


def call(fn: wire.Handle, *args: Any) -> dict[str, Any]:
    return {"op": "call", "id": fn.id, "a": wire.encode(list(args), refs), "k": {"$": "m", "v": []}}


def refs(value: Any, child: Callable[[Any], Any]) -> dict[str, Any]:
    assert type(value) in (wire.Obj, wire.Anchored, wire.Frozen, wire.Member)
    return {"$": "r", "id": value.id}


def test_worker_serves_discount(spawn: Callable[[Path, str], Worker]) -> None:
    w = spawn(DISCOUNT_SRC, "discount")
    mod = w.value({"op": "imp", "name": "discount"})
    assert mod == wire.ModuleRef(mod.id, "discount", True)
    final_price = w.value({"op": "get", "id": mod.id, "name": "final_price"})
    assert type(final_price) is wire.Obj and "__call__" in final_price.cls.ops

    reply = w.ask(
        {
            "op": "call",
            "id": final_price.id,
            "a": {"$": "l", "v": [{"$": "d", "v": "100.00"}]},
            "k": {"$": "m", "v": []},
        }
    )
    assert reply["op"] == "ret"
    assert reply["v"] == {"$": "d", "v": "90.00"}

    err = w.ask(call(final_price, Decimal(-1)))
    assert err["op"] == "err"
    assert err["exc"]["c"] == {"anchor": "builtins.ValueError"}
    raised = wire.decode(err["exc"], keep)
    assert raised.cls is ValueError and raised.args == ("subtotal must not be negative",)

    assert w.value({"op": "get", "id": mod.id, "name": "final_price"}).id == final_price.id
    assert w.value({"op": "imp", "name": "discount.core"}).name == "discount.core"
    assert w.finish() == 0, w.log.read_text()


SHAPES = """
import dataclasses, enum
from decimal import Decimal
from typing import NamedTuple

@dataclasses.dataclass(frozen=True)
class Point:
    x: int
    y: int

@dataclasses.dataclass
class Cart:
    items: list

class Color(enum.IntEnum):
    RED = 1

class Status(enum.Enum):
    PAID = "paid"

class Pair(NamedTuple):
    a: int
    b: int

class Money(Decimal):
    def __str__(self):
        return "forged"

class CouponError(ValueError):
    pass

def make(name):
    return {"point": Point(1, 2), "cart": Cart([1]), "red": Color.RED, "paid": Status.PAID,
            "pair": Pair(1, 2), "money": Money("1.50")}[name]

def fail(kind):
    if kind == "coupon":
        raise CouponError("negative coupon")
    raise KeyboardInterrupt

def append(items):
    items.append(1)

def same(a, b):
    return a is b
"""


SHAPES_FUNCS = ("make", "fail", "append", "same")


def anchors_in(desc: dict[str, Any]) -> list[str]:
    if "anchor" in desc:
        return [desc["anchor"]]
    return [a for base in desc["bases"] for a in anchors_in(base)]


def test_worker_describes_records_enums_and_anchors(
    tmp_path: Path, spawn: Callable[[Path, str], Worker]
) -> None:
    src = tmp_path / "src"
    (src / "shapes").mkdir(parents=True)
    (src / "shapes" / "__init__.py").write_text(SHAPES)
    marker = tmp_path / "imported"
    (src / "sibling.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    w = spawn(src, "shapes")
    mod = w.value({"op": "imp", "name": "shapes"})
    fn = {name: w.value({"op": "get", "id": mod.id, "name": name}) for name in SHAPES_FUNCS}

    point_reply = w.ask(call(fn["make"], "point"))
    assert point_reply["v"]["c"]["kind"] == "record"
    point = wire.decode(point_reply["v"], keep)
    assert type(point) is wire.Frozen and point.fields == (("x", 1), ("y", 2))

    cart = w.value(call(fn["make"], "cart"))
    assert type(cart) is wire.Obj and (cart.cls.kind, cart.cls.frozen) == ("record", False)
    assert w.value({"op": "fields", "id": cart.id}) == (("items", [1]),)
    assert (
        w.value({"op": "set", "id": cart.id, "name": "items", "v": wire.encode([2], values_only)})
        is None
    )
    assert w.value({"op": "fields", "id": cart.id}) == (("items", [2]),)

    red_reply = w.ask(call(fn["make"], "red"))
    assert "builtins.int" in anchors_in(red_reply["v"]["c"])
    red = wire.decode(red_reply["v"], keep)
    assert type(red) is wire.Anchored and type(red.base) is int and red.base == 1
    assert w.value(call(fn["make"], "red")).id == red.id

    paid = w.value(call(fn["make"], "paid"))
    assert type(paid) is wire.Member and (paid.name, paid.value, paid.cls.kind) == (
        "PAID",
        "paid",
        "enum",
    )
    pair = w.value(call(fn["make"], "pair"))
    assert type(pair) is wire.Anchored and pair.base == (1, 2) and type(pair.base) is tuple
    money = w.value(call(fn["make"], "money"))
    assert type(money) is wire.Anchored and money.base == Decimal("1.50")
    total = w.value(
        {"op": "op", "id": money.id, "name": "__add__", "a": wire.encode([money], refs)}
    )
    assert type(total) is Decimal and total == Decimal("3.00")

    coupon = w.ask(call(fn["fail"], "coupon"))
    assert coupon["op"] == "err" and "builtins.ValueError" in anchors_in(coupon["exc"]["c"])
    assert wire.decode(coupon["exc"], keep).args == ("negative coupon",)
    interrupt = w.ask(call(fn["fail"], "interrupt"))
    assert interrupt["exc"]["c"] == {"anchor": "builtins.KeyboardInterrupt"}
    assert wire.decode(interrupt["exc"], keep).cls is Exception

    appended = w.ask(call(fn["append"], []))
    assert wire.decode(appended["wb"], keep) == ((0, [1]),)
    assert w.value(call(fn["same"], point, point)) is True

    point_cls = w.value({"op": "get", "id": mod.id, "name": "Point"})
    assert type(point_cls) is wire.ClassRef and point_cls.cls.frozen is True

    outside = w.ask({"op": "imp", "name": "sibling"})
    assert outside["op"] == "err" and outside["exc"]["c"] == {"anchor": "builtins.ImportError"}
    assert not marker.exists()
    assert w.finish() == 0, w.log.read_text()


UNTRUSTED_FRAMES: dict[str, dict[str, Any] | bytes] = {
    "an id the worker never issued": {"op": "get", "id": 99, "name": "final_price"},
    "an oracle-side handle": {
        "op": "call",
        "id": 1,
        "a": {"$": "l", "v": [{"$": "o", "id": 1, "c": DESC}]},
        "k": {"$": "m", "v": []},
    },
    "a callback tag": {"op": "set", "id": 1, "name": "x", "v": {"$": "cb", "id": 1}},
    "bytes that are not a frame": b"\x00\x00\x00\x02{}",
}


@pytest.mark.parametrize("frame", UNTRUSTED_FRAMES.values(), ids=UNTRUSTED_FRAMES.keys())
def test_worker_exits_on_a_frame_it_cannot_parse(
    spawn: Callable[[Path, str], Worker], frame: dict[str, Any] | bytes
) -> None:
    w = spawn(DISCOUNT_SRC, "discount")
    w.value({"op": "imp", "name": "discount"})
    if isinstance(frame, bytes):
        os.write(w.requests, frame)
    else:
        wire.send(w.requests, {**frame, "n": 2})
    with pytest.raises(EOFError):
        wire.recv(w.replies, wire.REPLIES)
    assert w.finish() != 0
    assert "ProtocolViolation" in w.log.read_text()
