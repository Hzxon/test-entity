"""The result channel (#53): the record format between a pytest process and the harness.

A test process loads this file as a pytest plugin (``-p _testgate_channel``) from a
runner-owned directory. It writes one JSON line per record to a pipe the harness opened:

    collected{n}  collect_error{id}  collect_skip{id}  report{id, when, outcome, mark?, xfail?}
    end{status, violations?, leaked_modules?}

The harness imports this module for ``fold``, which turns those lines into a verdict. The
verdict comes from these records, never from the child's exit code.

Inputs, popped from the environment at import, so the harness never holds them:
- TESTGATE_RESULT_FD: write end of the record pipe; made non-inheritable.
- TESTGATE_KEY_FD (optional): read end of a pipe holding a key the harness wrote and closed.
  With a key, every record carries "seq" (0, 1, 2, ...) and "mac", an HMAC-SHA256 over
  f"{seq}:" plus the canonical JSON of the record without "mac". A process that can write to
  the pipe but cannot read this process's memory cannot forge, drop or reorder a record.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

ENV_RESULT_FD = "TESTGATE_RESULT_FD"
ENV_KEY_FD = "TESTGATE_KEY_FD"


@dataclass(frozen=True)
class Folded:
    ok: bool
    passed: int
    failed: int
    errors: int
    skipped: int
    failed_ids: list[str]
    violations: tuple[str, ...]


def seal(record: dict[str, Any], seq: int, key: bytes) -> dict[str, Any]:
    """The record with its sequence number and MAC added."""
    body = {**record, "seq": seq}
    return {**body, "mac": _mac(body, key)}


def _mac(body: dict[str, Any], key: bytes) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, f"{body['seq']}:{canonical}".encode(), hashlib.sha256).hexdigest()


def _records(text: str, key: bytes | None) -> tuple[list[dict[str, Any]], bool]:
    """The records in ``text`` and whether the stream is intact.

    Without a key, lines that are not JSON objects are skipped. With a key, the first line
    that is not the next sealed record ends the stream and marks it tampered.
    """
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if key is None:
            if isinstance(rec, dict):
                records.append(rec)
            continue
        if not isinstance(rec, dict):
            return records, False
        mac = rec.pop("mac", None)
        if (
            rec.get("seq") != len(records)
            or not isinstance(mac, str)
            or not hmac.compare_digest(mac, _mac(rec, key))
        ):
            return records, False
        records.append(rec)
    return records, True


def fold(text: str, key: bytes | None) -> Folded | None:
    """Fold channel records into counts and a verdict; None when an intact stream collected
    nothing.

    A skip counts only when a skip/skipif marker decided it; any other skip, a
    collection-time skip, or an xfail is a failure with its id. With a key, a stream with
    one bad, missing or reordered record is not ok. An end record that names violations or
    leaked modules is not ok.
    """
    records, intact = _records(text, key)
    collected: int | None = None
    ended = False
    clean = True
    violations: tuple[str, ...] = ()
    collect_errors: list[str] = []
    state: dict[str, str | None] = {}
    for rec in records:
        kind = rec.get("t")
        if kind == "collected":
            collected = int(rec.get("n", 0))
        elif kind in ("collect_error", "collect_skip"):
            collect_errors.append(str(rec.get("id", "")))
        elif kind == "end":
            ended = True
            raw = rec.get("violations", [])
            violations = tuple(str(v) for v in raw) if isinstance(raw, list) else (str(raw),)
            clean = not violations and not rec.get("leaked_modules")
        elif kind == "report":
            nid = str(rec.get("id", ""))
            when, outcome = rec.get("when"), rec.get("outcome")
            cur = state.get(nid)
            if outcome == "failed":
                state[nid] = "failed" if when == "call" else (cur if cur == "failed" else "error")
            elif outcome == "skipped":
                legit = when == "setup" and rec.get("mark") is True and rec.get("xfail") is not True
                state[nid] = "skipped" if legit and cur is None else "failed"
            elif outcome == "passed" and when == "call" and cur is None:
                state[nid] = "passed"
            else:
                state.setdefault(nid, None)
    if intact and collected is None and not collect_errors:
        return None
    finals = [v for v in state.values() if v is not None]
    counts = {k: finals.count(k) for k in ("passed", "failed", "error", "skipped")}
    failed_ids = [n for n, v in state.items() if v in ("failed", "error")] + collect_errors
    complete = collected is not None and len(finals) == collected
    return Folded(
        ok=intact and ended and clean and complete and not failed_ids,
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"] + len(collect_errors),
        skipped=counts["skipped"],
        failed_ids=failed_ids,
        violations=violations,
    )


def _read_key(fd: int) -> bytes:
    chunks = []
    try:
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


_fd = int(os.environ.pop(ENV_RESULT_FD, "-1"))
if _fd >= 0:
    os.set_inheritable(_fd, False)
_key_fd = int(os.environ.pop(ENV_KEY_FD, "-1"))
_key = _read_key(_key_fd) if _key_fd >= 0 else None
_seq = itertools.count()
_end_fields: dict[str, Any] = {}
# Ids whose item carries a skip or skipif marker. report.keywords is no use here:
# pytest adds "skip" to it for runtime skips too.
_marked: set[str] = set()


def note_end(**fields: Any) -> None:
    """Extra fields for the end record, written at session finish."""
    _end_fields.update(fields)


def _emit(record: dict[str, Any]) -> None:
    if _fd < 0:
        return
    if _key is not None:
        record = seal(record, next(_seq), _key)
    data = (json.dumps(record) + "\n").encode()
    try:
        while data:
            data = data[os.write(_fd, data) :]
    except OSError:
        pass


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _emit({"t": "collect_error", "id": report.nodeid})
    elif report.skipped:
        _emit({"t": "collect_skip", "id": report.nodeid})


def pytest_collection_finish(session: pytest.Session) -> None:
    _marked.update(
        item.nodeid
        for item in session.items
        if any(m.name in ("skip", "skipif") for m in item.iter_markers())
    )
    _emit({"t": "collected", "n": len(session.items)})


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    rec: dict[str, Any] = {
        "t": "report",
        "id": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
    }
    if report.skipped:
        rec["mark"] = report.nodeid in _marked
        rec["xfail"] = hasattr(report, "wasxfail")
    _emit(rec)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _emit({**_end_fields, "t": "end", "status": int(exitstatus)})
