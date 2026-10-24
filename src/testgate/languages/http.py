"""HTTP black-box adapter (ADR 0002).

The service under test starts as a subprocess from the sandbox copy; golden,
auto and hidden "tests" are ``*.http.yaml`` fixture files replayed by the
runner in this module. Fixture loading, placeholder filling and expectation
matching are pure and take an injectable ``send``, so they are testable
without a socket. Only ``_Service`` and ``_send_http`` touch the network.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema
import yaml

from testgate import gitinfo, sandbox
from testgate.core import (
    ChangedFile,
    CoverageData,
    GateError,
    GateResult,
    Mutant,
    ProjectConfig,
    RunContext,
    Sandbox,
    Status,
    TestRun,
)

FIXTURE_GLOB = "*.http.yaml"
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:::|\s|$)", re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{rand:(int|hex):([^}]*)\}\}")


# --- fixtures -----------------------------------------------------------------


@dataclass
class Case:
    file: str  # sandbox-relative fixture file
    name: str
    method: str
    path: str
    headers: dict[str, str]
    body: bytes | None
    expect: dict[str, Any]


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Send = Callable[[Case], Response]


@dataclass
class CaseResult:
    case: Case
    ok: bool
    reason: str = ""
    response: Response | None = None


@dataclass
class FixtureRun:
    results: list[CaseResult] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)  # "file: message"

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def fill_placeholders(text: str, rng: random.Random) -> str:
    """``{{rand:int:a:b}}`` and ``{{rand:hex:n}}`` drawn from ``rng``."""

    def one(m: re.Match[str]) -> str:
        kind, args = m.group(1), m.group(2).split(":")
        if kind == "int":
            lo, hi = int(args[0]), int(args[1])
            return str(rng.randint(lo, hi))
        return "".join(rng.choice("0123456789abcdef") for _ in range(int(args[0])))

    return _PLACEHOLDER.sub(one, text)


def load_cases(root: Path, files: list[Path], seed: str) -> tuple[list[Case], list[str]]:
    """Parse fixture files; placeholders are filled per case from the seed."""
    cases: list[Case] = []
    errors: list[str] = []
    for file in files:
        rel = file.relative_to(root).as_posix()
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{rel} - {exc}")
            continue
        if not isinstance(raw, list):
            errors.append(f"{rel} - a fixture file is a list of cases")
            continue
        for i, item in enumerate(raw):
            try:
                cases.append(_parse_case(rel, i, item, seed))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{rel} - case {i}: {exc}")
    return cases, errors


def _parse_case(rel: str, index: int, item: Any, seed: str) -> Case:
    if not isinstance(item, dict):
        raise TypeError("a case is a mapping")
    name = str(item.get("name") or f"case {index}")
    rng = random.Random(f"{seed}:{rel}:{name}")
    req = item.get("request")
    if not isinstance(req, dict) or "path" not in req:
        raise ValueError("request.path is required")
    expect = item.get("expect")
    if not isinstance(expect, dict):
        raise TypeError("expect is required and must be a mapping")
    text = fill_placeholders(json.dumps(req), rng)
    req = json.loads(text)
    method = str(req.get("method", "GET")).upper()
    path = str(req["path"])
    if req.get("query"):
        from urllib.parse import urlencode

        path += ("&" if "?" in path else "?") + urlencode(req["query"])
    headers = {str(k): str(v) for k, v in (req.get("headers") or {}).items()}
    body: bytes | None = None
    if "json" in req:
        body = json.dumps(req["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif "body" in req:
        body = str(req["body"]).encode()
    return Case(rel, name, method, path, headers, body, expect)


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_subset(e, a) for e, a in zip(expected, actual, strict=True))
        )
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return float(expected) == float(actual)
        except (TypeError, ValueError):
            return False
    return bool(expected == actual)


def check(case: Case, resp: Response) -> str:
    """Empty string when the response meets ``case.expect``, else the first mismatch."""
    exp = case.expect
    if "status" in exp:
        allowed = exp["status"] if isinstance(exp["status"], list) else [exp["status"]]
        if resp.status not in [int(s) for s in allowed]:
            return f"status {resp.status} != {exp['status']}"
    lower = {k.lower(): v for k, v in resp.headers.items()}
    for k, v in (exp.get("headers") or {}).items():
        if lower.get(str(k).lower()) != str(v):
            return f"header {k}: {lower.get(str(k).lower())!r} != {v!r}"
    if "text" in exp and str(exp["text"]) not in resp.text():
        return f"text {exp['text']!r} not in body"
    if "json" in exp or "schema" in exp:
        try:
            data = json.loads(resp.body or b"null")
        except ValueError:
            return "body is not JSON"
        if "json" in exp and not _subset(exp["json"], data):
            return f"json {json.dumps(exp['json'])} not a subset of {json.dumps(data)[:200]}"
        if "schema" in exp:
            try:
                jsonschema.validate(data, exp["schema"])
            except jsonschema.ValidationError as e:
                return f"schema: {e.message}"
    return ""


def run_cases(cases: list[Case], send: Send, fail_fast: bool = False) -> list[CaseResult]:
    out: list[CaseResult] = []
    for case in cases:
        try:
            resp = send(case)
        except Exception as exc:  # noqa: BLE001 - any transport failure is a failed case
            out.append(CaseResult(case, False, f"request failed: {exc}"))
        else:
            reason = check(case, resp)
            out.append(CaseResult(case, not reason, reason, resp))
        if fail_fast and not out[-1].ok:
            break
    return out


# --- routes (OpenAPI) ---------------------------------------------------------

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_TEMPLATE_PARAM = re.compile(r"\{[^/}]+\}")


def load_routes(text: str) -> dict[str, Any]:
    """``{"POST /price": <operation object>}`` from an OpenAPI document (YAML or JSON)."""
    doc = yaml.safe_load(text) or {}
    paths = doc.get("paths") if isinstance(doc, dict) else None
    if not isinstance(paths, dict):
        raise GateError("openapi document has no 'paths' mapping")
    routes: dict[str, Any] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in _METHODS:
            if method in item:
                routes[f"{method.upper()} {path}"] = item[method]
    return routes


def match_route(routes: dict[str, Any], method: str, path: str) -> str | None:
    """The route id a concrete request hits; literal segments win over templated ones."""
    concrete = path.split("?", 1)[0]
    best: tuple[int, str] | None = None
    for route in routes:
        rm, template = route.split(" ", 1)
        if rm != method.upper():
            continue
        pattern = (
            "^"
            + _TEMPLATE_PARAM.sub(
                "[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
            )
            + "$"
        )
        if re.match(pattern, concrete):
            literal = template.count("/") - len(_TEMPLATE_PARAM.findall(template))
            if best is None or literal > best[0]:
                best = (literal, route)
    return best[1] if best else None


def _route_key(openapi: str, route: str) -> str:
    return f"{openapi}#{route}"


def _spec_rel(cfg: ProjectConfig) -> str:
    """The OpenAPI document's repo-relative path (http.openapi is package-relative)."""
    assert cfg.http_openapi is not None
    return f"{cfg.package}/{cfg.http_openapi}" if cfg.package not in (".", "") else cfg.http_openapi


