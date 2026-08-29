#!/usr/bin/env python3
"""Build a stable, report-blind repository evaluation packet."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PERSONAS = {
    "human": ROOT / "evals" / "repository" / "personas" / "fractional-cmo.md",
    "agent": ROOT / "evals" / "repository" / "personas" / "chatgpt-scout.md",
}
SURFACES = {
    "human": [
        "README.md",
        "docs/STATUS.md",
        "docs/SAFE-TRIAL.md",
    ],
    "agent": [
        "README.md",
        "AGENTS.md",
        "llms.txt",
        "docs/STATUS.md",
        "docs/SAFE-TRIAL.md",
        "docs/AGENT-SETUP.md",
        "docs/AGENT-INTEGRATION.md",
    ],
}


def git_text(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def build_packet(persona: str) -> str:
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona: {persona}")
    sha = git_text("rev-parse", "HEAD")
    dirty = bool(git_text("status", "--porcelain"))
    tracked = [
        path
        for path in git_text("ls-files").splitlines()
        if path and not path.startswith("evals/repository/reports/")
    ]
    sections = [
        "# CMD repository evaluation packet",
        "",
        f"- Persona: `{persona}`",
        f"- Commit: `{sha}`",
        f"- Working tree: `{'modified' if dirty else 'clean'}`",
        "- Prior evaluation reports: excluded",
        "",
        "Score from scratch. Judge what exists, not what the project intends.",
        "",
        "## Frozen persona",
        "",
        PERSONAS[persona].read_text(encoding="utf-8").rstrip(),
        "",
        "## Public file map",
        "",
        "```text",
        *tracked,
        "```",
    ]
    for relative in SURFACES[persona]:
        sections.extend([
            "",
            f"## Surface: `{relative}`",
            "",
            (ROOT / relative).read_text(encoding="utf-8").rstrip(),
        ])
    return "\n".join(sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build one frozen CMD repository-evaluation packet.")
    parser.add_argument("persona", choices=sorted(PERSONAS))
    args = parser.parse_args(argv)
    print(build_packet(args.persona), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
