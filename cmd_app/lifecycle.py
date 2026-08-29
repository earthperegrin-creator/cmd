"""One-command local lifecycle for CMD."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cmd_app import config, onboarding
import cmd_db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def state_path(value: str = "") -> Path:
    return Path(value).expanduser() if value else config.state_dir(ROOT)


def runtime_file(state: Path) -> Path:
    return state / "runtime.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/api/health"


def health(
    host: str,
    port: int,
    timeout: float = 0.6,
    *,
    expected_state: Path | None = None,
    expected_root: Path | None = ROOT,
) -> dict[str, Any] | None:
    try:
        headers = {}
        auth_token = os.environ.get("CMD_AUTH_TOKEN", "").strip()
        if auth_token:
            headers["X-CMD-Token"] = auth_token
        request = urllib.request.Request(health_url(host, port), headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    if expected_root is not None:
        try:
            if Path(str(payload.get("root") or "")).resolve() != expected_root.resolve():
                return None
        except OSError:
            return None
    if expected_state is not None:
        try:
            expected_queue = (expected_state / "actions.jsonl").resolve()
            if Path(str(payload.get("queue") or "")).resolve() != expected_queue:
                return None
        except OSError:
            return None
    return payload


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def process_matches_server(pid: int, root: Path = ROOT) -> bool:
    """Verify a recorded PID still names this checkout's server process."""
    if not pid_is_alive(pid):
        return False
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return str((root / "server.py").resolve()) in command


def enforce_private_state_permissions(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)


def load_profile_argument(path: str) -> dict[str, Any]:
    if not path:
        return interactive_profile()
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile JSON must contain an object")
    return payload


def interactive_profile() -> dict[str, Any]:
    print("CMD onboarding asks exactly three questions. You can edit every answer before continuing.\n")
    sources = input("1/3 Where may CMD learn about you? Enter paths/labels separated by commas, or 'none': ").strip()
    identity = input("2/3 What should CMD call you, and what short profile summary should it use? ").strip()
    outcomes = input("3/3 What outcomes matter in the next 90 days? Separate them with semicolons: ").strip()
    first_name, _, summary = identity.partition(":")
    if not summary:
        words = identity.split(maxsplit=1)
        first_name = words[0] if words else ""
        summary = words[1] if len(words) > 1 else identity
    return {
        "first_name": first_name.strip(),
        "summary": summary.strip(),
        "authorized_sources": [] if sources.lower() == "none" else sources,
        "outcomes_90_days": outcomes,
    }


def setup(args: argparse.Namespace) -> dict[str, Any]:
    state = state_path(args.state_dir)
    profile = load_profile_argument(args.profile_json)
    enforce_private_state_permissions(state)
    result = onboarding.initialize_private_layer(state, profile, agent=args.agent)
    if not args.no_start:
        result["runtime"] = start_service(state, args.host, args.port, open_browser=not args.no_open)
    return result


def start_service(state: Path, host: str, port: int, *, open_browser: bool = True) -> dict[str, Any]:
    existing = health(host, port, expected_state=state)
    if existing:
        if open_browser:
            webbrowser.open(f"http://{host}:{port}/")
        return {"ok": True, "status": "already_running", "url": f"http://{host}:{port}/"}
    same_state_from_another_root = health(
        host,
        port,
        expected_state=state,
        expected_root=None,
    )
    if same_state_from_another_root:
        stopped = stop_service(state)
        if stopped.get("status") != "stopped":
            raise RuntimeError("the workspace is running from a stale checkout that CMD could not stop safely")
    if health(host, port, expected_root=None):
        raise RuntimeError(f"port {port} is already serving a different CMD workspace")
    enforce_private_state_permissions(state)
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    output = (logs / "server.log").open("ab")
    env = os.environ.copy()
    env["CMD_STATE_DIR"] = str(state.resolve())
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--host", host, "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output.close()
    write_json(runtime_file(state), {
        "pid": process.pid,
        "host": host,
        "port": port,
        "root": str(ROOT.resolve()),
        "state_dir": str(state.resolve()),
        "started_at": utc_stamp(),
    })
    for _ in range(40):
        if health(host, port, timeout=0.2, expected_state=state):
            if open_browser:
                webbrowser.open(f"http://{host}:{port}/")
            return {"ok": True, "status": "started", "pid": process.pid, "url": f"http://{host}:{port}/"}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"CMD did not start; inspect {logs / 'server.log'}")


