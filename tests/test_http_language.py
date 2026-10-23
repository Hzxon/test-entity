"""HttpLanguage: pure fixture logic with a fake transport, and the launcher with a real service."""

from __future__ import annotations

import json
import random
import sys
import textwrap
from pathlib import Path

import pytest

from testgate.core import GateError, ProjectConfig, Sandbox
from testgate.core import TestRun as _TestRun  # aliased so pytest does not try to collect it
from testgate.languages import get_language
from testgate.languages.http import (
    Case,
    Response,
    check,
    fill_placeholders,
    load_cases,
    run_cases,
)
from testgate.sandbox import build_sandbox, destroy

LANG = get_language("http")

# A dependency-free service: /health, POST /price with the discount rules, 404 elsewhere.
SERVICE = textwrap.dedent(
    """\
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("X-Service", "shop"); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/health": return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n) or b"{}")
            if self.path != "/price": return self._json(404, {"error": "not found"})
            s, c = data.get("subtotal", 0), data.get("coupon", 0)
            if s < 0 or c < 0: return self._json(400, {"error": "negative"})
            p = s * 0.9 if s >= 100 else s
            self._json(200, {"price": round(max(0, p - c), 2), "currency": "USD"})

    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
    """
)

GOLDEN = textwrap.dedent(
    """\
    - name: discount at the threshold
      request: {method: POST, path: /price, json: {subtotal: 100, coupon: 0}}
      expect: {status: 200, json: {price: 90.0}, headers: {X-Service: shop}}
    - name: negative is rejected
      request: {method: POST, path: /price, json: {subtotal: -1}}
      expect: {status: 400, text: negative}
    - name: unknown order is 404
      request: {path: "/orders/{{rand:int:1000:9999}}"}
      expect: {status: 404, schema: {type: object, required: [error]}}
    """
)


def case(expect: dict, **req) -> Case:
    return Case(
        "tests/golden/x.http.yaml",
        "c",
        req.get("method", "GET"),
        req.get("path", "/"),
        {},
        None,
        expect,
    )


def resp(status: int = 200, body: object = None, headers: dict | None = None) -> Response:
    raw = (
        b""
        if body is None
        else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    )
    return Response(status, headers or {"Content-Type": "application/json"}, raw)


# --- pure logic -----------------------------------------------------------------


def test_placeholders_are_deterministic_per_seed():
    a = fill_placeholders("/o/{{rand:int:10:99}}/{{rand:hex:6}}", random.Random("s"))
    b = fill_placeholders("/o/{{rand:int:10:99}}/{{rand:hex:6}}", random.Random("s"))
    c = fill_placeholders("/o/{{rand:int:10:99}}/{{rand:hex:6}}", random.Random("t"))
    assert a == b and a != c
    n, h = a.split("/")[2:]
    assert 10 <= int(n) <= 99 and len(h) == 6 and all(ch in "0123456789abcdef" for ch in h)


def test_check_matches_status_headers_text_json_subset_and_schema():
    r = resp(
        200,
        {"price": 90.0, "currency": "USD"},
        {"Content-Type": "application/json", "X-Service": "shop"},
    )
    assert check(case({"status": 200}), r) == ""
    assert check(case({"status": [200, 201]}), r) == ""
    assert check(case({"status": 404}), r).startswith("status 200")
    assert check(case({"headers": {"x-service": "shop"}}), r) == ""
    assert check(case({"headers": {"X-Service": "other"}}), r).startswith("header")
    assert check(case({"text": "USD"}), r) == ""
    assert check(case({"text": "EUR"}), r).startswith("text")
    assert check(case({"json": {"price": 90}}), r) == ""  # 90 == 90.0
    assert check(case({"json": {"price": 91}}), r).startswith("json")
    assert check(case({"schema": {"type": "object", "required": ["price"]}}), r) == ""
    assert check(case({"schema": {"type": "object", "required": ["total"]}}), r).startswith(
        "schema"
    )
    assert check(case({"json": {}}), resp(200, "not json", {})) == "body is not JSON"


def test_subset_lists_are_exact_length():
    assert check(case({"json": [1, 2]}), resp(200, [1, 2])) == ""
    assert check(case({"json": [1]}), resp(200, [1, 2])).startswith("json")


