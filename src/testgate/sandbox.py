"""Fresh temp root holding only the package and chosen tests, with a scrubbed env."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from testgate.core import ProjectConfig, Sandbox


def _ignore_tests(_dir: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n in ("conftest.py", "__pycache__", ".pytest_cache") or n.startswith(".")
    }


def _ignore_package(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__"}


def build_sandbox(
    config: ProjectConfig,
    python: str,
    tests: dict[str, Path],
    overrides: dict[str, str] | None = None,
    dest: str | None = None,
) -> Sandbox:
    # resolve(): macOS puts temp dirs behind the /var -> /private/var symlink, and tools that
    # realpath imported files (vitest coverage) would otherwise miss the include glob.
    root = Path(tempfile.mkdtemp(prefix="testgate-")).resolve()
    src_root = root / (dest if dest is not None else f"src/{config.import_name}")
    shutil.copytree(config.root / config.package, src_root, ignore=_ignore_package)

    test_dirs: dict[str, Path] = {}
    for name, src in tests.items():
        dst = root / "tests" / name
        shutil.copytree(src, dst, ignore=_ignore_tests)
        test_dirs[name] = dst

    for rel_path, text in (overrides or {}).items():
        # "<config.package>/x.py" -> "<root>/<dest>/x.py"
        rel = Path(rel_path).relative_to(config.package)
        target = src_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    tmp = root / "tmp"
    tmp.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "PYTHONPATH": f"{root}:{root / 'src'}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp),
    }

    return Sandbox(
        root=root,
        src_root=src_root,
        test_dirs=test_dirs,
        python=python,
        env=env,
        network=config.network,
    )


# Process groups started by run_in_sandbox and not yet killed, keyed by sandbox root,
# so destroy() and cli's final reap can kill anything still running (#54).
_GROUPS: dict[str, set[int]] = {}
_GROUPS_LOCK = threading.Lock()
_DRAIN_S = 5


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _forget(key: str, pgid: int) -> None:
    with _GROUPS_LOCK:
        groups = _GROUPS.get(key)
        if groups is not None:
            groups.discard(pgid)
            if not groups:
                del _GROUPS[key]


# --- OS isolation (#56) -----------------------------------------------------------

_ISOLATION: list[str | None] = []  # cached bwrap path, or None when unusable


def isolation_available() -> bool:
    """True when sandbox children run under bubblewrap (Linux with a working bwrap)."""
    return _bwrap() is not None


def _bwrap() -> str | None:
    if not _ISOLATION:
        _ISOLATION.append(_probe())
    return _ISOLATION[0]


def _probe() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    path = shutil.which("bwrap")
    if path is None:
        return None
    argv = [path, "--ro-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc"]
    try:
        proc = subprocess.run([*argv, "true"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if proc.returncode == 0 else None


def _masked() -> list[str]:
    """Directories a sandbox child must not read: the real home, the checkout (which
    holds the hidden tests and git config) and the runner's temp and work dirs."""
    paths = {str(Path.home()), os.getcwd(), "/tmp", "/var/tmp"}
    for var in ("GITHUB_WORKSPACE", "RUNNER_TEMP", "RUNNER_WORKSPACE"):
        if os.environ.get(var):
            paths.add(os.environ[var])
    return sorted(p for p in paths if p not in ("/", "") and os.path.isdir(p))


def _toolchain_dirs(python: str) -> list[str]:
    """Interpreter prefixes that may sit under a masked dir (a venv, uv's pythons).

    Every hop of the interpreter's symlink chain counts: a venv's python links to an
    alias dir (uv: cpython-3.13-<platform>) that itself links to the real install.
    """
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    for start in {python, sys.executable}:
        path = start if os.path.isabs(start) else shutil.which(start)
        for _ in range(16):  # a symlink loop must not hang the gate
            if not path:
                break
            parent = Path(path).parent
            dirs.add(str(parent.parent if parent.name == "bin" else parent))
            if not os.path.islink(path):
                break
            target = os.readlink(path)
            path = target if os.path.isabs(target) else str(parent / target)
    return sorted(d for d in dirs if os.path.isdir(d) and d != "/")


def wrap(
    sb: Sandbox,
    argv: list[str],
    binds: dict[str, bool] | None = None,
    network: bool | None = None,
    cwd: Path | None = None,
) -> list[str]:
    """argv under bubblewrap when available, else argv unchanged.

    The child sees the host read-only, with the masked dirs replaced by empty tmpfs,
    then the sandbox root (writable) and ``binds`` ({path: writable}) mounted back.
    No network namespace is shared unless the sandbox allows network.
    """
    bwrap = _bwrap()
    if bwrap is None:
        return argv
    net = sb.network if network is None else network
    out = [bwrap, "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    if not net:
        out.append("--unshare-net")
    out += ["--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
    for path in _masked():
        out += ["--tmpfs", path]
    for path in _toolchain_dirs(sb.python):
        out += ["--ro-bind", path, path]
    mounts = {**(binds or {}), str(sb.root): True}
    for path, writable in sorted(mounts.items()):
        out += ["--bind" if writable else "--ro-bind", path, path]
    return [*out, "--chdir", str(cwd or sb.root), "--", *argv]


def run_in_sandbox(
    sb: Sandbox,
    argv: list[str],
    timeout_s: int,
    extra_env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    binds: dict[str, bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run argv in its own process group and kill the whole group on every exit path.

    Output goes to unnamed temp files, not pipes, so a child that leaves a process
    holding stdout open cannot stall the run: once the direct child exits, the group
    is killed and the files are read.
    """
    env = {**sb.env, **(extra_env or {})}
    key = str(sb.root)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            wrap(sb, argv, binds),
            cwd=sb.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            pass_fds=pass_fds,
            start_new_session=True,
        )
        with _GROUPS_LOCK:
            _GROUPS.setdefault(key, set()).add(proc.pid)
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(proc.pid)
            try:
                proc.wait(timeout=_DRAIN_S)
            except subprocess.TimeoutExpired:
                pass
            _forget(key, proc.pid)
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", errors="replace")
        stderr = err.read().decode("utf-8", errors="replace")
    if timed_out:
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"testgate: timed out after {timeout_s}s"
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout=stdout, stderr=stderr)


def reap(sb: Sandbox | None = None) -> None:
    """Kill every process group still registered (for one sandbox, or all)."""
    with _GROUPS_LOCK:
        keys = [str(sb.root)] if sb is not None else list(_GROUPS)
        pgids = [g for k in keys for g in _GROUPS.pop(k, set())]
    for pgid in pgids:
        _kill_group(pgid)


def destroy(sb: Sandbox) -> None:
    reap(sb)
    shutil.rmtree(sb.root, ignore_errors=True)