def _base_spec(cfg: ProjectConfig, base_ref: Any) -> str | None:
    """The OpenAPI text at the base ref, or None (no ref, first adoption, no spec there)."""
    if not cfg.http_openapi or not base_ref or base_ref == "worktree":
        return None
    return gitinfo.show_file(cfg.root, str(base_ref), _spec_rel(cfg))


def merged_routes(base_text: str | None, head_text: str | None) -> tuple[dict[str, Any], set[str]]:
    """(routes, added) with the base spec in charge (#66).

    Every base route keeps its base definition, whatever the change did to it; the
    change can only add routes. ``added`` are the routes the base spec lacks.
    """
    base = load_routes(base_text) if base_text is not None else {}
    head = load_routes(head_text) if head_text is not None else {}
    added = {r for r in head if r not in base}
    return {**{r: head[r] for r in added}, **base}, added


# --- differential (#35) ---------------------------------------------------------


def same_response(a: Response, b: Response) -> bool:
    """Equal status and body; JSON bodies compare structurally so key order does not matter."""
    if a.status != b.status:
        return False
    try:
        return bool(json.loads(a.body or b"null") == json.loads(b.body or b"null"))
    except ValueError:
        return a.body == b.body


def unchanged_cases(
    cases: list[Case], routes: dict[str, Any], changed: set[str]
) -> list[tuple[Case, str]]:
    """(case, route) pairs whose route is in the document and not among the changed ones."""
    out: list[tuple[Case, str]] = []
    for case in cases:
        route = match_route(routes, case.method, case.path)
        if route is not None and route not in changed:
            out.append((case, route))
    return out


