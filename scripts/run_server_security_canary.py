#!/usr/bin/env python3
"""Adversarial live-server canary for CMD's local trust boundary."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "evals" / "reports" / "server-security-canary-latest.json"
PROFILE = {
    "first_name": "Maya",
    "summary": "A fictional independent consultant.",
    "authorized_sources": ["The isolated security canary only"],
    "outcomes_90_days": ["Review a fictional client strategy"],
}
SENSITIVE_PATHS = (
    "/.cmd/cmd.db",
    "/.git/config",
    "/server.py",
    "/cmd_app/lifecycle.py",
    "/../server.py",
    "/%2e%2e/server.py",
    "/index.html/../server.py",
)


def clean_environment(state: Path, token: str = "") -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CMD_")}
    env["CMD_STATE_DIR"] = str(state)
    env["CMD_WEEKLY_LOG_ROOT"] = str(state / "empty-weekly-context")
    env["PYTHONUNBUFFERED"] = "1"
    if token:
        env["CMD_AUTH_TOKEN"] = token
    return env


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        effective = {"Host": f"127.0.0.1:{port}", **(headers or {})}
        connection.request(method, path, body=body, headers=effective)
        response = connection.getresponse()
        content = response.read()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, content
    finally:
        connection.close()


def start_server(state: Path, host: str, port: int, token: str = "") -> tuple[subprocess.Popen[bytes], Any]:
    logs = tempfile.TemporaryFile()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--host", host, "--port", str(port), "--no-watch"],
        cwd=ROOT,
        env=clean_environment(state, token),
        stdin=subprocess.DEVNULL,
        stdout=logs,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    headers = {"X-CMD-Token": token} if token else {}
    for _ in range(60):
        if process.poll() is not None:
            logs.seek(0)
            raise RuntimeError(logs.read().decode("utf-8", errors="replace")[-1200:])
        try:
            if request(port, "GET", "/api/health", headers=headers)[0] == 200:
                return process, logs
        except OSError:
            pass
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=3)
    raise RuntimeError("server did not become healthy")


def stop_server(process: subprocess.Popen[bytes] | None, logs: Any | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if logs is not None:
        logs.close()


def approval_boundary(state: Path) -> bool:
    code = """
