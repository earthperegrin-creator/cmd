"""Read-only adapters that build a context catalog for the pure resolver."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_CATALOG_CACHE: dict[tuple[Any, ...], tuple[dict[str, Any], ...]] = {}


def load_local_context_catalog(
    startup_db: Path | None = None,
    people_index: Path | None = None,
) -> list[dict[str, Any]]:
    """Build a serializable local snapshot without mutating either source.

    This adapter is intentionally separate from ``context_resolver``. The
    resolver can therefore be replayed against a captured catalog in tests,
    while live CMD intake can use the user's current local stores.
    """

    key = _cache_key(startup_db, people_index)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return [dict(record) for record in cached]
    records: list[dict[str, Any]] = []
    if startup_db is not None:
        records.extend(_company_records(Path(startup_db)))
    if people_index is not None:
        records.extend(_people_records(Path(people_index)))
    _CATALOG_CACHE[key] = tuple(dict(record) for record in records)
    return [dict(record) for record in records]


def _cache_key(startup_db: Path | None, people_index: Path | None) -> tuple[Any, ...]:
    values: list[Any] = []
    for path in (startup_db, people_index):
        if path is None:
            values.append(None)
            continue
        resolved = Path(path).expanduser().resolve()
        try:
            stat = resolved.stat()
            values.append((str(resolved), stat.st_mtime_ns, stat.st_size))
        except OSError:
            values.append((str(resolved), None, None))
    return tuple(values)


def _company_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(companies)")}
            selected = [
                name for name in (
                    "id", "slug", "name", "local_name", "dba", "website_url",
                    "contact", "contact_email", "ceo_name", "memo_path",
                ) if name in columns
            ]
            if not selected:
                return []
            rows = connection.execute(
                f"SELECT {', '.join(selected)} FROM companies WHERE COALESCE(tracking_status, 'active') != 'archived'"
                if "tracking_status" in columns
                else f"SELECT {', '.join(selected)} FROM companies"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []

    records: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        slug = str(value.get("slug") or value.get("id") or "").strip()
        name = str(value.get("name") or value.get("local_name") or slug).strip()
        if not slug or not name:
            continue
        aliases = [value.get("local_name"), value.get("dba"), slug]
        website = str(value.get("website_url") or "").strip()
        domain = urlparse(website).netloc.lower().removeprefix("www.") if website else ""
        email = str(value.get("contact_email") or "").strip()
        records.append({
            "ref_id": f"company-{slug}",
            "kind": "company",
            "name": name,
            "aliases": [str(item).strip() for item in aliases if str(item or "").strip()],
            "email": email,
            "domain": domain,
            "search": " ".join(str(value.get(key) or "") for key in ("contact", "ceo_name")),
            "locator": f"startup.db:companies/{slug}",
            "provenance": "startup.db",
        })
    return records


def _people_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        match = re.match(r"^\s*-\s+id:\s*(.+?)\s*$", line)
        if match:
            if current:
                rows.append(current)
            current = {"id": _scalar(match.group(1))}
            continue
        if current is None:
            continue
        field = re.match(r"^\s{4}([a-z_]+):\s*(.*?)\s*$", line)
        if field and field.group(1) in {"name", "company", "email", "file", "linkedin"}:
            current[field.group(1)] = _scalar(field.group(2))
    if current:
        rows.append(current)

    records: list[dict[str, Any]] = []
    for row in rows:
        ref_id = row.get("id", "").strip()
        name = row.get("name", "").strip()
        if not ref_id or not name:
            continue
        company = row.get("company", "").strip()
        email = row.get("email", "").strip()
        records.append({
            "ref_id": f"person-{ref_id}",
            "kind": "person",
            "name": name,
            "aliases": [company] if company else [],
            "company": company,
            "email": email,
            "domain": email.rsplit("@", 1)[-1] if "@" in email else "",
            "locator": row.get("file") or f"people-index:{ref_id}",
            "provenance": "people-index",
            "search": row.get("linkedin", ""),
        })
    return records


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