def test_load_cases_reports_bad_files_and_builds_requests(tmp_path: Path):
    g = tmp_path / "tests" / "golden"
    g.mkdir(parents=True)
    (g / "ok.http.yaml").write_text(GOLDEN)
    (g / "bad.http.yaml").write_text("not: a list\n")
    (g / "broken.http.yaml").write_text(
        "- name: x\n  request: {method: GET}\n  expect: {status: 200}\n"
    )
    cases, errors = load_cases(tmp_path, sorted(g.glob("*.http.yaml")), "seed")
    assert [c.name for c in cases] == [
        "discount at the threshold",
        "negative is rejected",
        "unknown order is 404",
    ]
    assert cases[0].method == "POST" and cases[0].body == b'{"subtotal": 100, "coupon": 0}'
    assert cases[0].headers["Content-Type"] == "application/json"
    assert (
        cases[2].path.startswith("/orders/") and cases[2].path != "/orders/{{rand:int:1000:9999}}"
    )
    assert sorted(e.split(" - ")[0] for e in errors) == [
        "tests/golden/bad.http.yaml",
        "tests/golden/broken.http.yaml",
    ]


def test_run_cases_with_fake_transport_and_fail_fast():
    seen: list[str] = []

    def send(c: Case) -> Response:
        seen.append(c.name)
        if c.name == "boom":
            raise ConnectionError("refused")
        return resp(200, {"ok": True})

    cases = [
        Case("f", "a", "GET", "/", {}, None, {"status": 200}),
        Case("f", "boom", "GET", "/", {}, None, {"status": 200}),
        Case("f", "b", "GET", "/", {}, None, {"status": 500}),
    ]
    results = run_cases(cases, send)
    assert [r.ok for r in results] == [True, False, False]
    assert results[1].reason == "request failed: refused"
    assert results[2].reason.startswith("status 200")
    seen.clear()
    assert [r.ok for r in run_cases(cases, send, fail_fast=True)] == [True, False]
    assert seen == ["a", "boom"]


def test_failed_files_parses_case_and_load_lines():
    run = _TestRun(
        1,
        1,
        1,
        1,
        0,
        0.1,
        "PASSED tests/golden/a.http.yaml::x\nFAILED tests/hidden/b.http.yaml::y - status 500 != 200\nERROR tests/golden/c.http.yaml - case 0: expect is required\nERROR service - health not 2xx\n",
    )
    assert LANG.failed_files(run) == {"tests/hidden/b.http.yaml", "tests/golden/c.http.yaml"}


def test_config_requires_start_and_health(tmp_path: Path):
    from testgate.config import load_config

    (tmp_path / "testgate.yaml").write_text("language: http\npackage: .\nimport_name: app\n")
    with pytest.raises(GateError, match="http.start"):
        load_config(tmp_path, None)
    (tmp_path / "testgate.yaml").write_text(
        "language: http\npackage: .\nimport_name: app\nhttp:\n  start: [python, app.py, '{port}']\n"
        "  health: 'http://127.0.0.1:{port}/health'\n  startup_timeout_s: 5\n  env: {MODE: test}\n"
    )
    cfg = load_config(tmp_path, None)
    assert cfg.http_start == ["python", "app.py", "{port}"] and cfg.http_env == {"MODE": "test"}
    assert LANG.package_dest(cfg) == "app"
    assert LANG.auto_file_ok(Path("tests/auto/x.http.yaml")) and not LANG.auto_file_ok(
        Path("tests/auto/x.yaml")
    )


# --- real service -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "app.py").write_text(SERVICE)
    golden = tmp_path / "tests" / "golden"
    golden.mkdir(parents=True)
    (golden / "shop.http.yaml").write_text(GOLDEN)
    return ProjectConfig(
        root=tmp_path,
        package="svc",
        import_name="app",
        language="http",
        http_start=[sys.executable, "app.py", "{port}"],
        http_health="http://127.0.0.1:{port}/health",
        http_startup_timeout_s=15,
    )


def sandbox_for(cfg: ProjectConfig) -> Sandbox:
    sb = build_sandbox(cfg, sys.executable, {"golden": cfg.root / "tests" / "golden"}, dest="svc")
    sb.env["PATH"] = str(Path(sys.executable).parent) + ":" + sb.env["PATH"]
    LANG.prepare_sandbox(sb, cfg, cfg.root)
    return sb


