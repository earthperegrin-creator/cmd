#!/usr/bin/env python3
"""Build CMD's public candidate from an explicit source-to-target allowlist."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "publication" / "ship-manifest.txt"


@dataclass(frozen=True)
class ManifestEntry:
    source: PurePosixPath
    target: PurePosixPath


def safe_relative(value: str, *, line_number: int) -> PurePosixPath:
    path = PurePosixPath(value.strip())
    if not value.strip() or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"manifest line {line_number} has an unsafe path: {value!r}")
    return path


def read_manifest(path: Path = DEFAULT_MANIFEST) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    targets: set[PurePosixPath] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pieces = [piece.strip() for piece in line.split("=>")]
        if len(pieces) == 1:
            source = target = safe_relative(pieces[0], line_number=line_number)
        elif len(pieces) == 2:
            source = safe_relative(pieces[0], line_number=line_number)
            target = safe_relative(pieces[1], line_number=line_number)
        else:
            raise ValueError(f"manifest line {line_number} has more than one mapping operator")
        if target in targets:
            raise ValueError(f"manifest repeats exported target: {target}")
        targets.add(target)
        entries.append(ManifestEntry(source, target))
    if not entries:
        raise ValueError("manifest is empty")
    return sorted(entries, key=lambda entry: (entry.target.as_posix(), entry.source.as_posix()))


def prepare_destination(destination: Path) -> Path:
    target = destination.expanduser().resolve()
    broad = {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}
    if target in broad:
        raise ValueError("refusing to build into a broad or source directory")
    if target.exists():
        if not target.is_dir():
            raise ValueError("snapshot destination exists and is not a directory")
        if any(target.iterdir()):
            raise ValueError("snapshot destination must be empty")
    else:
        target.mkdir(parents=True)
    return target


def build_snapshot(
    destination: Path,
    *,
    source_root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, object]:
    target_root = prepare_destination(destination)
    entries = read_manifest(manifest_path)
    copied: list[str] = []
    for entry in entries:
        source = source_root / entry.source.as_posix()
        target = target_root / entry.target.as_posix()
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"manifest source must be a regular file: {entry.source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(entry.target.as_posix())
    return {
        "ok": True,
        "destination": str(target_root),
        "file_count": len(copied),
        "files": copied,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a manifest-bounded CMD public snapshot.")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        receipt = build_snapshot(args.destination, manifest_path=args.manifest)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 2
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
