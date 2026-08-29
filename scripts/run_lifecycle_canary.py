#!/usr/bin/env python3
"""Exercise CMD's complete local lifecycle in a disposable installation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "evals" / "reports" / "lifecycle-canary-latest.json"
PROFILE = {
    "first_name": "Maya",
    "summary": "A fictional independent strategy consultant.",
    "authorized_sources": ["The isolated lifecycle canary workspace only"],
    "outcomes_90_days": [
        "Deliver a fictional market-entry recommendation",
        "Standardize the fictional consulting practice",
    ],
}


def clean_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CMD_")}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 90,
    expect_json: bool = False,
) -> dict[str, Any] | subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{' '.join(command[:3])}: {message[-1200:]}")
    if not expect_json:
        return completed
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"command returned invalid JSON: {error}") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"command did not return an ok receipt: {payload}")
    return payload


def stage_product(destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for encoded in listed:
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        source = ROOT / relative
        target = destination / relative
        if source.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)


def git(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    run(["git", *command], cwd=cwd, env=env)


def initialize_disposable_remote(install: Path, remote: Path, updater: Path, env: dict[str, str]) -> None:
    git(["init", "-b", "main"], install, env)
    git(["config", "user.name", "CMD Lifecycle Canary"], install, env)
    git(["config", "user.email", "canary@example.invalid"], install, env)
    git(["add", "."], install, env)
    git(["commit", "-m", "Lifecycle canary baseline"], install, env)
    git(["init", "--bare", "--initial-branch=main", str(remote)], install.parent, env)
    git(["remote", "add", "origin", str(remote)], install, env)
    git(["push", "-u", "origin", "main"], install, env)
    git(["clone", "--branch", "main", str(remote), str(updater)], install.parent, env)
    git(["config", "user.name", "CMD Lifecycle Canary"], updater, env)
    git(["config", "user.email", "canary@example.invalid"], updater, env)
    (updater / "LIFECYCLE-CANARY.txt").write_text("disposable update reached\n", encoding="utf-8")
    git(["add", "LIFECYCLE-CANARY.txt"], updater, env)
    git(["commit", "-m", "Disposable lifecycle update"], updater, env)
    git(["push", "origin", "main"], updater, env)


def health(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("health endpoint did not return ok")
    return payload


def capture_fictional_outcome(port: int) -> dict[str, Any]:
    body = json.dumps({
        "text": "Prepare the fictional lifecycle client briefing",
        "metadata": {"visibleFilter": "work"},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks/capture",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("fictional Outcome capture was not verified")
    return payload


def outcome_titles(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT title FROM work_items WHERE parent_item_id IS NULL").fetchall()
    return {str(row[0]) for row in rows}


def stop_quietly(install: Path, state: Path, env: dict[str, str]) -> None:
    command = [sys.executable, str(install / "cmd"), "--state-dir", str(state), "stop"]
    try:
        subprocess.run(command, cwd=install, env=env, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def sanitize_error(error: Exception, suite: Path) -> str:
    return str(error).replace(str(suite), "<suite>").replace(str(ROOT), "<product>")[-1600:]


def execute(*, keep_workspace: bool = False) -> tuple[dict[str, Any], Path]:
    suite = Path(tempfile.mkdtemp(prefix="cmd-lifecycle-canary-"))
    install = suite / "install"
    moved_install = suite / "moved-install"
    remote = suite / "remote.git"
    updater = suite / "updater"
    state = suite / "private-state"
    restored = suite / "restored-state"
    purge_state = suite / "purge-state"
    archives = suite / "archives"
    profile = suite / "profile.json"
    env = clean_environment()
    port = free_port()
    restore_port = free_port()
    purge_port = free_port()
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise RuntimeError(f"{name}: {detail}")

    try:
        install.mkdir()
        stage_product(install)
        initialize_disposable_remote(install, remote, updater, env)
        profile.write_text(json.dumps(PROFILE, indent=2) + "\n", encoding="utf-8")
        cli = [sys.executable, str(install / "cmd"), "--state-dir", str(state)]

        setup_receipt = run(
            [*cli, "setup", "--profile-json", str(profile), "--agent", "none", "--port", str(port), "--no-open"],
            cwd=install,
            env=env,
            expect_json=True,
        )
        record("fresh_setup", setup_receipt.get("outcomes_created") == 2, "created the confirmed fictional Outcomes")
        record("private_permissions", (state.stat().st_mode & 0o777) == 0o700, "private state is owner-only")
        live = health(port)
        record("start_and_health", Path(str(live.get("queue") or "")).resolve() == (state / "actions.jsonl").resolve(), "server uses the intended private state")

        doctor = run([*cli, "doctor", "--port", str(port)], cwd=install, env=env, expect_json=True)
        required = {row["name"]: row["ok"] for row in doctor.get("checks") or []}
        record(
            "doctor",
            doctor.get("ok") is True and all(
                required.get(name)
                for name in ("python", "state", "profile", "database", "selected_worker", "server")
            ),
            "the server and configured worker path are included in the required checks",
        )

        capture_fictional_outcome(port)
        expected_title = "Prepare the fictional lifecycle client briefing"
        record("durable_user_data", expected_title in outcome_titles(state / "cmd.db"), "verified a user-created fictional Outcome")

        backup_receipt = run([*cli, "backup", "--destination", str(archives)], cwd=install, env=env, expect_json=True)
        archive = Path(str(backup_receipt["archive"]))
        with tarfile.open(archive, "r:gz") as bundle:
            members = set(bundle.getnames())
        record("consistent_backup", {"cmd.db", "profile.json"}.issubset(members) and (archive.stat().st_mode & 0o777) == 0o600, "backup contains private state and is owner-readable only")

        stopped = run([*cli, "stop"], cwd=install, env=env, expect_json=True)
        status = run([*cli, "status", "--port", str(port)], cwd=install, env=env, expect_json=True)
        record("stop", stopped.get("status") == "stopped" and status.get("status") == "stopped", "service stops and reports stopped")
        run([*cli, "start", "--port", str(port), "--no-open"], cwd=install, env=env, expect_json=True)
        record("restart", health(port).get("ok") is True, "service restarts against preserved state")

        install.rename(moved_install)
        install = moved_install
        cli = [sys.executable, str(install / "cmd"), "--state-dir", str(state)]
        run([*cli, "start", "--port", str(port), "--no-open"], cwd=install, env=env, expect_json=True)
        moved_health = health(port)
        record("repository_move_repair", Path(str(moved_health.get("root") or "")).resolve() == install.resolve(), "a moved checkout replaces only its own stale server and repairs runtime paths")

        update = run([*cli, "update", "--port", str(port)], cwd=install, env=env, expect_json=True)
        record("safe_update", update.get("before") != update.get("after") and update.get("restarted") is True and (install / "LIFECYCLE-CANARY.txt").exists(), "backup, fast-forward update, migration, and restart completed")
        record("update_preserves_data", expected_title in outcome_titles(state / "cmd.db"), "user Outcome survived update")

        uninstall = run([*cli, "uninstall"], cwd=install, env=env, expect_json=True)
        record("uninstall_preserves_data", uninstall.get("data", "").startswith("preserved") and (state / "cmd.db").exists(), "uninstall stops services without deleting private data")

        reinstall = run(
            [*cli, "setup", "--profile-json", str(profile), "--agent", "none", "--port", str(port), "--no-open"],
            cwd=install,
            env=env,
            expect_json=True,
        )
        record("reinstall_preserves_data", reinstall.get("outcomes_created") == 0 and expected_title in outcome_titles(state / "cmd.db") and health(port).get("ok") is True, "reinstall reuses existing Outcomes without duplication")
        run([*cli, "stop"], cwd=install, env=env, expect_json=True)

        restore_cli = [sys.executable, str(install / "cmd"), "--state-dir", str(restored)]
        run([*restore_cli, "restore", str(archive)], cwd=install, env=env, expect_json=True)
        run([*restore_cli, "start", "--port", str(restore_port), "--no-open"], cwd=install, env=env, expect_json=True)
        record("restore", expected_title in outcome_titles(restored / "cmd.db") and health(restore_port).get("ok") is True, "backup restores into a fresh runnable workspace")
        run([*restore_cli, "uninstall"], cwd=install, env=env, expect_json=True)

        purge_cli = [sys.executable, str(install / "cmd"), "--state-dir", str(purge_state)]
        run(
            [*purge_cli, "setup", "--profile-json", str(profile), "--agent", "none", "--port", str(purge_port), "--no-start", "--no-open"],
            cwd=install,
            env=env,
            expect_json=True,
        )
        purged = run([*purge_cli, "uninstall", "--purge-data"], cwd=install, env=env, expect_json=True)
        record("explicit_purge", purged.get("data") == "deleted" and not purge_state.exists(), "private data deletion occurs only through the explicit purge flag")
    except Exception as error:  # noqa: BLE001 - the report must retain any lifecycle failure
        errors.append(sanitize_error(error, suite))
    finally:
        stop_quietly(install, state, env)
        stop_quietly(install, restored, env)
        stop_quietly(install, purge_state, env)

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canary": "disposable_full_local_lifecycle",
        "passed": not errors and checks and all(check["passed"] for check in checks),
        "checks": checks,
        "errors": errors,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not keep_workspace:
        shutil.rmtree(suite)
    return report, suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CMD's complete local lifecycle in a disposable installation.")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args(argv)
    report, suite = execute(keep_workspace=args.keep_workspace)
    output = dict(report)
    if args.keep_workspace:
        output["workspace"] = str(suite)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
