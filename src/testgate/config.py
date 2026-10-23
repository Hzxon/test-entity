"""testgate.yaml -> ProjectConfig.

The file is read from the base ref, never from the working tree, so the author
of a change cannot loosen the gates in the same change. Unknown keys raise
GateError so a typo cannot silently disable a gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from testgate import gitinfo
from testgate.core import GateError, ProjectConfig

CONFIG_FILE = "testgate.yaml"

TOP_KEYS = {
    "version",
    "package",
    "import_name",
    "tests",
    "protected",
    "seed_env",
    "gates",
    "sandbox",
}
TESTS_KEYS = {"golden", "auto", "hidden", "hidden_env"}
SANDBOX_KEYS = {"network", "timeout_s", "python"}

# gate name -> {YAML option -> ProjectConfig field}
GATE_OPTIONS: dict[str, dict[str, str]] = {
    "protected": {},
    "hygiene": {},
    "build": {},
    "tests": {"repeats": "repeats"},
    "hidden": {"required": "hidden_required"},
    "coverage": {"min_total": "coverage_min_total", "min_changed": "coverage_min_changed"},
    "mutation": {
        "min_score": "mutation_min_score",
        "max_mutants": "mutation_max_mutants",
        "max_shown": "mutation_max_shown",
        "timeout_s": "mutation_timeout_s",
    },
}

_FRACTIONS = ("coverage_min_total", "coverage_min_changed", "mutation_min_score")
_POSITIVE_INTS = (
    "repeats",
    "mutation_max_mutants",
    "mutation_max_shown",
    "mutation_timeout_s",
    "timeout_s",
)


def _check_keys(section: str, data: dict[str, Any], allowed: set[str]) -> None:
    for key in data:
        if key not in allowed:
            where = f"{section}." if section else ""
            raise GateError(f"unknown key in {CONFIG_FILE}: {where}{key}")


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise GateError(f"{CONFIG_FILE}: {name} must be a mapping")
    return value


def _string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise GateError(f"{CONFIG_FILE}: {name} must be a non-empty string")
    return value


def _read_text(root: Path, base_ref: str | None) -> tuple[str, str]:
    """Return (yaml text, source) where source is the base ref or "worktree"."""
    if base_ref is not None:
        text = gitinfo.show_file(root, base_ref, CONFIG_FILE)
        if text is not None:
            return text, base_ref
        # first adoption: the file does not exist at the base ref yet
        source = "worktree"
    else:
        source = "worktree"
    path = root / CONFIG_FILE
    if not path.is_file():
        raise GateError(f"{CONFIG_FILE} not found")
    return path.read_text(encoding="utf-8"), source


def load_config(root: Path, base_ref: str | None) -> ProjectConfig:
    text, source = _read_text(root, base_ref)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateError(f"{CONFIG_FILE} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{CONFIG_FILE} must be a mapping at the top level")
    _check_keys("", data, TOP_KEYS)

    if "package" not in data:
        raise GateError(f"{CONFIG_FILE}: missing required key: package")
    if "import_name" not in data:
        raise GateError(f"{CONFIG_FILE}: missing required key: import_name")
    cfg = ProjectConfig(
        root=root,
        package=_string("package", data["package"]),
        import_name=_string("import_name", data["import_name"]),
    )

    tests = _mapping("tests", data.get("tests"))
    _check_keys("tests", tests, TESTS_KEYS)
    if "golden" in tests:
        cfg.golden = _string("tests.golden", tests["golden"])
    if "auto" in tests:
        cfg.auto = _string("tests.auto", tests["auto"])
    if tests.get("hidden") is not None:
        cfg.hidden = _string("tests.hidden", tests["hidden"])
    if "hidden_env" in tests:
        cfg.hidden_env = _string("tests.hidden_env", tests["hidden_env"])

    if "protected" in data:
        prot = data["protected"]
        if not isinstance(prot, list) or not all(isinstance(p, str) for p in prot):
            raise GateError(f"{CONFIG_FILE}: protected must be a list of paths")
        cfg.protected = list(prot)
    if "seed_env" in data:
        cfg.seed_env = _string("seed_env", data["seed_env"])

    gates = _mapping("gates", data.get("gates"))
    _check_keys("gates", gates, set(GATE_OPTIONS))
    for name, value in gates.items():
        if isinstance(value, bool):
            cfg.gates[name] = value
            continue
        opts = _mapping(f"gates.{name}", value)
        _check_keys(f"gates.{name}", opts, {"enabled", *GATE_OPTIONS[name]})
        enabled = opts.get("enabled", True)
        if not isinstance(enabled, bool):
            raise GateError(f"{CONFIG_FILE}: gates.{name}.enabled must be true or false")
        cfg.gates[name] = enabled
        for opt, fld in GATE_OPTIONS[name].items():
            if opt in opts:
                setattr(cfg, fld, opts[opt])

    sandbox = _mapping("sandbox", data.get("sandbox"))
    _check_keys("sandbox", sandbox, SANDBOX_KEYS)
    if "network" in sandbox:
        cfg.network = sandbox["network"]
    if "timeout_s" in sandbox:
        cfg.timeout_s = sandbox["timeout_s"]
    if sandbox.get("python") is not None:
        cfg.python = _string("sandbox.python", sandbox["python"])

    _validate(cfg)
    cfg.raw = {**data, "_source": source}
    return cfg


def _validate(cfg: ProjectConfig) -> None:
    for fld in _FRACTIONS:
        value = getattr(cfg, fld)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GateError(f"{CONFIG_FILE}: {fld} must be a number between 0 and 1")
        if not 0 <= value <= 1:
            raise GateError(f"{CONFIG_FILE}: {fld} must be between 0 and 1, got {value}")
    for fld in _POSITIVE_INTS:
        value = getattr(cfg, fld)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GateError(f"{CONFIG_FILE}: {fld} must be an integer >= 1, got {value!r}")
    if not isinstance(cfg.hidden_required, bool):
        raise GateError(f"{CONFIG_FILE}: gates.hidden.required must be true or false")
    if not isinstance(cfg.network, bool):
        raise GateError(f"{CONFIG_FILE}: sandbox.network must be true or false")