import json
import server
send = {"id":"send","metadata":{"proposed_operation":{"capability":"gmail.send","risk_level":"external_commit","execution_mode":"execute","payload":{"to":"person@example.invalid","subject":"Fictional","body":"Draft"}}}}
prepare = {"id":"prepare","metadata":{"proposed_operation":{"capability":"calendar.create","risk_level":"external_commit","execution_mode":"prepare_only","payload":{"title":"Fictional review"}}}}
print(json.dumps({"send_requires":server.action_requires_approval(send,{"autonomy_policy":"only_destructive"}),"send_can_run":server.action_can_run(send,set()),"prepare_can_run":server.action_can_run(prepare,set())}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=clean_environment(state),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if completed.returncode:
        return False
    payload = json.loads(completed.stdout)
    return payload == {"send_requires": True, "send_can_run": False, "prepare_can_run": True}


def execute() -> dict[str, Any]:
    suite = Path(tempfile.mkdtemp(prefix="cmd-server-security-canary-"))
    state = suite / "state"
    profile = suite / "profile.json"
    loopback_port = free_port()
    remote_port = free_port()
    refused_port = free_port()
    token = "fictional-local-canary-token"
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    profile.write_text(json.dumps(PROFILE, indent=2) + "\n", encoding="utf-8")
    setup = subprocess.run(
        [sys.executable, str(ROOT / "cmd"), "--state-dir", str(state), "setup", "--profile-json", str(profile), "--agent", "none", "--no-start", "--no-open"],
        cwd=ROOT,
        env=clean_environment(state),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if setup.returncode:
        errors.append("isolated setup failed")

    process: subprocess.Popen[bytes] | None = None
    logs = None
    try:
        process, logs = start_server(state, "127.0.0.1", loopback_port)
        root_status, root_headers, _ = request(loopback_port, "GET", "/")
        record("intended_routes", root_status == 200 and request(loopback_port, "GET", "/api/health")[0] == 200, "application shell and intended API are served")
        record(
            "security_headers",
            root_headers.get("x-content-type-options") == "nosniff"
            and root_headers.get("referrer-policy") == "no-referrer"
            and root_headers.get("cache-control") == "no-store"
            and "frame-ancestors 'none'" in root_headers.get("content-security-policy", ""),
            "responses disable sniffing, referrers, framing, and caching",
        )
        get_statuses = {path: request(loopback_port, "GET", path)[0] for path in SENSITIVE_PATHS}
        record("get_allowlist", all(status == 404 for status in get_statuses.values()), "GET cannot fetch state, Git, source, or traversal paths")
        head_statuses = {path: request(loopback_port, "HEAD", path)[0] for path in SENSITIVE_PATHS}
        record("head_allowlist", all(status == 404 for status in head_statuses.values()), "HEAD cannot reveal repository files or path metadata")
        hostile_host = request(loopback_port, "GET", "/api/health", headers={"Host": "attacker.example"})[0]
        record("host_header_guard", hostile_host == 403, "loopback rejects a non-local Host header without authentication")
        cross_origin = request(
            loopback_port,
            "POST",
            "/api/not-a-route",
            headers={"Origin": "https://attacker.example", "Content-Type": "application/json"},
            body=b"{}",
        )[0]
        same_origin = request(
            loopback_port,
            "POST",
            "/api/not-a-route",
            headers={"Origin": f"http://127.0.0.1:{loopback_port}", "Content-Type": "application/json"},
            body=b"{}",
        )[0]
        record("origin_guard", cross_origin == 403 and same_origin == 404, "cross-origin mutations are rejected before routing")
    except Exception as error:  # noqa: BLE001 - retain the live trust-boundary failure
        errors.append(f"loopback boundary: {str(error)[-800:]}")
    finally:
        stop_server(process, logs)

    refused = subprocess.run(
        [sys.executable, str(ROOT / "server.py"), "--host", "0.0.0.0", "--port", str(refused_port), "--no-watch"],
        cwd=ROOT,
        env=clean_environment(state),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    record("non_loopback_default_deny", refused.returncode != 0 and "requires CMD_AUTH_TOKEN" in refused.stderr, "non-loopback startup fails without an explicit token")

    process = None
    logs = None
    try:
        process, logs = start_server(state, "0.0.0.0", remote_port, token)
        unauthorized = request(remote_port, "GET", "/api/health")[0]
        wrong = request(remote_port, "GET", "/api/health", headers={"X-CMD-Token": "wrong"})[0]
        token_status = request(remote_port, "GET", "/api/health", headers={"X-CMD-Token": token})[0]
        bearer_status = request(remote_port, "GET", "/api/health", headers={"Authorization": f"Bearer {token}"})[0]
        record("authenticated_non_loopback", unauthorized == 403 and wrong == 403 and token_status == 200 and bearer_status == 200, "non-loopback requests require the exact configured token")
        bad_origin = request(
            remote_port,
            "POST",
            "/api/not-a-route",
            headers={"X-CMD-Token": token, "Origin": "https://attacker.example", "Content-Type": "application/json"},
            body=b"{}",
        )[0]
        good_origin = request(
            remote_port,
            "POST",
            "/api/not-a-route",
            headers={"X-CMD-Token": token, "Origin": f"http://127.0.0.1:{remote_port}", "Content-Type": "application/json"},
            body=b"{}",
        )[0]
        record("authenticated_origin_guard", bad_origin == 403 and good_origin == 404, "authentication does not bypass mutation-origin validation")
    except Exception as error:  # noqa: BLE001
        errors.append(f"authenticated boundary: {str(error)[-800:]}")
    finally:
        stop_server(process, logs)

    try:
        record("external_effect_approval", approval_boundary(state), "external execution remains blocked while preparation may run")
    except Exception as error:  # noqa: BLE001
        errors.append(f"approval boundary: {str(error)[-800:]}")

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canary": "adversarial_local_server_trust_boundary",
        "passed": not errors and checks and all(check["passed"] for check in checks),
        "checks": checks,
        "errors": errors,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(suite)
    return report


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Validate CMD's live local-server trust boundary.").parse_args(argv)
    report = execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
