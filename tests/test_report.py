"""report: verdict, markdown privacy, outputs."""

from __future__ import annotations

import json
from pathlib import Path

from testgate.core import ChangedFile, GateResult, ProjectConfig, RunContext, Status
from testgate.languages import get_language
from testgate.report import build_report, private_text, to_markdown, write_outputs

SECRET = "test_hidden_secret_name AssertionError: expected 42"


def make_ctx(tmp_path: Path, changed: list[ChangedFile] | None = None) -> RunContext:
    cfg = ProjectConfig(root=tmp_path, package="src/pkg", import_name="pkg")
    ctx = RunContext(
        config=cfg,
        base_ref="origin/main",
        changed=changed or [],
        hidden_dir=None,
        seed="seed-1234",
        seed_is_secret=False,
        python="python",
    )
    ctx.log(f"private log line: {SECRET}")
    return ctx


def make_results() -> list[GateResult]:
    return [
        GateResult("protected", Status.PASS, "ok"),
        GateResult(
            "hidden",
            Status.FAIL,
            "hidden: 1 passed, 2 failed",
            public={"passed": 1, "failed": 2, "errors": 0},
            private={"output": SECRET},
        ),
        GateResult(
            "mutation",
            Status.WARN,
            "a | b",
            public={"survivors": [{"id": "a.py:3:ROR", "line": 3}], "empty": []},
        ),
    ]


def test_verdict_fail_when_any_blocks(tmp_path: Path) -> None:
    report = build_report(make_ctx(tmp_path), make_results())
    assert report.verdict is Status.FAIL


def test_verdict_error_blocks(tmp_path: Path) -> None:
    results = [GateResult("build", Status.ERROR, "crashed")]
    assert build_report(make_ctx(tmp_path), results).verdict is Status.FAIL


def test_verdict_pass_with_warn_and_skip(tmp_path: Path) -> None:
    results = [
        GateResult("protected", Status.PASS, "ok"),
        GateResult("hygiene", Status.WARN, "w"),
        GateResult("mutation", Status.SKIP, "s"),
    ]
    assert build_report(make_ctx(tmp_path), results).verdict is Status.PASS


def test_report_names_the_oracle_mode(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.language = get_language("http")
    report = build_report(ctx, [])
    assert report.to_public_dict()["oracle"] == "black-box"
    assert "oracle: black-box" in to_markdown(report)
    ctx.language = get_language("python")
    assert build_report(ctx, []).to_public_dict()["oracle"] == "in-process"


def test_changed_counts(tmp_path: Path) -> None:
    changed = [ChangedFile("a.py", {1, 2}), ChangedFile("b.py", {5})]
    report = build_report(make_ctx(tmp_path, changed), [])
    assert (report.changed_files, report.changed_lines) == (2, 3)


def test_markdown_has_no_private_text(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    md = to_markdown(build_report(ctx, make_results()))
    assert "testgate: FAIL" in md
    assert "| hidden | fail | hidden: 1 passed, 2 failed |" in md
    assert "a.py:3:ROR" in md
    assert "a \\| b" in md
    assert SECRET not in md
    assert "test_hidden_secret_name" not in md
    assert "seed-1234" not in md
    assert "seed_is_secret: false" in md


def test_private_text_has_everything(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    text = private_text(ctx, make_results())
    assert "private log line" in text
    assert "test_hidden_secret_name" in text


def test_write_outputs_and_summary_append(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    results = make_results()
    report = build_report(ctx, results)
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n")
    write_outputs(report, ctx, results, tmp_path / "out/r.json", tmp_path / "out/p.log", summary)
    data = json.loads((tmp_path / "out/r.json").read_text())
    assert data["verdict"] == "fail"
    assert SECRET not in (tmp_path / "out/r.json").read_text()
    assert "test_hidden_secret_name" in (tmp_path / "out/p.log").read_text()
    text = summary.read_text()
    assert text.startswith("existing\n")
    assert "testgate: FAIL" in text
    write_outputs(report, ctx, results, None, None, summary)
    assert summary.read_text().count("testgate: FAIL") == 2


def test_write_outputs_all_none(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_outputs(build_report(ctx, []), ctx, [], None, None, None)