def test_build_starts_and_stops_the_service(project: ProjectConfig):
    sb = sandbox_for(project)
    try:
        assert "HTTP_PROXY" in sb.env  # network off: best-effort proxy block for the service
        proc = LANG.build(sb, project, 30)
        assert proc.returncode == 0, proc.stderr
        project.http_health = "http://127.0.0.1:{port}/nope"  # never 2xx
        project.http_startup_timeout_s = 2
        assert LANG.build(sb, project, 30).returncode == 1
        project.http_start = [sys.executable, "-c", "raise SystemExit(3)"]
        bad = LANG.build(sb, project, 30)
        assert bad.returncode == 1 and "exited with 3" in bad.stderr
    finally:
        destroy(sb)


def test_run_tests_against_the_service(project: ProjectConfig):
    sb = sandbox_for(project)
    try:
        LANG.build(sb, project, 30)
        run = LANG.run_tests(sb, ["golden"], 60, "seed-1")
        assert run.ok and (run.passed, run.failed, run.errors) == (3, 0, 0), run.output
        assert "PASSED tests/golden/shop.http.yaml::discount at the threshold" in run.output
        # a wrong expectation fails and names the file
        (sb.root / "tests" / "golden" / "bad.http.yaml").write_text(
            "- name: wrong\n  request: {method: POST, path: /price, json: {subtotal: 50}}\n  expect: {status: 200, json: {price: 45}}\n"
        )
        run = LANG.run_tests(sb, ["golden"], 60, "seed-1")
        assert not run.ok and run.failed == 1
        assert LANG.failed_files(run) == {"tests/golden/bad.http.yaml"}
        # file filter and fail-fast
        run = LANG.run_tests(
            sb, [], 60, "seed-1", files=["tests/golden/bad.http.yaml"], fail_fast=True
        )
        assert (run.passed, run.failed) == (0, 1)
        # no fixtures at all is a failure, not a pass
        assert not LANG.run_tests(sb, ["hidden"], 60, "seed-1").ok
    finally:
        destroy(sb)


def test_run_tests_before_build_is_an_error(project: ProjectConfig):
    fresh = get_language("http")
    sb = sandbox_for(project)
    try:
        with pytest.raises(GateError, match="build must run"):
            fresh.run_tests(sb, ["golden"], 10, "s")
    finally:
        destroy(sb)


# --- route coverage (#34) -----------------------------------------------------

from testgate.languages.http import load_routes, match_route

OPENAPI = textwrap.dedent(
    """\
    openapi: 3.0.0
    info: {title: shop, version: "1"}
    paths:
      /health:
        get: {summary: health}
      /price:
        post: {summary: price}
      /orders/{id}:
        get: {summary: one order}
        delete: {summary: cancel}
      /orders/latest:
        get: {summary: latest}
    """
)


def test_load_routes_and_match_prefers_literal_segments():
    routes = load_routes(OPENAPI)
    assert sorted(routes) == [
        "DELETE /orders/{id}",
        "GET /health",
        "GET /orders/latest",
        "GET /orders/{id}",
        "POST /price",
    ]
    assert match_route(routes, "GET", "/orders/42?x=1") == "GET /orders/{id}"
    assert match_route(routes, "GET", "/orders/latest") == "GET /orders/latest"
    assert match_route(routes, "get", "/health") == "GET /health"
    assert match_route(routes, "PUT", "/price") is None
    assert match_route(routes, "GET", "/orders/1/items") is None
    with pytest.raises(GateError, match="paths"):
        load_routes("openapi: 3.0.0\n")


def test_run_coverage_counts_routes_hit(project: ProjectConfig):
    (project.root / "svc" / "openapi.yaml").write_text(OPENAPI)
    project.http_openapi = "openapi.yaml"
    sb = sandbox_for(project)
    try:
        LANG.build(sb, project, 30)
        run, data = LANG.run_coverage(sb, ["golden"], 60, "seed", "svc")
        assert run.ok
        hit = {k for k, v in data.covered.items() if v}
        # golden fixtures: POST /price twice, GET /orders/<n> once
        assert hit == {"openapi.yaml#POST /price", "openapi.yaml#GET /orders/{id}"}
        assert len(data.statements) == 5 and data.total() == 0.4
    finally:
        destroy(sb)