def stop_service(state: Path) -> dict[str, Any]:
    runtime = read_json(runtime_file(state))
    pid = int(runtime.get("pid") or 0)
    if not pid_is_alive(pid):
        runtime_file(state).unlink(missing_ok=True)
        return {"ok": True, "status": "not_running"}
    recorded_root = str(runtime.get("root") or "").strip()
    recorded_state = str(runtime.get("state_dir") or "").strip()
    if recorded_state and Path(recorded_state).resolve() != state.resolve():
        raise RuntimeError("refusing to stop a process recorded for a different CMD workspace")
    process_root = Path(recorded_root) if recorded_root else ROOT
    if not process_matches_server(pid, process_root):
        runtime_file(state).unlink(missing_ok=True)
        return {"ok": True, "status": "stale_runtime", "pid": pid}
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_is_alive(pid):
            break
        time.sleep(0.1)
    if pid_is_alive(pid):
        raise RuntimeError(f"CMD process {pid} did not stop cleanly")
    runtime_file(state).unlink(missing_ok=True)
    return {"ok": True, "status": "stopped", "pid": pid}


def status(state: Path, host: str, port: int) -> dict[str, Any]:
    runtime = read_json(runtime_file(state))
    live = health(host, port, expected_state=state)
    return {
        "ok": True,
        "status": "running" if live else "stopped",
        "url": f"http://{host}:{port}/",
        "state_dir": str(state.resolve()),
        "pid": runtime.get("pid"),
        "profile_exists": (state / "profile.json").exists(),
        "database_exists": (state / "cmd.db").exists(),
    }


def doctor(state: Path, host: str, port: int) -> dict[str, Any]:
    server_health = health(host, port, expected_state=state)
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 11), "detail": sys.version.split()[0]},
        {"name": "state", "ok": state.exists() and os.access(state, os.W_OK), "detail": str(state.resolve())},
        {"name": "profile", "ok": (state / "profile.json").exists(), "detail": str(state / "profile.json")},
        {"name": "database", "ok": (state / "cmd.db").exists(), "detail": str(state / "cmd.db")},
        {"name": "codex", "ok": shutil.which("codex") is not None, "detail": shutil.which("codex") or "not found"},
        {"name": "claude", "ok": shutil.which("claude") is not None, "detail": shutil.which("claude") or "not found"},
        {"name": "server", "ok": server_health is not None, "detail": health_url(host, port)},
    ]
    required = {"python", "state", "profile", "database"}
    return {"ok": all(check["ok"] for check in checks if check["name"] in required), "checks": checks}


def backup(state: Path, destination: str = "") -> dict[str, Any]:
    if not state.exists():
        raise ValueError(f"state directory does not exist: {state}")
    backup_dir = Path(destination).expanduser() if destination else state / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_dir == state / "backups":
        backup_dir.chmod(0o700)
    archive = backup_dir / f"cmd-backup-{utc_stamp()}.tar.gz"
    suffix = 2
    while archive.exists():
        archive = backup_dir / f"cmd-backup-{utc_stamp()}-{suffix}.tar.gz"
        suffix += 1
    with tempfile.TemporaryDirectory(prefix="cmd-backup-") as tmpdir:
        snapshot_db = Path(tmpdir) / "cmd.db"
        source_db = state / "cmd.db"
        if source_db.exists():
            with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(snapshot_db)) as snapshot:
                source.backup(snapshot)
        with tarfile.open(archive, "w:gz") as bundle:
            for path in state.rglob("*"):
                if (
                    path == backup_dir
                    or backup_dir in path.parents
                    or path.name in {"runtime.json", "cmd.db", "cmd.db-wal", "cmd.db-shm"}
                ):
                    continue
                bundle.add(path, arcname=path.relative_to(state), recursive=False)
            if snapshot_db.exists():
                bundle.add(snapshot_db, arcname="cmd.db", recursive=False)
    archive.chmod(0o600)
    return {"ok": True, "archive": str(archive.resolve())}


