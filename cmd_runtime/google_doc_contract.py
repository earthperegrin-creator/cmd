"""Google Docs artifact verification contract for CMD receipts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


GOOGLE_DOC_CONTRACT_ENFORCED_AT = "2026-07-24T07:55:00+00:00"
DOC_URL_RE = re.compile(r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)")


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def enforced_for_result(result: dict[str, Any]) -> bool:
    result_time = parse_iso(result.get("time") or result.get("timestamp"))
    enforced_at = parse_iso(GOOGLE_DOC_CONTRACT_ENFORCED_AT)
    if result_time is None or enforced_at is None:
        return True
    return result_time >= enforced_at


def google_doc_ids_from_text(value: Any) -> set[str]:
    return {match.group(1) for match in DOC_URL_RE.finditer(str(value or ""))}


def google_doc_ids_from_artifact(artifact: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(artifact, dict):
        artifact_type = str(artifact.get("type") or artifact.get("artifact_type") or "").lower()
        for key in ("url", "uri", "content", "body", "text"):
            ids.update(google_doc_ids_from_text(artifact.get(key)))
        external_id = str(artifact.get("external_id") or artifact.get("document_id") or "").strip()
        if artifact_type in {"google_doc", "google_docs_document"} and external_id:
            ids.add(external_id)
    else:
        ids.update(google_doc_ids_from_text(artifact))
    return ids


def google_doc_artifact_ids(result: dict[str, Any]) -> set[str]:
    """Return Doc IDs for provider artifacts this receipt claims to have produced.

    Sources are deliberately ignored: a Google Doc may be an input source for a
    research task, and that should not make the task subject to this output
    artifact contract.
    """
    ids = google_doc_ids_from_artifact(result.get("artifact"))
    artifact = result.get("artifact")
    if isinstance(artifact, dict):
        return ids
    text = " ".join(str(result.get(key) or "") for key in ("summary", "conclusion"))
    if ids and re.search(r"\b(created|saved|wrote|repaired|rebuilt|updated|formatted|imported)\b", text, re.IGNORECASE):
        return ids
    return ids


def google_doc_verification_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[Any] = [
        result.get("google_doc_verification"),
        (result.get("verification") or {}).get("google_doc_native") if isinstance(result.get("verification"), dict) else None,
    ]
    artifact = result.get("artifact")
    if isinstance(artifact, dict):
        verification = artifact.get("verification")
        if isinstance(verification, dict):
            candidates.extend([
                verification.get("google_doc_native"),
                verification,
            ])
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return None


def google_doc_verification_ok(result: dict[str, Any]) -> bool:
    targets = google_doc_artifact_ids(result)
    verification = google_doc_verification_payload(result)
    if not verification or verification.get("ok") is not True:
        return False
    verified_id = str(verification.get("document_id") or verification.get("external_id") or "").strip()
    if targets and verified_id and verified_id not in targets:
        return False
    return True


def result_needs_google_doc_verification(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "") != "completed":
        return False
    if not enforced_for_result(result):
        return False
    return bool(google_doc_artifact_ids(result))


def result_blocks_on_google_doc_contract(result: dict[str, Any]) -> bool:
    return result_needs_google_doc_verification(result) and not google_doc_verification_ok(result)


def google_doc_contract_summary(result: dict[str, Any], fallback: str = "") -> str:
    ids = sorted(google_doc_artifact_ids(result))
    target = ids[0] if ids else "Google Doc artifact"
    return (
        f"{fallback} Google Doc artifact {target} is not a completed CMD artifact until "
        "a post-write native-structure verifier passes and is recorded in the receipt."
    ).strip()


def body_from_document(document: dict[str, Any]) -> dict[str, Any]:
    tabs = document.get("tabs")
    if isinstance(tabs, list) and tabs:
        document_tab = tabs[0].get("documentTab") if isinstance(tabs[0], dict) else None
        if isinstance(document_tab, dict) and isinstance(document_tab.get("body"), dict):
            return document_tab["body"]
    return document.get("body") if isinstance(document.get("body"), dict) else {}


def paragraph_text(paragraph: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in paragraph.get("elements") or []:
        text_run = element.get("textRun") if isinstance(element, dict) else None
        if isinstance(text_run, dict):
            parts.append(str(text_run.get("content") or ""))
    return "".join(parts).strip()


def iter_paragraphs_and_tables(content: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    paragraphs: list[dict[str, Any]] = []
    tables = 0
    for part in content:
        paragraph = part.get("paragraph")
        if isinstance(paragraph, dict):
            paragraphs.append(paragraph)
            continue
        table = part.get("table")
        if not isinstance(table, dict):
            continue
        tables += 1
        for row in table.get("tableRows") or []:
            for cell in row.get("tableCells") or []:
                for cell_part in cell.get("content") or []:
                    cell_paragraph = cell_part.get("paragraph")
                    if isinstance(cell_paragraph, dict):
                        paragraphs.append(cell_paragraph)
    return paragraphs, tables


def analyze_google_doc(document: dict[str, Any], *, expect_table: bool = False) -> dict[str, Any]:
    body = body_from_document(document)
    paragraphs, table_count = iter_paragraphs_and_tables(body.get("content") or [])
    texts: list[str] = []
    style_counts: dict[str, int] = {}
    list_count = 0
    for paragraph in paragraphs:
        text = paragraph_text(paragraph)
        if not text:
            continue
        texts.append(text)
        style = str((paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "")
        if style:
            style_counts[style] = style_counts.get(style, 0) + 1
        if "bullet" in paragraph:
            list_count += 1

    leftover = [
        text for text in texts
        if text.startswith("#")
        or text.startswith("|")
        or text.startswith("- ")
        or "**" in text
    ]
    checks = {
        "no_leftover_markdown": len(leftover) == 0,
        "expected_table_present": (table_count > 0) if expect_table else True,
    }
    return {
        "ok": all(checks.values()),
        "document_id": str(document.get("documentId") or document.get("document_id") or ""),
        "revision_id": str(document.get("revisionId") or document.get("revision_id") or ""),
        "title": str(document.get("title") or ""),
        "checks": checks,
        "counts": {
            "paragraphs": len(texts),
            "tables": table_count,
            "lists": list_count,
            "styles": style_counts,
        },
        "leftover_markdown_count": len(leftover),
        "leftover_markdown_lines": leftover[:25],
    }