# --- transport and service ----------------------------------------------------


def _send_http(base: str, timeout_s: float) -> Send:
    def send(case: Case) -> Response:
        req = urllib.request.Request(
            base + case.path, data=case.body, headers=case.headers, method=case.method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return Response(r.status, dict(r.headers.items()), r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers.items()), e.read())

    return send


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")


_OVERRIDE = "compose.testgate.yaml"  # written next to the compose file by prepare_sandbox


def compose_cmd(cfg: ProjectConfig, project: str, *args: str) -> list[str]:
    """``docker compose -f <file> -f <override> -p <project> <args>``; files are package-relative."""
    assert cfg.http_compose is not None
    override = str(PurePosixPath(cfg.http_compose).parent / _OVERRIDE)
    return ["docker", "compose", "-f", cfg.http_compose, "-f", override, "-p", project, *args]


# --- compose safety (#57) -------------------------------------------------------

_HOST_MODES = ("pid", "ipc", "uts", "userns_mode", "cgroup")
_FORBIDDEN_KEYS = ("privileged", "cap_add", "devices", "network_mode", "networks", "extends")


def _is_dockerfile(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name.startswith("dockerfile") or name.endswith(".dockerfile")


def compose_problems(text: str, service: str, port: int, package_dir: Path) -> list[str]:
    """Reasons the compose file could reach the host; empty when it is safe to run.

    The service under test and its helpers run on the runner's Docker daemon, so any
    of these would hand the change root on the runner, and the secrets with it.
    """
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return [f"compose file is not YAML: {exc}"]
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not services:
        return ["compose file has no services"]
    problems: list[str] = []
    if "docker.sock" in text:
        problems.append("mounts the Docker socket")
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for key in _FORBIDDEN_KEYS:
            if key in svc and svc[key] not in (False, None, [], {}):
                problems.append(f"service {name}: {key} is not allowed")
        for key in _HOST_MODES:
            if str(svc.get(key, "")).startswith("host"):
                problems.append(f"service {name}: {key}: host is not allowed")
        for opt in svc.get("security_opt") or []:
            if "unconfined" in str(opt):
                problems.append(f"service {name}: security_opt {opt} is not allowed")
        for vol in svc.get("volumes") or []:
            source, kind = _volume_source(vol)
            if kind == "bind" and not _inside(package_dir, source):
                problems.append(f"service {name}: bind mount outside the project: {source}")
        for spec in svc.get("ports") or []:
            if name != service:
                problems.append(f"service {name}: only {service} may publish ports")
            elif _container_port(spec) != port:
                problems.append(f"service {name}: publishes {spec}; only {port} is allowed")
    for vname, vol in (doc.get("volumes") or {}).items():
        opts = vol.get("driver_opts") if isinstance(vol, dict) else None
        if isinstance(opts, dict) and opts:
            problems.append(f"volume {vname}: driver_opts are not allowed")
    return problems


def _volume_source(vol: Any) -> tuple[str, str]:
    if isinstance(vol, dict):
        source = str(vol.get("source", ""))
        return source, str(vol.get("type", "volume"))
    source = str(vol).split(":", 1)[0]
    bind = source.startswith(("/", ".", "~", "$")) or "/" in source
    return source, "bind" if bind else "volume"


def _inside(package_dir: Path, source: str) -> bool:
    if source.startswith(("~", "$")):
        return False
    target = (package_dir / source).resolve()
    return target == package_dir.resolve() or package_dir.resolve() in target.parents


def _container_port(spec: Any) -> int | None:
    if isinstance(spec, dict):
        value = spec.get("target")
    else:
        value = str(spec).rsplit(":", 1)[-1].split("/", 1)[0]
    try:
        return int(str(value))
    except ValueError:
        return None


def _override(services: list[str], service: str) -> str:
    """testgate owns networking: helpers sit on an internal network only (no egress);
    the service under test joins it too, plus the default network its port needs."""
    nets = {
        s: ["default", "testgate_internal"] if s == service else ["testgate_internal"]
        for s in services
    }
    doc = {
        "networks": {"testgate_internal": {"internal": True}},
        "services": {s: {"networks": n} for s, n in nets.items()},
    }
    return yaml.safe_dump(doc, sort_keys=True)


class _Service:
    """The service under test: a subprocess from ``cwd`` killed with its process group,
    or a docker compose project when the config uses the compose form."""

    def __init__(self, cfg: ProjectConfig, cwd: Path, env: dict[str, str], port: int, log: Path):
        self.cfg = cfg
        self.cwd = cwd
        self.env = {**env, "PORT": str(port)}
        self.port = port
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None
        self.project: str | None = None  # compose project name once started

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout_s: int) -> tuple[bool, str]:
        if self.cfg.http_compose:
            ok, why = self._start_compose(timeout_s)
            if not ok:
                return False, why
        else:
            argv = [a.replace("{port}", str(self.port)) for a in self.cfg.http_start]
            with self.log.open("ab") as out:
                self.proc = subprocess.Popen(
                    argv, cwd=self.cwd, env=self.env, stdout=out, stderr=out, start_new_session=True
                )
        health = self.cfg.http_health.replace("{port}", str(self.port))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False, f"service exited with {self.proc.returncode} before becoming healthy"
            try:
                with urllib.request.urlopen(health, timeout=2) as r:
                    if 200 <= r.status < 300:
                        return True, ""
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.2)
        return False, f"health {health} not 2xx within {timeout_s}s"

    def _docker_env(self) -> dict[str, str]:
        # the docker CLI talks to the daemon over a socket; the sandbox's dead proxy must not apply
        return {k: v for k, v in self.env.items() if k not in _PROXY_VARS}

    def _compose(self, *args: str, timeout_s: int = 600) -> subprocess.CompletedProcess[str]:
        assert self.project is not None
        proc = subprocess.run(
            compose_cmd(self.cfg, self.project, *args),
            cwd=self.cwd,
            env=self._docker_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        with self.log.open("a", encoding="utf-8") as out:
            out.write(f"$ {' '.join(proc.args)}\n{proc.stdout}{proc.stderr}")
        return proc

    def _start_compose(self, timeout_s: int) -> tuple[bool, str]:
        if shutil.which("docker") is None:
            return False, "docker not found on PATH"
        self.project = f"testgate-{random.Random().randrange(16**8):08x}"
        up = self._compose("up", "-d", "--build", "--remove-orphans", timeout_s=max(timeout_s, 60))
        if up.returncode != 0:
            return False, f"docker compose up failed: {up.stderr.strip()[-300:]}"
        mapped = self._compose(
            "port", self.cfg.http_compose_service, str(self.cfg.http_compose_port)
        )
        host_port = mapped.stdout.strip().rsplit(":", 1)[-1]
        if mapped.returncode != 0 or not host_port.isdigit():
            return False, f"docker compose port gave no mapping: {mapped.stdout.strip()!r}"
        self.port = int(host_port)
        return True, ""

    def stop(self) -> None:
        if self.project is not None:
            self._compose("logs", "--no-color")
            self._compose("down", "-v", "--remove-orphans")
            self.project = None
            return
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)

    def output(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def ctx_path(ctx: RunContext, sb: Sandbox) -> str:
    """PATH for a service sandbox: the interpreter's directory first, then the sandbox default."""
    return f"{Path(ctx.python).parent}:{sb.env.get('PATH', '')}"


# --- the adapter ----------------------------------------------------------------


class HttpLanguage:
    name = "http"
    # the service may be written in anything; these are what the hygiene scan reads
    source_suffixes: tuple[str, ...] = (".py", ".js", ".mjs", ".ts", ".go", ".rb", ".rs", ".java")
    test_glob: str = FIXTURE_GLOB
    detection_tokens: tuple[str, ...] = ("TESTGATE_",)
    _record: Callable[[Case], None] | None = None  # set by run_coverage to observe requests

    def package_dest(self, cfg: ProjectConfig) -> str:
        return "app" if cfg.package in (".", "") else cfg.package

    def prepare_sandbox(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None:
        sb.env.update(cfg.http_env)
        if cfg.http_compose:
            self._trusted_compose(sb, cfg, project_root)
        if not sb.network:
            # best effort for a process we do not control: proxy-aware clients hit a dead port
            sb.env.update(
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "https_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                }
            )

    def _trusted_compose(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None:
        """Replace the compose file and every Dockerfile with their base-ref copies (#57).

        The change still supplies the code that goes into the image; it cannot change
        how the image is built or how the container runs. Problems are kept for build().
        """
        assert cfg.http_compose is not None
        source = cfg.raw.get("_source")
        if source and source != "worktree":
            pkg = cfg.package.strip("/")
            prefix = "" if pkg in ("", ".") else pkg + "/"
            for rel in [cfg.http_compose] + [
                f[len(prefix) :]
                for f in gitinfo.list_files(project_root, str(source), pkg or ".")
                if _is_dockerfile(f)
            ]:
                text = gitinfo.show_file(project_root, str(source), prefix + rel)
                target = sb.src_root / rel
                if text is None:
                    target.unlink(missing_ok=True)  # the base ref has no such file
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
        compose = sb.src_root / cfg.http_compose
        text = compose.read_text(encoding="utf-8") if compose.is_file() else ""
        self._compose_problems = compose_problems(
            text, cfg.http_compose_service, cfg.http_compose_port, sb.src_root
        )
        doc = yaml.safe_load(text) if text and not self._compose_problems else None
        services = list((doc or {}).get("services") or {})
        (compose.parent / _OVERRIDE).write_text(
            _override(services, cfg.http_compose_service), encoding="utf-8"
        )

    def auto_file_ok(self, path: Path) -> bool:
        return path.name.endswith(".http.yaml")

    def _service(self, sb: Sandbox) -> _Service:
        return _Service(self._cfg, sb.src_root, sb.env, free_port(), sb.root / "service.log")

    def build(
        self, sb: Sandbox, cfg: ProjectConfig, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        self._cfg = cfg
        problems = getattr(self, "_compose_problems", []) if cfg.http_compose else []
        if problems:
            argv = compose_cmd(cfg, "<project>", "up")
            return subprocess.CompletedProcess(argv, 1, "", "; ".join(problems))
        svc = self._service(sb)
        try:
            ok, why = svc.start(min(timeout_s, cfg.http_startup_timeout_s))
        finally:
            svc.stop()
        argv = (
            compose_cmd(cfg, "<project>", "up")
            if cfg.http_compose
            else [a.replace("{port}", str(svc.port)) for a in cfg.http_start]
        )
        return subprocess.CompletedProcess(argv, 0 if ok else 1, svc.output(), why)

    def _files(self, sb: Sandbox, names: list[str], files: list[str] | None) -> list[Path]:
        if files:
            return [sb.root / f for f in files]
        out: list[Path] = []
        for n in names:
            out.extend(sorted((sb.root / "tests" / n).rglob(FIXTURE_GLOB)))
        return out

    def run_tests(
        self,
        sb: Sandbox,
        names: list[str],
        timeout_s: int,
        seed: str,
        files: list[str] | None = None,
        fail_fast: bool = False,
    ) -> TestRun:
        cfg = getattr(self, "_cfg", None)
        if cfg is None:
            raise GateError("http adapter: build must run before tests")
        t0 = time.monotonic()
        cases, load_errors = load_cases(sb.root, self._files(sb, names, files), seed)
        lines = [f"ERROR {e}" for e in load_errors]
        results: list[CaseResult] = []
        service_error = ""
        svc = self._service(sb)
        try:
            ok, why = svc.start(cfg.http_startup_timeout_s)
            if not ok:
                service_error = why
                lines.append(f"ERROR service - {why}")
            else:
                per_request = max(1.0, min(30.0, timeout_s / max(1, len(cases))))
                send = _send_http(svc.base, per_request)
                record = getattr(self, "_record", None)
                if record is not None:

                    def send(case: Case, _inner: Send = send, _rec: Any = record) -> Response:
                        _rec(case)
                        return _inner(case)

                results = run_cases(cases, send, fail_fast)
        finally:
            svc.stop()
        for r in results:
            tag = "PASSED" if r.ok else "FAILED"
            lines.append(
                f"{tag} {r.case.file}::{r.case.name}" + (f" - {r.reason}" if r.reason else "")
            )
        passed = sum(1 for r in results if r.ok)
        failed = len(results) - passed
        errors = len(load_errors) + (1 if service_error else 0)
        exit_code = 0 if (results and failed == 0 and errors == 0) else 1
        output = "\n".join(lines) + "\n--- service log ---\n" + svc.output()
        return TestRun(exit_code, passed, failed, errors, 0, time.monotonic() - t0, output)

    def run_coverage(
        self, sb: Sandbox, names: list[str], timeout_s: int, seed: str, repo_package: str
    ) -> tuple[TestRun, CoverageData]:
        """Route coverage: every route in the OpenAPI document is a statement, hit ones are covered.

        Keys are ``<openapi path>#<METHOD> <template>`` with a single line 1, so the
        coverage gate's math and report work unchanged.
        """
        cfg = getattr(self, "_cfg", None)
        if cfg is None:
            raise GateError("http adapter: build must run before coverage")
        if not cfg.http_openapi:
            return self.run_tests(sb, names, timeout_s, seed), CoverageData()
        base_text = _base_spec(cfg, cfg.raw.get("_source"))
        doc = sb.src_root / cfg.http_openapi
        if not doc.is_file() and base_text is None:
            raise GateError(f"http.openapi not found in the package: {cfg.http_openapi}")
        head_text = doc.read_text(encoding="utf-8") if doc.is_file() else None
        routes, _ = merged_routes(base_text, head_text)
        hits: set[str] = set()
        self._record = lambda case: hits.add(match_route(routes, case.method, case.path) or "")
        try:
            run = self.run_tests(sb, names, timeout_s, seed)
        finally:
            self._record = None
        data = CoverageData()
        for route in routes:
            key = _route_key(cfg.http_openapi, route)
            data.statements[key] = {1}
            data.covered[key] = {1} if route in hits else set()
        return run, data

    def changed_units(self, ctx: RunContext) -> list[ChangedFile] | None:
        """Routes that count as changed: only routes the base spec lacks (#66).

        Editing or deleting a base route's definition no longer marks it changed, so
        a change cannot exempt routes from the differential gate by touching the spec;
        the mutation gate reports such an edit for human review instead.
        """
        cfg = ctx.config
        if ctx.base_ref is None or not cfg.http_openapi:
            return None
        now_file = cfg.root / _spec_rel(cfg)
        if not now_file.is_file():
            return None
        base_text = _base_spec(cfg, ctx.base_ref)
        _, added = merged_routes(base_text, now_file.read_text(encoding="utf-8"))
        return [ChangedFile(_route_key(cfg.http_openapi, r), {1}) for r in sorted(added)]

    def spec_changed(self, ctx: RunContext) -> bool:
        """True when the change edits the OpenAPI document the base ref has."""
        cfg = ctx.config
        if ctx.base_ref is None or not cfg.http_openapi:
            return False
        base = _base_spec(cfg, ctx.base_ref)
        now_file = cfg.root / _spec_rel(cfg)
        now = now_file.read_text(encoding="utf-8") if now_file.is_file() else None
        return base is not None and now != base

    def generate_mutants(
        self, path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]:
        return []  # mutation_gate replaces mutation for http mode

    def mutation_gate(self, ctx: RunContext) -> GateResult:
        """Differential run: fixtures on unchanged routes must get identical answers from
        the base ref's service and the head's. A difference is a surviving "mutant"."""
        result = self._differential(ctx)
        if self.spec_changed(ctx):
            # judged against the base spec; the edit itself is a contract change (#66)
            result.public["spec_changed"] = True
            result.summary += "; the OpenAPI spec changed: a human must review it"
        return result

    def _differential(self, ctx: RunContext) -> GateResult:
        cfg = ctx.config
        name = "mutation"
        if ctx.base_ref is None:
            return GateResult(name, Status.SKIP, "skipped: no base ref")
        if not cfg.http_openapi:
            return GateResult(
                name, Status.SKIP, "skipped: no openapi document to tell changed routes"
            )
        changed_units = self.changed_units(ctx) or []
        changed = {c.path.split("#", 1)[1] for c in changed_units}
        tmp = Path(tempfile.mkdtemp(prefix="testgate-base-"))
        head: _Service | None = None
        base: _Service | None = None
        boxes: list[Sandbox] = []
        try:
            present = gitinfo.archive(cfg.root, ctx.base_ref, [cfg.package], tmp)
            if cfg.package not in present:
                return GateResult(name, Status.SKIP, "skipped: package absent at base ref")
            dirs = {"golden": cfg.root / cfg.golden}
            if ctx.hidden_dir is not None:
                dirs["hidden"] = ctx.hidden_dir
            dirs = {k: v for k, v in dirs.items() if v.is_dir()}
            head_sb = sandbox.build_sandbox(cfg, ctx.python, dirs, dest=self.package_dest(cfg))
            base_cfg = dataclasses.replace(cfg, root=tmp)
            base_sb = sandbox.build_sandbox(base_cfg, ctx.python, {}, dest=self.package_dest(cfg))
            boxes = [head_sb, base_sb]
            for sb in boxes:
                sb.env["PATH"] = ctx_path(ctx, sb)
                self.prepare_sandbox(sb, cfg, cfg.root)
            routes, _ = merged_routes(
                _base_spec(cfg, ctx.base_ref),
                (head_sb.src_root / cfg.http_openapi).read_text(encoding="utf-8"),
            )
            cases, _ = load_cases(head_sb.root, self._files(head_sb, list(dirs), None), ctx.seed)
            pairs = unchanged_cases(cases, routes, changed)
            if not pairs:
                return GateResult(
                    name,
                    Status.SKIP,
                    "skipped: no fixture targets an unchanged route",
                    {"changed_routes": len(changed)},
                )
            self._cfg = cfg
            head = self._service(head_sb)
            base = self._service(base_sb)
            ok, why = head.start(cfg.http_startup_timeout_s)
            if not ok:
                return GateResult(
                    name, Status.ERROR, f"head service: {why}", {}, {"log": head.output()}
                )
            ok, why = base.start(cfg.http_startup_timeout_s)
            if not ok:
                return GateResult(
                    name,
                    Status.SKIP,
                    "skipped: base ref service never became healthy",
                    {},
                    {"log": base.output()},
                )
            send_head = _send_http(head.base, 30)
            send_base = _send_http(base.base, 30)
            survivors: list[dict[str, Any]] = []
            private: list[dict[str, Any]] = []
            identical = 0
            for i, (case, route) in enumerate(pairs):
                try:
                    a, b = send_head(case), send_base(case)
                except OSError as exc:
                    private.append(
                        {"case": self._survivor(case, route, i)["case"], "route": route}
                        | {"error": str(exc)}
                    )
                    survivors.append(self._survivor(case, route, i))
                    continue
                same = same_response(a, b)
                identical += same
                entry: dict[str, Any] = {
                    "case": self._survivor(case, route, i)["case"],
                    "route": route,
                    "identical": same,
                }
                if not case.file.startswith("tests/hidden/"):
                    # hidden fixtures stay out of the log: no file, request or body (#51)
                    entry |= {
                        "file": case.file,
                        "head": [a.status, a.text()[:500]],
                        "base": [b.status, b.text()[:500]],
                    }
                private.append(entry)
                if not same:
                    survivors.append(self._survivor(case, route, i))
            score = identical / len(pairs)
            public = {
                "compared": len(pairs),
                "identical": identical,
                "score": round(score, 4),
                "changed_routes": len(changed),
                "survivors": survivors[: cfg.mutation_max_shown],
            }
            summary = f"differential: {identical}/{len(pairs)} identical on unchanged routes (score {score:.2f})"
            if score < cfg.mutation_min_score:
                return GateResult(
                    name,
                    Status.FAIL,
                    f"{summary}, below {cfg.mutation_min_score:.2f}",
                    public,
                    {"cases": private},
                )
            return GateResult(name, Status.PASS, summary, public, {"cases": private})
        finally:
            for svc in (head, base):
                if svc is not None:
                    svc.stop()
            for box in boxes:
                sandbox.destroy(box)
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _survivor(case: Case, route: str, index: int) -> dict[str, Any]:
        # hidden case names stay private: identify hidden cases by position only
        label = case.name if case.file.startswith("tests/golden/") else f"hidden case {index}"
        return {"route": route, "case": label, "file": case.file.split("/", 2)[1]}

    def failed_files(self, run: TestRun) -> set[str]:
        return {m.group(1) for m in _FAILED_LINE.finditer(run.output) if m.group(1) != "service"}