def test_changed_units_are_routes_whose_spec_changed(project: ProjectConfig, tmp_path: Path):
    import subprocess

    from testgate.core import RunContext

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@x", "-c", "user.name=t", *args],
            cwd=project.root,
            check=True,
            capture_output=True,
        )

    spec = project.root / "svc" / "openapi.yaml"
    spec.write_text(OPENAPI)
    project.http_openapi = "openapi.yaml"
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    # the PR: a new route and a changed definition of POST /price
    spec.write_text(
        OPENAPI.replace("post: {summary: price}", "post: {summary: price, deprecated: true}")
        + "  /refunds:\n    post: {summary: refund}\n"
    )
    ctx = RunContext(project, "main", [], None, "s", False, sys.executable, language=LANG)
    changed = LANG.changed_units(ctx)
    assert sorted(c.path for c in changed) == [
        "openapi.yaml#POST /price",
        "openapi.yaml#POST /refunds",
    ]
    ctx.base_ref = None
    assert LANG.changed_units(ctx) is None


# --- differential gate (#35) ---------------------------------------------------

from testgate.languages.http import same_response, unchanged_cases


def test_same_response_compares_json_structurally_and_text_exactly():
    assert same_response(resp(200, {"a": 1, "b": 2}), Response(200, {}, b'{"b": 2, "a": 1}'))
    assert not same_response(resp(200, {"a": 1}), resp(201, {"a": 1}))
    assert not same_response(resp(200, {"a": 1}), resp(200, {"a": 2}))
    assert same_response(resp(200, "plain", {}), resp(200, "plain", {}))
    assert not same_response(resp(200, "plain", {}), resp(200, "plain ", {}))


def test_unchanged_cases_drop_changed_and_unknown_routes():
    routes = load_routes(OPENAPI)
    cases = [
        Case("f", "p", "POST", "/price", {}, None, {}),
        Case("f", "o", "GET", "/orders/5", {}, None, {}),
        Case("f", "x", "GET", "/nowhere", {}, None, {}),
    ]
    pairs = unchanged_cases(cases, routes, {"POST /price"})
    assert [(c.name, r) for c, r in pairs] == [("o", "GET /orders/{id}")]


def test_survivor_labels_keep_hidden_case_names_private():
    g = Case("tests/golden/a.http.yaml", "golden name", "GET", "/", {}, None, {})
    h = Case("tests/hidden/b.http.yaml", "secret name", "GET", "/", {}, None, {})
    assert LANG._survivor(g, "GET /", 0) == {
        "route": "GET /",
        "case": "golden name",
        "file": "golden",
    }
    assert LANG._survivor(h, "GET /", 3) == {
        "route": "GET /",
        "case": "hidden case 3",
        "file": "hidden",
    }


# --- docker compose start variant (#37) ------------------------------------------

from testgate.languages.http import compose_cmd


def test_compose_config_and_command(tmp_path: Path):
    from testgate.config import load_config

    (tmp_path / "testgate.yaml").write_text(
        "language: http\npackage: svc\nimport_name: app\nhttp:\n"
        "  start: {compose: compose.yaml, service: shop, port: 8000}\n"
        "  health: 'http://127.0.0.1:{port}/health'\n"
    )
    cfg = load_config(tmp_path, None)
    assert (cfg.http_compose, cfg.http_compose_service, cfg.http_compose_port) == (
        "compose.yaml",
        "shop",
        8000,
    )
    assert cfg.http_start == []
    assert compose_cmd(cfg, "testgate-abc", "up", "-d") == [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-p",
        "testgate-abc",
        "up",
        "-d",
    ]
    for bad in (
        "  start: {compose: compose.yaml, service: shop}\n",
        "  start: {compose: compose.yaml, service: shop, port: '8000'}\n",
        "  start: {compose: compose.yaml, service: shop, port: 8000, extra: 1}\n",
    ):
        (tmp_path / "testgate.yaml").write_text(
            "language: http\npackage: svc\nimport_name: app\nhttp:\n"
            + bad
            + "  health: 'http://127.0.0.1:{port}/h'\n"
        )
        with pytest.raises(GateError, match="http.start"):
            load_config(tmp_path, None)


def test_compose_start_without_docker_fails_cleanly(
    project: ProjectConfig, monkeypatch: pytest.MonkeyPatch
):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    project.http_compose, project.http_compose_service, project.http_compose_port = (
        "compose.yaml",
        "shop",
        8000,
    )
    project.http_start = []
    sb = sandbox_for(project)
    try:
        proc = LANG.build(sb, project, 30)
        assert proc.returncode == 1 and "docker not found" in proc.stderr
        assert proc.args[:2] == ["docker", "compose"]
    finally:
        destroy(sb)
