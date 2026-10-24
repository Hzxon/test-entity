"""Report building and rendering.

The JSON and markdown outputs are public: they never read GateResult.private
or RunContext.private_log. Only private_text does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testgate.core import GateResult, Report, RunContext, Status


def build_report(ctx: RunContext, results: list[GateResult]) -> Report:
    verdict = Status.FAIL if any(r.blocks for r in results) else Status.PASS
    return Report(
        verdict=verdict,
        gates=results,
        base_ref=ctx.base_ref,
        changed_files=len(ctx.changed),
        changed_lines=sum(len(c.lines) for c in ctx.changed),
        seed_is_secret=ctx.seed_is_secret,
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _format_item(item: Any) -> str:
    if isinstance(item, dict):
        return ", ".join(f"{k}={v}" for k, v in item.items())
    return str(item)


def to_markdown(report: Report) -> str:
    lines = [
        f"# testgate: {report.verdict.value.upper()}",
        "",
        "| gate | status | summary |",
        "| --- | --- | --- |",
    ]
    for g in report.gates:
        lines.append(f"| {g.name} | {g.status.value} | {_cell(g.summary)} |")
    lines.append("")
    lines.append(
        f"base: {report.base_ref or 'none'}; changed: {report.changed_files} file(s), "
        f"{report.changed_lines} line(s); seed_is_secret: {str(report.seed_is_secret).lower()}; "
        f"isolation: {report.isolation}"
    )
    for g in report.gates:
        if not g.public:
            continue
        lines += ["", f"## {g.name}", ""]
        for key, value in g.public.items():
            if isinstance(value, list):
                if not value:
                    lines.append(f"- {key}: none")
                    continue
                lines.append(f"- {key}:")
                lines.extend(f"  - {_cell(_format_item(item))}" for item in value)
            elif isinstance(value, dict):
                lines.append(f"- {key}: {_cell(_format_item(value))}")
            else:
                lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def private_text(ctx: RunContext, results: list[GateResult]) -> str:
    parts = list(ctx.private_log)
    for g in results:
        parts.append(f"== {g.name} ({g.status.value}) ==")
        parts.append(json.dumps(g.private, default=str, indent=2))
    return "\n".join(parts) + "\n"


def _write(path: Path, text: str, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)


def write_outputs(
    report: Report,
    ctx: RunContext,
    results: list[GateResult],
    json_path: Path | None,
    private_path: Path | None,
    summary_path: Path | None,
) -> None:
    if json_path is not None:
        _write(json_path, json.dumps(report.to_public_dict(), indent=2) + "\n")
    if private_path is not None:
        _write(private_path, private_text(ctx, results))
    if summary_path is not None:
        _write(summary_path, to_markdown(report) + "\n", append=True)
