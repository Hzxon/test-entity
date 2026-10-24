"""cli: init, run, mutants, exit codes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("testgate.sandbox")
pytest.importorskip("testgate.runner")
pytest.importorskip("testgate.mutator")
pytest.importorskip("testgate.gitinfo")

from testgate.cli import main


def test_init_writes_files_and_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--package", "src/widget", "--import-name", "widget"]) == 0
    cfg = (tmp_path / "testgate.yaml").read_text()
    assert "package: src/widget" in cfg
    assert "import_name: widget" in cfg
    assert "__PACKAGE__" not in cfg
    example = (tmp_path / "tests/golden/test_example.py").read_text()
    assert "import widget" in example
    assert "from hypothesis import given, strategies as st" in example
    assert (tmp_path / "tests/auto/.gitkeep").exists()
    wf = (tmp_path / ".github/workflows/testgate.yml").read_text()
    assert "env.HIDDEN_REPO != ''" in wf
    out = capsys.readouterr().out
    assert "TESTGATE_SEED" in out
    assert "hidden tests repo" in out
    assert '"testgate" required check' in out


def test_init_language_js(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from testgate.config import load_config

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--language", "js"]) == 0
    cfg = load_config(tmp_path, None)
    assert (cfg.language, cfg.package, cfg.import_name) == ("js", "src", "index.js")
    assert "\nlanguage: js\n" in (tmp_path / "testgate.yaml").read_text()
    example = (tmp_path / "tests/golden/example.test.js").read_text()
    assert "../../src/index.js" in example
    assert "fast-check" in example
    assert not (tmp_path / "tests/golden/test_example.py").exists()


def test_init_python_config_keeps_language_commented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert "# language: python" in (tmp_path / "testgate.yaml").read_text()


def test_init_refuses_overwrite_then_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "testgate.yaml").write_text("mine\n")
    assert main(["init"]) == 2
    assert (tmp_path / "testgate.yaml").read_text() == "mine\n"
    assert not (tmp_path / ".github").exists()
    assert main(["init", "--force"]) == 0
    assert "package:" in (tmp_path / "testgate.yaml").read_text()


def test_init_template_config_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from testgate.config import load_config

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--package", "pkg"]) == 0
    cfg = load_config(tmp_path, None)
    assert (cfg.package, cfg.import_name) == ("pkg", "pkg")
    assert cfg.hidden is None


def test_usage_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert main(["run", "--root", str(tmp_path), "--no-git"]) == 2
    assert "testgate.yaml not found" in capsys.readouterr().err
    (tmp_path / "testgate.yaml").write_text("package: p\nimport_name: p\nbogus: 1\n")
    assert main(["run", "--root", str(tmp_path), "--no-git"]) == 2
    assert "bogus" in capsys.readouterr().err
    assert main(["run", "--root", str(tmp_path), "--no-git", "--only", "nope"]) == 2


def test_run_without_base_ref_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=tmp_path, check=True)
    (tmp_path / "testgate.yaml").write_text("package: p\nimport_name: p\n")
    assert main(["run", "--root", str(tmp_path)]) == 2
    assert "no base ref found; pass --base or use --no-git" in capsys.readouterr().err


def make_project(root: Path, good: bool) -> Path:
    pkg = root / "src/calc"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    golden = root / "tests/golden"
    golden.mkdir(parents=True)
    (golden / "test_ops.py").write_text(
        "from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    hidden = root.parent / (root.name + "-hidden")
    hidden.mkdir()
    expected = "7" if good else "8"
    (hidden / "test_h.py").write_text(
        "from calc.ops import add\n\n\n"
        f"def test_hidden_secret_name():\n    assert add(3, 4) == {expected}\n"
    )
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "gates:\n  tests: {repeats: 2}\n  coverage: {min_changed: 0.0}\n"
        "  mutation: {min_score: 0.0, timeout_s: 60}\n"
    )
    return hidden


@pytest.mark.parametrize(("good", "code"), [(True, 0), (False, 1)])
def test_run_no_git_reaches_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    good: bool,
    code: int,
) -> None:
    monkeypatch.delenv("TESTGATE_SEED", raising=False)
    monkeypatch.delenv("TESTGATE_HIDDEN_DIR", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, good)
    json_path = tmp_path / "out.json"
    log_path = tmp_path / "private.log"
    rc = main(
        [
            "run",
            "--root",
            str(root),
            "--no-git",
            "--json",
            str(json_path),
            "--private-log",
            str(log_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == code
    assert ("testgate: PASS" if good else "testgate: FAIL") in out
    data = json.loads(json_path.read_text())
    assert data["verdict"] == ("pass" if good else "fail")
    assert data["seed_is_secret"] is False
    by_name = {g["name"]: g for g in data["gates"]}
    assert by_name["protected"]["status"] == "skip"
    assert "test_hidden_secret_name" not in json_path.read_text()
    assert "test_hidden_secret_name" not in out
    assert "test_hidden_secret_name" not in log_path.read_text()  # #51


def test_run_quiet_and_step_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, True)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("TESTGATE_SEED", "abc")
    rc = main(["run", "--root", str(root), "--no-git", "--quiet", "--only", "build"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert "testgate: PASS" in summary.read_text()
    assert "seed_is_secret: true" in summary.read_text()


def test_mutants_lists_without_running_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, True)
    assert main(["mutants", "--root", str(root), "--no-git", "--max", "5"]) == 0
    out = capsys.readouterr().out
    assert "src/calc/ops.py:2:" in out
    assert " -> " in out


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_run_fails_when_coverage_drops_below_base(tmp_path: Path) -> None:
    """A change that deletes the test covering a branch drops coverage and fails the gate."""
    root = tmp_path / "proj"
    root.mkdir()
    hidden = make_project(root, good=True)
    ops = root / "src/calc/ops.py"
    ops.write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sign(x):\n    if x < 0:\n"
        "        return -1\n    return 1\n"
    )
    golden = root / "tests/golden/test_ops.py"
    golden.write_text(
        golden.read_text() + "\n\nfrom calc.ops import sign\n\n\n"
        "def test_sign():\n    assert sign(-5) == -1\n    assert sign(5) == 1\n"
    )
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "protected: []\n"
        "gates:\n  tests: {repeats: 1}\n  coverage: {min_changed: 0.0, max_drop: 0.0}\n"
        "  mutation: false\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # the "PR": drop the test of the negative branch
    golden.write_text(golden.read_text().replace("    assert sign(-5) == -1\n", ""))

    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    coverage = next(g for g in report["gates"] if g["name"] == "coverage")
    assert coverage["status"] == "fail"
    assert "dropped" in coverage["summary"]
    assert coverage["baseline_total"] == 1.0
    assert coverage["drop"] > 0
    assert code == 1


def test_run_baseline_survives_hidden_tests_for_new_code(tmp_path: Path) -> None:
    """A hidden test that imports a module added by the PR is excluded at base, not fatal (#27)."""
    root = tmp_path / "proj"
    root.mkdir()
    hidden = make_project(root, good=True)
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "protected: []\n"
        "gates:\n  tests: {repeats: 1}\n  coverage: {min_changed: 0.0, max_drop: 1.0}\n"
        "  mutation: false\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # the PR adds a module, and a hidden test for it lands alongside
    (root / "src/calc/extra.py").write_text("def triple(x):\n    return x * 3\n")
    (hidden / "test_extra.py").write_text(
        "from calc.extra import triple\n\n\ndef test_hidden_triple():\n    assert triple(2) == 6\n"
    )
    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    coverage = next(g for g in report["gates"] if g["name"] == "coverage")
    assert "baseline_skipped" not in coverage
    assert coverage["baseline_hidden_excluded"] == 1
    assert coverage["baseline_total"] == 1.0
    assert coverage["drop"] is not None
    assert code == 0


def test_run_http_mode_changed_routes_need_fixtures(tmp_path: Path) -> None:
    """HTTP mode: a route added to the spec without a fixture fails min_changed (#34)."""
    import re
    import sys
    import textwrap

    src = (Path(__file__).parent / "test_http_language.py").read_text()
    service = re.search(r'SERVICE = textwrap.dedent\(\n    """\\\n(.*?)    """\n\)', src, re.DOTALL)
    spec = re.search(r'OPENAPI = textwrap.dedent\(\n    """\\\n(.*?)    """\n\)', src, re.DOTALL)
    assert service and spec
    root = tmp_path / "proj"
    (root / "svc").mkdir(parents=True)
    (root / "tests" / "golden").mkdir(parents=True)
    (root / "svc" / "app.py").write_text(textwrap.dedent(service.group(1)))
    (root / "svc" / "openapi.yaml").write_text(textwrap.dedent(spec.group(1)))
    (root / "tests" / "golden" / "p.http.yaml").write_text(
        "- name: price\n  request: {method: POST, path: /price, json: {subtotal: 100}}\n  expect: {status: 200}\n"
    )
    (root / "testgate.yaml").write_text(
        "language: http\npackage: svc\nimport_name: app\nprotected: []\n"
        f"http:\n  start: ['{sys.executable}', app.py, '{{port}}']\n"
        "  health: 'http://127.0.0.1:{port}/health'\n  startup_timeout_s: 15\n  openapi: openapi.yaml\n"
        "gates:\n  hidden: {required: false}\n  tests: {repeats: 1}\n"
        "  coverage: {min_changed: 1.0, max_drop: 1.0}\n  mutation: false\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # the PR adds a route nobody fixtures
    with (root / "svc" / "openapi.yaml").open("a") as f:
        f.write("  /refunds:\n    post: {summary: refund}\n")
    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    coverage = next(g for g in report["gates"] if g["name"] == "coverage")
    assert coverage["status"] == "fail"
    assert coverage["changed"] == 0.0
    assert coverage["uncovered_changed"] == ["openapi.yaml#POST /refunds:1"]
    assert coverage["baseline_total"] == 0.2  # 1 of 5 routes hit at base
    assert code == 1


def _http_project(tmp_path: Path) -> Path:
    """A git repo with the stdlib shop service, its spec and one golden fixture per route."""
    import re
    import sys
    import textwrap

    src = (Path(__file__).parent / "test_http_language.py").read_text()
    service = re.search(r'SERVICE = textwrap.dedent\(\n    """\\\n(.*?)    """\n\)', src, re.DOTALL)
    spec = re.search(r'OPENAPI = textwrap.dedent\(\n    """\\\n(.*?)    """\n\)', src, re.DOTALL)
    assert service and spec
    root = tmp_path / "proj"
    (root / "svc").mkdir(parents=True)
    (root / "tests" / "golden").mkdir(parents=True)
    (root / "svc" / "app.py").write_text(textwrap.dedent(service.group(1)))
    (root / "svc" / "openapi.yaml").write_text(textwrap.dedent(spec.group(1)))
    (root / "tests" / "golden" / "p.http.yaml").write_text(
        "- name: price\n  request: {method: POST, path: /price, json: {subtotal: 100}}\n  expect: {status: 200}\n"
        "- name: order\n  request: {path: /orders/7}\n  expect: {status: 404}\n"
        "- name: health\n  request: {path: /health}\n  expect: {status: 200}\n"
    )
    (root / "testgate.yaml").write_text(
        "language: http\npackage: svc\nimport_name: app\nprotected: []\n"
        f"http:\n  start: ['{sys.executable}', app.py, '{{port}}']\n"
        "  health: 'http://127.0.0.1:{port}/health'\n  startup_timeout_s: 15\n  openapi: openapi.yaml\n"
        "gates:\n  hidden: {required: false}\n  tests: {repeats: 1}\n"
        "  coverage: {min_changed: 0.0, max_drop: 1.0}\n  mutation: {min_score: 1.0}\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def test_run_http_differential_catches_undeclared_behaviour_change(tmp_path: Path) -> None:
    """HTTP mode (#35): a response change on a route whose spec did not change is a survivor."""
    root = _http_project(tmp_path)
    app = root / "svc" / "app.py"
    app.write_text(app.read_text().replace('"currency": "USD"', '"currency": "EUR"'))
    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    mutation = next(g for g in report["gates"] if g["name"] == "mutation")
    assert mutation["status"] == "fail", mutation
    assert mutation["compared"] == 3 and mutation["identical"] == 2
    assert mutation["survivors"] == [{"route": "POST /price", "case": "price", "file": "golden"}]
    assert code == 1

    # #66: editing the spec no longer exempts the route; the edit is flagged for review
    spec = root / "svc" / "openapi.yaml"
    spec.write_text(
        spec.read_text().replace("post: {summary: price}", "post: {summary: price in EUR}")
    )
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    mutation = next(g for g in report["gates"] if g["name"] == "mutation")
    assert mutation["status"] == "fail", mutation
    assert mutation["compared"] == 3 and mutation["changed_routes"] == 0
    assert mutation["spec_changed"] is True
    assert "human must review" in mutation["summary"]
    assert code == 1


# --- secure workflow template (#69) -------------------------------------------

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_init_workflow_is_locked_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--action", f"acme/testgate@{SHA}"]) == 0
    text = (tmp_path / ".github/workflows/testgate.yml").read_text()
    wf = yaml.safe_load(text)
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["testgate"]
    assert job["environment"] == "testgate"
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses[-1] == f"acme/testgate@{SHA}"
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref  # every action pinned by SHA
    for step in job["steps"]:
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
    assert "upload-artifact" not in text and "private" not in text.split("never uploaded")[1]
    assert "@v0" not in text


def test_init_without_action_emits_a_sha_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (
        "uses: <owner>/testgate@<full-sha>"
        in (tmp_path / ".github/workflows/testgate.yml").read_text()
    )
    assert "Replace <owner>/testgate@<full-sha>" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", ["acme/testgate@v0", "acme/testgate@main", "acme/testgate@abc123", "testgate@" + SHA]
)
def test_init_refuses_unpinned_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--action", bad]) == 2
    assert not (tmp_path / ".github").exists()
