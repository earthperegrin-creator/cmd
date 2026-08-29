"""Daemon-side adapters for every capability in the v1 registry."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .capabilities import CapabilityRegistry


class AdapterError(RuntimeError):
    """Raised when an adapter cannot execute or independently read back work."""


class GoogleTransport(Protocol):
    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]: ...

    def readback(
        self, capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]
    ) -> Mapping[str, Any] | None: ...


class BufferTransport(Protocol):
    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]: ...


class HotmailTransport(Protocol):
    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]: ...

    def readback(
        self, capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]
    ) -> Mapping[str, Any] | None: ...


class WebTransport(Protocol):
    def search(self, query: str, limit: int) -> Sequence[Mapping[str, Any]]: ...

    def open(self, url: str) -> Mapping[str, Any]: ...


Operation = Callable[[Mapping[str, Any], str], Mapping[str, Any]]


@dataclass(frozen=True)
class AdapterCoverage:
    missing_executors: tuple[str, ...]
    missing_verifiers: tuple[str, ...]
    missing_cleanups: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.missing_executors or self.missing_verifiers or self.missing_cleanups)


VERIFIERS = frozenset({
    "task.readback", "artifact.hash", "web.sources", "web.source",
    "buffer.channels_readback", "buffer.posts_readback", "buffer.draft_readback", "buffer.schedule_readback", "buffer.publish_readback",
    "gmail.search_readback", "gmail.message_readback", "gmail.draft_readback",
    "gmail.sent_readback", "gmail.draft_absent", "calendar.list_readback",
    "hotmail.profile_readback", "hotmail.folders_readback", "hotmail.search_readback",
    "hotmail.list_readback", "hotmail.message_readback", "hotmail.draft_readback",
    "hotmail.sent_readback", "hotmail.message_absent",
    "calendar.event_readback", "calendar.event_absent", "drive.list_readback",
    "drive.file_readback", "drive.file_absent", "docs.structure_readback", "callmemo.readback",
})
CLEANUPS = frozenset({
    "task.update", "local.delete", "gmail.delete_draft", "calendar.delete",
    "calendar.update", "drive.delete", "hotmail.delete",
})
GOOGLE_PREFIXES = ("gmail.", "calendar.", "drive.", "docs.")
BUFFER_PREFIX = "buffer."
HOTMAIL_PREFIX = "hotmail."


def payload_hash(payload: Mapping[str, Any]) -> str:
    value = {key: item for key, item in payload.items() if key != "approved_payload_hash"}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CapabilityAdapters:
    """Host-owned implementations exposed to the broker, never to workers directly."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        task_database: Path,
        local_roots: Sequence[Path],
        artifact_root: Path,
        google: GoogleTransport | None = None,
        buffer: BufferTransport | None = None,
        hotmail: HotmailTransport | None = None,
        web: WebTransport | None = None,
        callmemo_prepare: Operation | None = None,
        callmemo_commit: Operation | None = None,
    ):
        self.registry = registry
        self.task_database = Path(task_database)
        self.local_roots = tuple(Path(root).expanduser().resolve() for root in local_roots)
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.google = google
        self.buffer = buffer
        self.hotmail = hotmail
        self.web = web or HttpWebTransport()
        self.callmemo_prepare = callmemo_prepare
        self.callmemo_commit = callmemo_commit
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._initialize_tasks()
        self.executors: dict[str, Operation] = {
            manifest.name: self._executor_for(manifest.name) for manifest in registry.all()
        }

    def coverage(self) -> AdapterCoverage:
        manifests = self.registry.all()
        return AdapterCoverage(
            tuple(sorted(item.name for item in manifests if item.name not in self.executors)),
            tuple(sorted(item.name for item in manifests if item.verifier not in VERIFIERS)),
            tuple(sorted(item.name for item in manifests if item.cleanup and item.cleanup not in CLEANUPS)),
        )

    def verifier(self, capability: str, payload: Mapping[str, Any], artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        manifest = self.registry.get(capability)
        if manifest is None:
            raise AdapterError(f"unknown capability: {capability}")
        readback = artifact.get("readback")
        passed = artifact.get("status") == "ok"
        mismatches: dict[str, Mapping[str, Any]] = {}
        if manifest.verifier.endswith("_absent"):
            passed = passed and isinstance(readback, Mapping) and readback.get("absent") is True
        elif manifest.verifier == "artifact.hash":
            path = Path(str(artifact.get("path") or artifact.get("output_path") or ""))
            passed = passed and path.is_file() and artifact.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
        elif manifest.verifier in {"gmail.search_readback", "calendar.list_readback", "drive.list_readback"}:
            passed = passed and isinstance(readback, Mapping)
        elif manifest.verifier == "buffer.channels_readback":
            passed = passed and isinstance(readback, Mapping) and isinstance(readback.get("channels"), list)
        elif manifest.verifier == "buffer.posts_readback":
            passed = passed and isinstance(readback, Mapping) and isinstance(readback.get("posts"), Mapping)
        elif manifest.verifier in {"buffer.draft_readback", "buffer.schedule_readback", "buffer.publish_readback"}:
            post = readback.get("post", readback) if isinstance(readback, Mapping) else None
            actual_text = post.get("text") if isinstance(post, Mapping) else None
            if actual_text != payload.get("text"):
                mismatches["text"] = {"expected": payload.get("text"), "actual": actual_text}
            expected_mentions = payload.get("linkedin_mentions") or []
            if expected_mentions and isinstance(post, Mapping):
                metadata = post.get("metadata")
                actual_annotations = metadata.get("annotations", []) if isinstance(metadata, Mapping) else []
                actual_pairs = {
                    (str(item.get("text") or ""), str(item.get("url") or "").rstrip("/"))
                    for item in actual_annotations
                    if isinstance(item, Mapping)
                }
                expected_pairs = {
                    (str(item.get("localized_name") or ""), str(item.get("link") or "").rstrip("/"))
                    for item in expected_mentions
                    if isinstance(item, Mapping)
                }
                if actual_pairs != expected_pairs:
                    mismatches["linkedin_mentions"] = {
                        "expected": sorted(expected_pairs),
                        "actual": sorted(actual_pairs),
                    }
            expected_link = payload.get("linkedin_link_attachment") or {}
            if expected_link and isinstance(post, Mapping):
                metadata = post.get("metadata")
                actual_link = metadata.get("linkAttachment") if isinstance(metadata, Mapping) else None
                actual_link = actual_link if isinstance(actual_link, Mapping) else {}
                expected_url = str(expected_link.get("url") or "").rstrip("/")
                actual_url = str(actual_link.get("expandedUrl") or actual_link.get("url") or "").rstrip("/")
                link_checks = {
                    "url": (expected_url, actual_url),
                    "title": (str(expected_link.get("title") or ""), str(actual_link.get("title") or "")),
                    "description": (
                        str(expected_link.get("description") or ""),
                        str(actual_link.get("text") or ""),
                    ),
                    "thumbnail_url": (
                        str(expected_link.get("thumbnail_url") or ""),
                        str(actual_link.get("thumbnail") or ""),
                    ),
                }
                failed_link_checks = {
                    field: {"expected": expected, "actual": actual}
                    for field, (expected, actual) in link_checks.items()
                    if expected and expected != actual
                }
                if failed_link_checks:
                    mismatches["linkedin_link_attachment"] = failed_link_checks
            expected_thread = payload.get("twitter_thread") or []
            if expected_thread and isinstance(post, Mapping):
                metadata = post.get("metadata")
                actual_thread = metadata.get("thread", []) if isinstance(metadata, Mapping) else []
                expected_texts = [str(item.get("text") or "") for item in expected_thread]
                actual_texts = [
                    str(item.get("text") or "")
                    for item in actual_thread
                    if isinstance(item, Mapping)
                ]
                if actual_texts != expected_texts:
                    mismatches["twitter_thread"] = {
                        "expected": expected_texts,
                        "actual": actual_texts,
                    }
            expected_repost = payload.get("twitter_repost") or {}
            if expected_repost and isinstance(post, Mapping):
                metadata = post.get("metadata")
                actual_repost = metadata.get("retweet") if isinstance(metadata, Mapping) else None
                actual_repost = actual_repost if isinstance(actual_repost, Mapping) else {}
                expected_post_id = str(expected_repost.get("post_id") or "")
                actual_post_id = str(actual_repost.get("id") or "")
                if actual_post_id != expected_post_id:
                    mismatches["twitter_repost"] = {
                        "expected": {"post_id": expected_post_id},
                        "actual": {"post_id": actual_post_id},
                    }
            expected_status = {
                "buffer.draft_readback": "draft",
                "buffer.schedule_readback": "scheduled",
                "buffer.publish_readback": "sent",
            }[manifest.verifier]
            passed = (
                passed
                and isinstance(post, Mapping)
                and bool(artifact.get("provider_id"))
                and post.get("status") == expected_status
                and not mismatches
            )
        elif manifest.verifier.startswith(("gmail.", "hotmail.", "calendar.", "drive.", "callmemo.")):
            mismatches = _material_mismatches(capability, payload, readback)
            passed = passed and isinstance(readback, Mapping) and bool(artifact.get("provider_id")) and not mismatches
        elif manifest.verifier == "task.readback":
            mismatches = _material_mismatches(capability, payload, readback)
            passed = passed and isinstance(readback, Mapping) and not mismatches
        return {
            "passed": passed,
            "verifier": manifest.verifier,
            "provider_id": artifact.get("provider_id"),
            "payload_hash": artifact.get("payload_hash"),
            "readback": readback,
            "mismatches": mismatches,
        }

    def cleanup(self, capability: str, artifact: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        manifest = self.registry.get(capability)
        if manifest is None or not manifest.cleanup:
            raise AdapterError(f"capability has no cleanup: {capability}")
        cleanup_payload = artifact.get("cleanup_payload")
        if not isinstance(cleanup_payload, Mapping):
            raise AdapterError("artifact has no cleanup payload")
        cleanup_capability = manifest.cleanup
        if cleanup_capability == "local.delete":
            path = self._bounded_cleanup_path(str(cleanup_payload["path"]))
            path.unlink(missing_ok=True)
            return {"status": "ok", "deleted_path": str(path), "readback": {"absent": not path.exists()}}
        return self.executors[cleanup_capability](cleanup_payload, f"cleanup:{idempotency_key}")

    def _executor_for(self, capability: str) -> Operation:
        if capability.startswith(GOOGLE_PREFIXES):
            return lambda payload, key, name=capability: self._google(name, payload, key)
        if capability.startswith(BUFFER_PREFIX):
            return lambda payload, key, name=capability: self._buffer(name, payload, key)
        if capability.startswith(HOTMAIL_PREFIX):
            return lambda payload, key, name=capability: self._hotmail(name, payload, key)
        return {
            "task.create": self._task_create,
            "task.update": self._task_update,
            "local.read": self._local_read,
            "local.write": self._local_write,
            "web.search": self._web_search,
            "web.open": self._web_open,
            "callmemo.prepare": self._callmemo_prepare,
            "callmemo.commit": self._callmemo_commit,
        }[capability]

    def _google(self, capability: str, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        if self.google is None:
            raise AdapterError("resident Google transport is not configured")
        outbound = dict(payload)
        outbound.pop("approved_payload_hash", None)
        if capability == "gmail.draft" and outbound.get("attachments"):
            outbound["attachments"] = [str(self._bounded_path(str(path))) for path in outbound["attachments"]]
        if capability == "drive.upload":
            outbound["local_path"] = str(self._bounded_path(str(outbound["local_path"])))
        if capability == "drive.export":
            suffix = _export_suffix(str(payload["mime_type"]))
            outbound["output_path"] = str(self.artifact_root / f"{key[:24]}{suffix}")
        if capability == "gmail.attachment_download":
            outbound["output_path"] = str(_gmail_attachment_output_path(self.artifact_root, payload, key))
        before = None
        if capability == "calendar.update":
            before = self.google.execute(
                "calendar.get",
                {"calendar_id": outbound["calendar_id"], "event_id": outbound["event_id"]},
                f"before:{key}",
            )
        result = dict(self.google.execute(capability, outbound, key))
        readback = self.google.readback(capability, outbound, result)
        provider_id = _provider_id(capability, result, readback)
        artifact: dict[str, Any] = {
            "status": "ok", "capability": capability, "payload_hash": payload_hash(payload),
            "provider_id": provider_id, "result": result, "readback": readback,
            "reused": bool(result.get("reused")),
        }
        if capability == "gmail.search":
            ids = [str(row["id"]) for row in result.get("messages", []) if isinstance(row, Mapping) and row.get("id")]
            artifact["_derived_handles"] = {"gmail.search": ids}
        elif capability == "drive.list":
            ids = [str(row["id"]) for row in result.get("files", []) if isinstance(row, Mapping) and row.get("id")]
            artifact["_derived_handles"] = {"drive.list": ids}
        cleanup = _google_cleanup_payload(capability, payload, provider_id, result, before=before)
        if cleanup:
            artifact["cleanup_payload"] = cleanup
        if capability in {"drive.export", "gmail.attachment_download"} and result.get("output_path"):
            path = Path(str(result["output_path"]))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifact.update({
                "output_path": str(path),
                "path": str(path),
                "sha256": digest,
                "cleanup_payload": {"path": str(path)},
            })
            if result.get("sha256") and result.get("sha256") != digest:
                raise AdapterError(f"{capability} returned a mismatched artifact hash")
        return artifact

    def _buffer(self, capability: str, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        if self.buffer is None:
            raise AdapterError("resident Buffer transport is not configured")
        outbound = dict(payload)
        outbound.pop("approved_payload_hash", None)
        result = dict(self.buffer.execute(capability, outbound, key))
        if result.get("status") not in {"ok", "completed"}:
            raise AdapterError(str(result.get("error") or f"Buffer {capability} did not complete"))
        readback: Mapping[str, Any] = result
        provider_id = None
        if capability in {"buffer.draft", "buffer.schedule", "buffer.publish"}:
            post = result.get("post")
            if not isinstance(post, Mapping):
                raise AdapterError(f"Buffer {capability} did not return a post")
            provider_id = _provider_id(capability, post, post)
            if not provider_id:
                raise AdapterError(f"Buffer {capability} did not return a provider id")
        return {
            "status": "ok",
            "capability": capability,
            "payload_hash": payload_hash(payload),
            "provider_id": provider_id,
            "result": result,
            "readback": readback,
            "reused": bool(result.get("reused")),
        }

    def _hotmail(self, capability: str, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        if self.hotmail is None:
            raise AdapterError("resident Hotmail transport is not configured")
        outbound = dict(payload)
        outbound.pop("approved_payload_hash", None)
        if capability == "hotmail.draft" and outbound.get("attachments"):
            outbound["attachments"] = [str(self._bounded_path(str(path))) for path in outbound["attachments"]]
        if capability == "hotmail.send" and outbound.get("attachments"):
            outbound["attachments"] = [str(self._bounded_path(str(path))) for path in outbound["attachments"]]
        if capability == "hotmail.attachment_download":
            outbound["output_path"] = str(_mail_attachment_output_path(self.artifact_root, payload, key))
        result = dict(self.hotmail.execute(capability, outbound, key))
        readback = self.hotmail.readback(capability, outbound, result)
        provider_id = _provider_id(capability, result, readback)
        artifact: dict[str, Any] = {
            "status": "ok", "capability": capability, "payload_hash": payload_hash(payload),
            "provider_id": provider_id, "result": result, "readback": readback,
            "reused": bool(result.get("reused")),
        }
        if capability in {"hotmail.search", "hotmail.list"}:
            ids = [str(row["id"]) for row in result.get("messages", []) if isinstance(row, Mapping) and row.get("id")]
            artifact["_derived_handles"] = {capability: ids}
        if capability == "hotmail.attachment_download" and result.get("output_path"):
            path = Path(str(result["output_path"]))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifact.update({
                "output_path": str(path),
                "path": str(path),
                "sha256": digest,
                "cleanup_payload": {"path": str(path)},
            })
            if result.get("sha256") and result.get("sha256") != digest:
                raise AdapterError("hotmail.attachment_download returned a mismatched artifact hash")
        cleanup = _hotmail_cleanup_payload(capability, provider_id)
        if cleanup:
            artifact["cleanup_payload"] = cleanup
        return artifact

    def _initialize_tasks(self) -> None:
        self.task_database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.task_database)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS adapter_tasks (
                  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                  list_name TEXT NOT NULL, notes TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS adapter_task_updates (
                  idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, changes_json TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def _task_create(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        task_id = "task-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:20]
        with closing(sqlite3.connect(self.task_database)) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO adapter_tasks(id,idempotency_key,title,list_name,notes) VALUES(?,?,?,?,?)",
                (task_id, key, payload["title"], payload["list"], payload.get("notes", "")),
            )
            row = connection.execute(
                "SELECT id,title,list_name,notes,status FROM adapter_tasks WHERE idempotency_key=?", (key,),
            ).fetchone()
            connection.commit()
        readback = dict(zip(("task_id", "title", "list", "notes", "status"), row))
        expected = {"title": payload["title"], "list": payload["list"], "notes": payload.get("notes", "")}
        if any(readback[field] != value for field, value in expected.items()):
            raise AdapterError("idempotency key was reused with a different task payload")
        return {
            "status": "ok", "capability": "task.create", "provider_id": readback["task_id"],
            "payload_hash": payload_hash(payload), "readback": readback,
            "cleanup_payload": {"task_id": readback["task_id"], "changes": {"status": "cancelled"}},
        }

    def _task_update(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        allowed = {"title", "list", "notes", "status"}
        changes = dict(payload["changes"])
        if not changes or not set(changes) <= allowed:
            raise AdapterError("task changes contain unsupported fields")
        columns = {"list": "list_name", **{name: name for name in allowed - {"list"}}}
        with closing(sqlite3.connect(self.task_database)) as connection:
            seen = connection.execute(
                "SELECT changes_json FROM adapter_task_updates WHERE idempotency_key=?", (key,),
            ).fetchone()
            encoded = json.dumps(changes, sort_keys=True)
            if seen and seen[0] != encoded:
                raise AdapterError("idempotency key was reused with different task changes")
            if not seen:
                assignments = ",".join(f"{columns[name]}=?" for name in sorted(changes))
                values = [changes[name] for name in sorted(changes)]
                cursor = connection.execute(
                    f"UPDATE adapter_tasks SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (*values, payload["task_id"]),
                )
                if cursor.rowcount != 1:
                    raise AdapterError("task does not exist")
                connection.execute(
                    "INSERT INTO adapter_task_updates VALUES(?,?,?)", (key, payload["task_id"], encoded),
                )
            row = connection.execute(
                "SELECT id,title,list_name,notes,status FROM adapter_tasks WHERE id=?", (payload["task_id"],),
            ).fetchone()
            connection.commit()
        readback = dict(zip(("task_id", "title", "list", "notes", "status"), row))
        return {"status": "ok", "capability": "task.update", "provider_id": readback["task_id"], "payload_hash": payload_hash(payload), "readback": readback}

    def _bounded_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not any(path == root or root in path.parents for root in self.local_roots):
            raise AdapterError("local path is outside daemon allowlists")
        return path

    def _bounded_cleanup_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        roots = (*self.local_roots, self.artifact_root)
        if not any(path == root or root in path.parents for root in roots):
            raise AdapterError("cleanup path is outside daemon allowlists")
        return path

    def _local_read(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        path = self._bounded_path(str(payload["path"]))
        if not path.is_file():
            raise AdapterError("local read target is not a file")
        data = path.read_bytes()
        maximum = int(payload.get("max_bytes", len(data)))
        if len(data) > maximum:
            raise AdapterError("local file exceeds max_bytes")
        return {"status": "ok", "capability": "local.read", "path": str(path), "content": data.decode("utf-8"), "sha256": hashlib.sha256(data).hexdigest(), "payload_hash": payload_hash(payload)}

    def _local_write(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        path = self._bounded_path(str(payload["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(payload["content"]).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{key[:12]}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
        return {"status": "ok", "capability": "local.write", "path": str(path), "payload_hash": payload_hash(payload), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "cleanup_payload": {"path": str(path)}}

    def _web_search(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        sources = [_normalize_source(row) for row in self.web.search(str(payload["query"]), int(payload.get("max_results", 10)))]
        return {"status": "ok", "capability": "web.search", "payload_hash": payload_hash(payload), "sources": sources, "_derived_handles": {"web.search": [row["url"] for row in sources]}}

    def _web_open(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        source = _normalize_source(self.web.open(str(payload["url"])))
        return {"status": "ok", "capability": "web.open", "payload_hash": payload_hash(payload), "source": source}

    def _callmemo_prepare(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        if self.callmemo_prepare is None:
            raise AdapterError("callmemo prepare transport is not configured")
        result = dict(self.callmemo_prepare(payload, key))
        path = self._bounded_path(str(result.get("path") or result.get("output_path") or ""))
        if not path.is_file():
            raise AdapterError("callmemo prepare did not produce a bounded artifact")
        return {"status": "ok", "capability": "callmemo.prepare", "path": str(path), "payload_hash": payload_hash(payload), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "cleanup_payload": {"path": str(path)}, "result": result}

    def _callmemo_commit(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        if self.callmemo_commit is None:
            raise AdapterError("callmemo commit transport is not configured")
        result = dict(self.callmemo_commit(payload, key))
        provider_id = _provider_id("callmemo.commit", result, result.get("readback") if isinstance(result.get("readback"), Mapping) else None)
        if not provider_id or not isinstance(result.get("readback"), Mapping):
            raise AdapterError("callmemo commit did not return provider readback")
        return {"status": "ok", "capability": "callmemo.commit", "provider_id": provider_id, "payload_hash": payload_hash(payload), "readback": result["readback"], "result": result, "cleanup_payload": {"draft_id": provider_id}}


class ResidentGoogleTransport:
    """Thin bridge to CMD's retrying, credential-owning Google daemon."""

    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        from scripts.google_service_client import execute

        return execute(capability, dict(payload), idempotency_key)

    def readback(self, capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if capability in {"gmail.search", "gmail.read", "gmail.attachment_download", "calendar.list", "calendar.get", "drive.list", "drive.get", "drive.export", "docs.get"}:
            return result
        target = _readback_request(capability, payload, result)
        if target is None:
            return None
        read_capability, read_payload = target
        try:
            return self.execute(read_capability, read_payload, f"readback:{payload_hash(payload)}")
        except RuntimeError as error:
            detail = str(error).lower()
            if capability in {"gmail.delete_draft", "calendar.delete", "drive.delete"} and (
                "404" in detail or "not found" in detail or "does not exist" in detail
            ):
                return {"absent": True}
            raise


class ResidentBufferTransport:
    """Thin bridge to CMD's retrying, credential-owning Buffer daemon."""

    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        from scripts.buffer_service_client import execute

        return execute(capability, dict(payload), idempotency_key)


class ResidentHotmailTransport:
    """Thin bridge to CMD's retrying, credential-owning Hotmail daemon."""

    def execute(self, capability: str, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        from scripts.hotmail_service_client import execute

        return execute(capability, dict(payload), idempotency_key)

    def readback(self, capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if capability in {"hotmail.profile", "hotmail.folders", "hotmail.search", "hotmail.list", "hotmail.read", "hotmail.attachment_download"}:
            return result
        target = _hotmail_readback_request(capability, payload, result)
        if target is None:
            return None
        read_capability, read_payload = target
        try:
            return self.execute(read_capability, read_payload, f"readback:{payload_hash(payload)}")
        except RuntimeError as error:
            detail = str(error).lower()
            if capability == "hotmail.delete" and ("404" in detail or "not found" in detail or "does not exist" in detail):
                return {"absent": True}
            raise


class HttpWebTransport:
    """No-key host web transport using Bing RSS plus bounded page extraction."""

    USER_AGENT = "CMD-v2/1.0"

    def search(self, query: str, limit: int) -> Sequence[Mapping[str, Any]]:
        url = "https://www.bing.com/search?format=rss&" + urllib.parse.urlencode({"q": query})
        root = ET.fromstring(self._get(url))
        return [
            {"url": item.findtext("link", ""), "title": item.findtext("title", ""), "snippet": item.findtext("description", "")}
            for item in root.findall("./channel/item")[:limit]
        ]

    def open(self, url: str) -> Mapping[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AdapterError("web URL must be absolute HTTP(S)")
        raw = self._get(url).decode("utf-8", errors="replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))).strip()
        return {"url": url, "title": html.unescape(title.group(1)).strip() if title else parsed.netloc, "snippet": text[:4000]}

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read(2_000_000)


def _normalize_source(value: Mapping[str, Any]) -> dict[str, str]:
    url = str(value.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AdapterError("web result has no valid source URL")
    return {"url": url, "title": str(value.get("title") or parsed.netloc), "snippet": str(value.get("snippet") or "")}


def _provider_id(capability: str, result: Mapping[str, Any], readback: Mapping[str, Any] | None) -> str | None:
    candidates = ("provider_id", "draft_id", "message_id", "attachment_id", "output_path", "path", "event_id", "file_id", "id", "deleted_draft_id", "deleted_event_id", "deleted_file_id")
    for source in (result, result.get("event", {}), result.get("file", {}), result.get("message", {}), readback or {}):
        if isinstance(source, Mapping):
            for field in candidates:
                if source.get(field):
                    return str(source[field])
    return None


def _google_cleanup_payload(
    capability: str,
    payload: Mapping[str, Any],
    provider_id: str | None,
    result: Mapping[str, Any],
    *,
    before: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    if capability == "gmail.draft" and provider_id:
        return {"draft_id": provider_id}
    if capability == "calendar.create" and provider_id:
        return {"calendar_id": payload["calendar_id"], "event_id": provider_id, "approved_payload_hash": "cleanup" * 11}
    if capability == "calendar.update" and provider_id and before:
        event = before.get("event", before)
        if isinstance(event, Mapping):
            changes = _calendar_restore_changes(dict(payload.get("changes") or {}), event)
            return {"calendar_id": payload["calendar_id"], "event_id": provider_id, "changes": changes, "approved_payload_hash": "cleanup" * 11}
    if capability in {"drive.create", "drive.copy", "drive.upload"} and provider_id:
        return {"file_id": provider_id, "approved_payload_hash": "cleanup" * 11}
    return None


def _hotmail_cleanup_payload(capability: str, provider_id: str | None) -> Mapping[str, Any] | None:
    if capability == "hotmail.draft" and provider_id:
        return {"message_id": provider_id, "approved_payload_hash": "cleanup" * 11}
    return None


def _calendar_restore_changes(changes: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for field in changes:
        if field == "title":
            restored[field] = event.get("summary", "")
        elif field in {"start", "end"}:
            value = event.get(field, {})
            restored[field] = value.get("dateTime") if isinstance(value, Mapping) else None
        elif field == "timezone":
            value = event.get("start", {})
            restored[field] = value.get("timeZone") if isinstance(value, Mapping) else None
        elif field == "attendees":
            restored[field] = [row.get("email") for row in event.get("attendees", []) if isinstance(row, Mapping)]
        else:
            restored[field] = event.get(field, "")
    return restored


def _readback_request(capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    provider_id = _provider_id(capability, result, None)
    if capability == "gmail.draft" and provider_id:
        return "gmail.get_draft", {"draft_id": provider_id}
    if capability == "gmail.send" and provider_id:
        return "gmail.read", {"message_id": provider_id}
    if capability == "gmail.delete_draft":
        return "gmail.get_draft", {"draft_id": payload["draft_id"]}
    if capability in {"calendar.create", "calendar.update"} and provider_id:
        return "calendar.get", {"calendar_id": payload["calendar_id"], "event_id": provider_id}
    if capability == "calendar.delete":
        return "calendar.get", {"calendar_id": payload["calendar_id"], "event_id": payload["event_id"]}
    if capability in {"drive.create", "drive.copy", "drive.upload"} and provider_id:
        return "drive.get", {"file_id": provider_id}
    if capability == "drive.delete":
        return "drive.get", {"file_id": payload["file_id"]}
    return None


def _hotmail_readback_request(capability: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    provider_id = _provider_id(capability, result, None)
    if capability == "hotmail.draft" and provider_id:
        return "hotmail.read", {"message_id": provider_id}
    if capability == "hotmail.move" and provider_id:
        return "hotmail.read", {"message_id": provider_id}
    if capability == "hotmail.delete":
        return "hotmail.read", {"message_id": payload["message_id"]}
    return None


def _export_suffix(mime_type: str) -> str:
    return {
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }.get(mime_type, ".bin")


def _gmail_attachment_output_path(root: Path, payload: Mapping[str, Any], key: str) -> Path:
    return _mail_attachment_output_path(root, payload, key)


def _mail_attachment_output_path(root: Path, payload: Mapping[str, Any], key: str) -> Path:
    filename = Path(str(payload.get("filename") or "")).name.strip()
    if not filename:
        selector = str(payload.get("attachment_id") or "attachment")[:32]
        filename = f"{selector}.bin"
    filename = re.sub(r"[^A-Za-z0-9 ._()\\[\\]\\-+@,&<>]", "_", filename).strip(" .")
    filename = (filename or "attachment.bin")[:180]
    return root / f"{key[:24]}-{filename}"


def _material_mismatches(
    capability: str, payload: Mapping[str, Any], readback: Any
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(readback, Mapping):
        return {"readback": {"expected": "mapping", "actual": type(readback).__name__}}
    actual: Mapping[str, Any] = readback
    expected: dict[str, Any] = {}
    if capability in {"gmail.draft", "gmail.send"}:
        fields = ("to", "cc", "bcc", "subject", "body")
        expected = {field: payload.get(field, [] if field in {"to", "cc", "bcc"} else "") for field in fields}
        actual = {field: readback.get(field, [] if field in {"to", "cc", "bcc"} else "") for field in fields}
        for field in ("to", "cc", "bcc"):
            expected[field] = _normalize_mailboxes(expected[field])
            actual[field] = _normalize_mailboxes(actual[field])
        if payload.get("thread_id"):
            expected["thread_id"] = payload["thread_id"]
            actual["thread_id"] = readback.get("thread_id")
    elif capability == "gmail.read":
        expected = {"id": payload.get("message_id")}
    elif capability == "hotmail.read":
        message = readback.get("message", readback)
        actual = message if isinstance(message, Mapping) else {}
        expected = {"id": payload.get("message_id")}
    elif capability == "hotmail.draft":
        message = readback.get("message", readback)
        if not isinstance(message, Mapping):
            return {"message": {"expected": "mapping", "actual": type(message).__name__}}
        actual = {
            "subject": message.get("subject"),
            "body": message.get("body", ""),
            "to": _normalize_mailboxes(message.get("to", [])),
            "cc": _normalize_mailboxes(message.get("cc", [])),
        }
        expected = {
            "subject": payload.get("subject"),
            "body": payload.get("body"),
            "to": _normalize_mailboxes(payload.get("to", [])),
            "cc": _normalize_mailboxes(payload.get("cc", [])),
        }
    elif capability == "hotmail.move":
        message = readback.get("message", readback)
        actual = message if isinstance(message, Mapping) else {}
        expected = {"id": payload.get("message_id")}
    elif capability == "calendar.create":
        event = readback.get("event", readback)
        if not isinstance(event, Mapping):
            return {"event": {"expected": "mapping", "actual": type(event).__name__}}
        actual = {
            "title": event.get("summary"),
            "start": event.get("start", {}).get("dateTime") if isinstance(event.get("start"), Mapping) else None,
            "end": event.get("end", {}).get("dateTime") if isinstance(event.get("end"), Mapping) else None,
            "timezone": event.get("start", {}).get("timeZone") if isinstance(event.get("start"), Mapping) else None,
            "attendees": [row.get("email") for row in event.get("attendees", []) if isinstance(row, Mapping)],
            "description": event.get("description", ""),
            "location": event.get("location", ""),
        }
        expected = {field: payload.get(field, [] if field == "attendees" else "") for field in actual}
    elif capability == "calendar.update":
        event = readback.get("event", readback)
        expected = dict(payload.get("changes") or {})
        if not isinstance(event, Mapping):
            actual = {}
        else:
            actual = dict(event)
            if "title" in expected:
                actual["title"] = event.get("summary")
            if "start" in expected and isinstance(event.get("start"), Mapping):
                actual["start"] = event["start"].get("dateTime")
            if "end" in expected and isinstance(event.get("end"), Mapping):
                actual["end"] = event["end"].get("dateTime")
            if "timezone" in expected and isinstance(event.get("start"), Mapping):
                actual["timezone"] = event["start"].get("timeZone")
            if "attendees" in expected:
                actual["attendees"] = [row.get("email") for row in event.get("attendees", []) if isinstance(row, Mapping)]
    elif capability == "calendar.get":
        event = readback.get("event", readback)
        actual = event if isinstance(event, Mapping) else {}
        expected = {"id": payload.get("event_id")}
    elif capability in {"drive.create", "drive.copy", "drive.upload"}:
        file_value = readback.get("file", readback)
        if not isinstance(file_value, Mapping):
            return {"file": {"expected": "mapping", "actual": type(file_value).__name__}}
        actual = {
            "name": file_value.get("name"),
            "parent_id": (file_value.get("parents") or [None])[0],
        }
        expected = {"name": payload.get("name"), "parent_id": payload.get("parent_id")}
        if capability != "drive.copy":
            actual["mime_type"] = file_value.get("mimeType")
            expected["mime_type"] = payload.get("mime_type")
    elif capability == "drive.get":
        file_value = readback.get("file", readback)
        actual = file_value if isinstance(file_value, Mapping) else {}
        expected = {"id": payload.get("file_id")}
    elif capability == "docs.get":
        document = readback.get("document", readback)
        actual = document if isinstance(document, Mapping) else {}
        expected = {"documentId": payload.get("document_id")}
    elif capability == "task.create":
        expected = {"title": payload.get("title"), "list": payload.get("list"), "notes": payload.get("notes", "")}
    elif capability == "task.update":
        expected = dict(payload.get("changes") or {})
    else:
        return {}
    return {
        field: {"expected": expected_value, "actual": actual.get(field)}
        for field, expected_value in expected.items()
        if actual.get(field) != expected_value
    }


def _normalize_mailboxes(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [address.casefold() for _, address in getaddresses(str(item) for item in values) if address]
