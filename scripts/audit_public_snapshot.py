#!/usr/bin/env python3
"""Audit every file in a built public snapshot before publication."""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_public_snapshot import read_manifest  # noqa: E402


DEFAULT_MANIFEST = ROOT / "publication" / "ship-manifest.txt"
REQUIRED = {
    "README.md", "LICENSE", "SECURITY.md", "PUBLICATION-BOUNDARY.md", "cmd", "server.py",
    "index.html", "examples/profile.json", "examples/weekly-log.md",
    "publication/repository-metadata.json",
    "scripts/run_lifecycle_canary.py", "scripts/run_server_security_canary.py",
}
TEXT_SUFFIXES = {
    "", ".css", ".html", ".ini", ".js", ".json", ".md", ".py", ".txt", ".yaml", ".yml",
}
FORBIDDEN_CONTENT = {
    "absolute macOS home path": re.compile(r"/Users/[A-Za-z0-9._-]+"),
    "creator handle": re.compile("earth" + "peregrin", re.IGNORECASE),
    "creator identity": re.compile("And" + r"rew(?:\s+Yang)?", re.IGNORECASE),
    "private organization": re.compile("Dar" + r"win\s+Ventures?", re.IGNORECASE),
    "private workspace": re.compile("ai" + "-sandbox", re.IGNORECASE),
    "private email domain": re.compile(r"@[A-Za-z0-9.-]*" + "darwin-venture" + r"\.[A-Za-z]{2,}", re.IGNORECASE),
    "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub-style secret": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
FORBIDDEN_PARTS = {".cmd", ".cmd-demo", ".git", "__pycache__", ".pytest_cache"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".bak", ".backup", ".pem", ".key"}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def snapshot_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def path_violation(relative: str) -> str | None:
    path = Path(relative)
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return "runtime or repository metadata path"
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return "environment secret path"
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return "runtime, secret, or backup suffix"
    return None


def audit(snapshot: Path, manifest_path: Path | None = None) -> list[str]:
    root = snapshot.expanduser().resolve()
    manifest = (manifest_path or root / "publication" / "ship-manifest.txt").resolve()
    problems: list[str] = []
    try:
        entries = read_manifest(manifest)
    except (OSError, ValueError) as error:
        return [f"manifest: {error}"]
    expected = {entry.target.as_posix() for entry in entries}
    actual = snapshot_files(root) if root.is_dir() else set()
    for missing in sorted(expected - actual):
        problems.append(f"{missing}: missing exported file")
    for extra in sorted(actual - expected):
        problems.append(f"{extra}: file is outside the manifest")
    for missing in sorted(REQUIRED - actual):
        problems.append(f"{missing}: missing required public file")

    with tempfile.TemporaryDirectory(prefix="cmd-public-compile-") as tmpdir:
        compile_root = Path(tmpdir)
        for relative in sorted(actual):
            reason = path_violation(relative)
            if reason:
                problems.append(f"{relative}: {reason}")
                continue
            path = root / relative
            if path.suffix == ".py":
                try:
                    py_compile.compile(
                        str(path),
                        cfile=str(compile_root / (relative.replace("/", "__") + "c")),
                        doraise=True,
                    )
                except py_compile.PyCompileError as error:
                    problems.append(f"{relative}: does not compile: {error.msg}")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"{relative}: expected text but is not UTF-8")
                continue
            # Files under publication/ are source templates that can be mapped
            # to another exported location; validate links at their target.
            if path.suffix.lower() == ".md" and "publication" not in Path(relative).parts:
                for raw_target in MARKDOWN_LINK.findall(content):
                    target = raw_target.strip().strip("<>").split("#", 1)[0].strip()
                    if not target or "://" in target or target.startswith(("mailto:", "#")):
                        continue
                    linked = (path.parent / target).resolve()
                    try:
                        linked.relative_to(root)
                    except ValueError:
                        problems.append(f"{relative}: local link escapes the snapshot: {raw_target}")
                        continue
                    if not linked.exists():
                        problems.append(f"{relative}: broken local link: {raw_target}")
            for line_number, line in enumerate(content.splitlines(), 1):
                for label, pattern in FORBIDDEN_CONTENT.items():
                    if pattern.search(line):
                        problems.append(f"{relative}:{line_number}: {label}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a built CMD public snapshot.")
    parser.add_argument("snapshot", type=Path, nargs="?", default=ROOT)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    problems = audit(args.snapshot, args.manifest)
    receipt = {
        "ok": not problems,
        "snapshot": str(args.snapshot.expanduser().resolve()),
        "problem_count": len(problems),
        "problems": problems,
    }
    print(json.dumps(receipt, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
