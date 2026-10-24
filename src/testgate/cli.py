"""Command line entry point: init, run, mutants."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from importlib import resources
from pathlib import Path

from testgate import config as config_mod
from testgate import gitinfo, sandbox
from testgate.core import GateError, GateResult, RunContext, Status
from testgate.gates import GATE_ORDER, run_gates
from testgate.languages import get_language
from testgate.report import build_report, to_markdown, write_outputs

NEXT_STEPS = """\
Next steps:
  1. Create the environment "testgate" with yourself as required reviewer. Every gate
     run waits for you; read the PR's .github/** diff before approving it.
  2. Set TESTGATE_SEED as an environment secret (`openssl rand -hex 32`), not a repo secret.
  3. Create the private hidden tests repo, add a read-only deploy key, and set the
     TESTGATE_HIDDEN_REPO and TESTGATE_HIDDEN_SSH_KEY environment secrets.
  4. Protect main with the "testgate" required check.
"""
PLACEHOLDER_ACTION = "<owner>/testgate@<full-sha>"
_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")


def _template(name: str) -> str:
    return resources.files("testgate").joinpath("templates", name).read_text(encoding="utf-8")


def _cmd_init(args: argparse.Namespace) -> int:
    action = args.action or PLACEHOLDER_ACTION
    if args.action and not _ACTION.match(args.action):
        # a tag or branch lets whoever moves it pick the gate's code
        raise GateError(f"--action must be owner/repo@<40-hex commit sha>, got {args.action!r}")
    js = args.language == "js"
    if js:
        package = args.package or "src"
        import_name = args.import_name or "index.js"
    elif args.package:
        package = args.package
        import_name = args.import_name or Path(package.rstrip("/")).name
    else:
        import_name = args.import_name or "mypkg"
        package = f"src/{import_name}"
    subs = {"__PACKAGE__": package, "__IMPORT_NAME__": import_name}

    def render(text: str) -> str:
        for key, value in subs.items():
            text = text.replace(key, value)
        return text

    config_text = render(_template("testgate.yaml"))
    if js:
        config_text = config_text.replace("# language: python", "language: js")
        example = ("tests/golden/example.test.js", _template("example.test.js"))
    else:
        example = ("tests/golden/test_example.py", render(_template("test_example.py")))

    root = Path.cwd()
    targets = [
        (root / "testgate.yaml", config_text),
        (root / example[0], example[1]),
        (root / "tests/auto/.gitkeep", ""),
        (
            root / ".github/workflows/testgate.yml",
            _template("testgate.yml").replace("__ACTION__", action),
        ),
    ]
    existing = [t for t, _ in targets if t.exists()]
    if existing and not args.force:
        for path in existing:
            print(f"exists: {path.relative_to(root)}", file=sys.stderr)
        print("testgate: refusing to overwrite; pass --force", file=sys.stderr)
        return 2
    for path, text in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(root)}")
    if action == PLACEHOLDER_ACTION:
        print(f"Replace {PLACEHOLDER_ACTION} in the workflow with a full commit SHA.")
    print(NEXT_STEPS, end="")
    return 0


def _resolve_base(root: Path, args: argparse.Namespace) -> tuple[bool, str | None]:
    no_git = args.no_git or not gitinfo.is_git_repo(root)
    if no_git:
        return True, None
    base_ref = gitinfo.resolve_base(root, args.base)
    if base_ref is None:
        raise GateError("no base ref found; pass --base or use --no-git")
    return False, base_ref


def _find_hidden(
    root: Path, cfg_hidden: str | None, env_name: str, flag: str | None
) -> Path | None:
    candidates: list[Path] = []
    if flag:
        candidates.append(Path(flag))
    if cfg_hidden:
        candidates.append(root / cfg_hidden)
    if os.environ.get(env_name):
        candidates.append(Path(os.environ[env_name]))
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root or Path.cwd()).resolve()
    only: list[str] | None = None
    if args.only:
        only = [g.strip() for g in args.only.split(",") if g.strip()]
        unknown = [g for g in only if g not in GATE_ORDER]
        if unknown:
            raise GateError(f"unknown gate in --only: {', '.join(unknown)}")
    no_git, base_ref = _resolve_base(root, args)
    cfg = config_mod.load_config(root, base_ref)
    hidden_dir = _find_hidden(root, cfg.hidden, cfg.hidden_env, args.hidden)
    env_seed = os.environ.get(cfg.seed_env)
    seed = env_seed if env_seed else secrets.token_hex(8)
    python = args.python or cfg.python or sys.executable
    changed = [] if no_git or base_ref is None else gitinfo.changed_lines(root, base_ref)
    ctx = RunContext(
        config=cfg,
        base_ref=base_ref,
        changed=changed,
        hidden_dir=hidden_dir,
        seed=seed,
        seed_is_secret=bool(env_seed),
        python=python,
        language=get_language(cfg.language),
        head="" if no_git else gitinfo.head_sha(root),
    )
    isolated = sandbox.isolation_available()
    if cfg.require_isolation and not isolated:
        # sandbox.require_isolation: no gate runs without OS isolation (#56)
        results = [
            GateResult(
                "isolation",
                Status.FAIL,
                "OS isolation required (sandbox.require_isolation) but bubblewrap is unavailable",
            ),
            *(GateResult(g, Status.SKIP, "skipped: no OS isolation") for g in GATE_ORDER),
        ]
    else:
        results = run_gates(ctx, only)
    report = build_report(ctx, results)
    report.isolation = "bwrap" if isolated else "best-effort"
    if not args.quiet:
        print(to_markdown(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    write_outputs(
        report,
        ctx,
        results,
        Path(args.json) if args.json else None,
        Path(args.private_log) if args.private_log else None,
        Path(summary) if summary else None,
    )
    return 0 if report.verdict == Status.PASS else 1


def _cmd_mutants(args: argparse.Namespace) -> int:
    root = Path(args.root or Path.cwd()).resolve()
    no_git, base_ref = _resolve_base(root, args)
    cfg = config_mod.load_config(root, base_ref)
    language = get_language(cfg.language)
    limit = args.max if args.max is not None else cfg.mutation_max_mutants
    package = cfg.package.strip("/")
    prefix = "" if package in ("", ".") else package + "/"

    lines_by_path: dict[str, set[int]] = {}
    if no_git or base_ref is None:
        base = root / package
        for file in sorted(base.rglob("*")) if base.is_dir() else []:
            if (
                not file.is_file()
                or file.suffix not in language.source_suffixes
                or "__pycache__" in file.parts
                or "node_modules" in file.parts
            ):
                continue
            count = len(file.read_text(encoding="utf-8").splitlines())
            lines_by_path[file.relative_to(root).as_posix()] = set(range(1, count + 1))
    else:
        for cf in gitinfo.changed_lines(root, base_ref):
            if cf.path.startswith(prefix) and (root / cf.path).is_file():
                lines_by_path[cf.path] = cf.lines

    total = 0
    for path, lines in sorted(lines_by_path.items()):
        source = (root / path).read_text(encoding="utf-8")
        for m in language.generate_mutants(path, source, lines, "mutants", limit):
            print(f"{m.id}  {m.operator}  {m.before} -> {m.after}")
            total += 1
    print(f"{total} mutant(s)", file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testgate", description="Merge gates for AI-authored changes."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write testgate.yaml, example tests and the workflow")
    init.add_argument("--package", help="source directory, e.g. src/mypkg")
    init.add_argument("--import-name", help="top-level module name")
    init.add_argument(
        "--language", choices=["python", "js"], default="python", help="project language"
    )
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument("--action", help="the testgate action to pin, owner/repo@<full commit sha>")

    run = sub.add_parser("run", help="run the gates and print the verdict")
    run.add_argument("--root", help="project root (default: current directory)")
    run.add_argument("--base", help="base ref to compare with")
    run.add_argument("--no-git", action="store_true", help="skip git; protected gate is skipped")
    run.add_argument("--hidden", help="hidden tests directory")
    run.add_argument("--only", help="comma-separated gates to run")
    run.add_argument("--json", help="write the public report as JSON")
    run.add_argument("--private-log", help="write the private log")
    run.add_argument("--python", help="interpreter for the sandbox")
    run.add_argument("--quiet", action="store_true", help="do not print the markdown report")

    mut = sub.add_parser("mutants", help="list the mutants that would be generated")
    mut.add_argument("--root", help="project root (default: current directory)")
    mut.add_argument("--base", help="base ref to compare with")
    mut.add_argument("--no-git", action="store_true", help="treat every line as changed")
    mut.add_argument("--max", type=int, help="maximum mutants")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    handlers = {"init": _cmd_init, "run": _cmd_run, "mutants": _cmd_mutants}
    try:
        return handlers[args.command](args)
    except GateError as exc:
        print(f"testgate: {exc}", file=sys.stderr)
        return 2
    finally:
        sandbox.reap()  # nothing a sandbox child started outlives the run (#54)


if __name__ == "__main__":
    raise SystemExit(main())