def restore(state: Path, archive_value: str) -> dict[str, Any]:
    archive = Path(archive_value).expanduser().resolve()
    if not archive.is_file():
        raise ValueError(f"backup does not exist: {archive}")
    if state.exists() and any(state.iterdir()):
        raise ValueError("restore target must be empty; preserve or move the current state first")
    state.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                raise ValueError("backup contains a symbolic or hard link")
            target = (state / member.name).resolve()
            if state.resolve() not in target.parents and target != state.resolve():
                raise ValueError("backup contains an unsafe path")
            if sys.version_info >= (3, 12):
                bundle.extract(member, state, filter="data")
            else:
                bundle.extract(member, state)
    return {"ok": True, "state_dir": str(state.resolve()), "archive": str(archive)}


def update_product(state: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    if not (ROOT / ".git").exists():
        raise ValueError("this CMD installation is not a Git checkout")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError("update refused because the product checkout has local changes")
    runtime = read_json(runtime_file(state))
    runtime_host = str(runtime.get("host") or host)
    runtime_port = int(runtime.get("port") or port)
    was_running = health(runtime_host, runtime_port, expected_state=state) is not None
    saved = backup(state)
    stopped = False
    if was_running:
        stop_service(state)
        stopped = True
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        cmd_db.init_db(state / "cmd.db")
    except Exception:
        if stopped:
            start_service(state, runtime_host, runtime_port, open_browser=False)
        raise
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    restarted = False
    if was_running:
        start_service(state, runtime_host, runtime_port, open_browser=False)
        restarted = True
    return {
        "ok": True,
        "backup": saved["archive"],
        "status": "updated",
        "before": before,
        "after": after,
        "restarted": restarted,
    }


def uninstall(state: Path, *, purge_data: bool = False) -> dict[str, Any]:
    if purge_data:
        resolved = state.resolve()
        if resolved in {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}:
            raise ValueError("refusing to purge a broad directory")
    stopped = stop_service(state)
    if purge_data:
        shutil.rmtree(resolved, ignore_errors=False)
        return {"ok": True, "status": "uninstalled", "data": "deleted", "stopped": stopped}
    return {"ok": True, "status": "uninstalled", "data": f"preserved at {state.resolve()}", "stopped": stopped}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="cmd", description="Set up and operate local CMD.")
    command.add_argument("--state-dir", default="", help="Override private CMD state directory.")
    sub = command.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="Create the private profile and start CMD.")
    setup_parser.add_argument("--profile-json", default="", help="Confirmed three-question onboarding response.")
    setup_parser.add_argument("--agent", choices=["none", "codex", "claude"], default="none")
    setup_parser.add_argument("--host", default=DEFAULT_HOST)
    setup_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    setup_parser.add_argument("--no-start", action="store_true")
    setup_parser.add_argument("--no-open", action="store_true")
    for name in ("start", "status", "doctor"):
        item = sub.add_parser(name)
        item.add_argument("--host", default=DEFAULT_HOST)
        item.add_argument("--port", type=int, default=DEFAULT_PORT)
        if name == "start":
            item.add_argument("--no-open", action="store_true")
    sub.add_parser("stop")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--destination", default="")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("archive")
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--host", default=DEFAULT_HOST)
    update_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--purge-data", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state = state_path(args.state_dir)
    try:
        if args.command == "setup":
            result = setup(args)
        elif args.command == "start":
            result = start_service(state, args.host, args.port, open_browser=not args.no_open)
        elif args.command == "stop":
            result = stop_service(state)
        elif args.command == "status":
            result = status(state, args.host, args.port)
        elif args.command == "doctor":
            result = doctor(state, args.host, args.port)
        elif args.command == "backup":
            result = backup(state, args.destination)
        elif args.command == "restore":
            result = restore(state, args.archive)
        elif args.command == "update":
            result = update_product(state, args.host, args.port)
        else:
            result = uninstall(state, purge_data=args.purge_data)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1
