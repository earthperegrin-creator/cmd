"""Per-job Safehouse runtime packet and worker lifecycle adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Sequence


class SandboxError(ValueError):
    """Raised when a job runtime cannot be safely prepared."""


@dataclass(frozen=True)
class RuntimePacket:
    job_dir: Path
    runtime_dir: Path
    lease_file: Path
    tools_file: Path
    mcp_config: Path
    prompt_file: Path
    network_profile: Path


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_code: int | None
    reason: str
    stdout: str
    stderr: str
    heartbeats: int


def prepare_runtime(
    job_dir: Path,
    *,
    lease_token: str,
    tools: Sequence[str],
    proxy_command: Path,
) -> RuntimePacket:
    job_dir = job_dir.resolve()
    required = [job_dir / "job.json", job_dir / "context", job_dir / "scratch", job_dir / "artifacts", job_dir / "events.jsonl"]
    if not all(path.exists() for path in required):
        raise SandboxError("job directory is not fully materialized")
    if not lease_token or any(not isinstance(tool, str) or not tool for tool in tools):
        raise SandboxError("lease token and tool names are required")
    proxy_command = proxy_command.resolve()
    if not proxy_command.is_file():
        raise SandboxError("job proxy executable does not exist")
    runtime = job_dir / "runtime"
    runtime.mkdir(exist_ok=False)
    lease_file = runtime / "lease.token"
    tools_file = runtime / "tools.json"
    mcp_config = runtime / "mcp.json"
    prompt_file = runtime / "prompt.txt"
    network_profile = runtime / "deny-network.sb"
    pinned_proxy = runtime / "cmd-job-proxy"
    shutil.copy2(proxy_command, pinned_proxy)
    lease_file.write_text(lease_token, encoding="utf-8")
    tools_file.write_text(json.dumps({"tools": sorted(set(tools))}, indent=2) + "\n", encoding="utf-8")
    mcp_config.write_text(json.dumps({
        "mcpServers": {
            "cmd_job": {
                "command": str(pinned_proxy),
                "args": ["--lease-file", str(lease_file), "--tools-file", str(tools_file)],
            }
        }
    }, indent=2) + "\n", encoding="utf-8")
    prompt_file.write_text(
        "Execute only the contract in job.json. Read only context/ and runtime/. "
        "Write intermediate work to scratch/ and final outputs to artifacts/. "
        "Use only the cmd_job tools listed in runtime/tools.json.\n",
        encoding="utf-8",
    )
    network_profile.write_text("(deny network-outbound (remote ip))\n", encoding="utf-8")
    os.chmod(lease_file, 0o400)
    os.chmod(pinned_proxy, 0o500)
    return RuntimePacket(job_dir, runtime, lease_file, tools_file, mcp_config, prompt_file, network_profile)


def safehouse_prefix(packet: RuntimePacket) -> list[str]:
    read_only = [packet.job_dir / "job.json", packet.job_dir / "context", packet.runtime_dir]
    writable = [packet.job_dir / "scratch", packet.job_dir / "artifacts", packet.job_dir / "events.jsonl"]
    return [
        "safehouse",
        "--workdir=",
        f"--add-dirs-ro={':'.join(str(path) for path in read_only)}",
        f"--add-dirs={':'.join(str(path) for path in writable)}",
        f"--append-profile={packet.network_profile}",
        "--",
    ]


def codex_command(packet: RuntimePacket, *, model: str = "") -> list[str]:
    proxy = json.loads(packet.mcp_config.read_text(encoding="utf-8"))["mcpServers"]["cmd_job"]
    command = [
        "codex", "exec", "--cd", str(packet.job_dir), "--ignore-user-config",
        "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "danger-full-access",
        "-c", f'mcp_servers.cmd_job.command={json.dumps(proxy["command"])}',
        "-c", f'mcp_servers.cmd_job.args={json.dumps(proxy["args"])}',
    ]
    if model:
        command.extend(["--model", model])
    command.append(packet.prompt_file.read_text(encoding="utf-8"))
    return [*safehouse_prefix(packet), *command]


def claude_command(packet: RuntimePacket, *, model: str = "") -> list[str]:
    command = [
        "claude", "-p", packet.prompt_file.read_text(encoding="utf-8"),
        "--bare", "--safe-mode", "--disable-slash-commands", "--no-session-persistence",
        "--tools", "",
        "--permission-mode", "bypassPermissions", "--strict-mcp-config",
        "--mcp-config", str(packet.mcp_config), "--output-format", "text",
    ]
    if model:
        command.extend(["--model", model])
    return [*safehouse_prefix(packet), *command]


class SafehouseExecutor:
    def __init__(self, *, poll_interval: float = 0.05, kill_grace: float = 0.5):
        self.poll_interval = poll_interval
        self.kill_grace = kill_grace

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: Path | None = None,
        cancel: Event | None = None,
        heartbeat: Callable[[], None] | None = None,
        heartbeat_interval: float = 30.0,
    ) -> ExecutionResult:
        cancel = cancel or Event()
        try:
            process = subprocess.Popen(
                list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
                cwd=str(cwd) if cwd is not None else None,
            )
        except OSError as error:
            return ExecutionResult("startup_failed", None, str(error), "", "", 0)
        started = time.monotonic()
        next_heartbeat = started
        heartbeats = 0
        terminal_status = "completed"
        reason = "Worker exited successfully."
        while process.poll() is None:
            now = time.monotonic()
            if cancel.is_set():
                terminal_status, reason = "cancelled", "Worker was cancelled."
                self._stop(process)
                break
            if now - started >= timeout_seconds:
                terminal_status, reason = "timed_out", "Worker exceeded its execution timeout."
                self._stop(process)
                break
            if heartbeat and now >= next_heartbeat:
                heartbeat()
                heartbeats += 1
                next_heartbeat = now + heartbeat_interval
            time.sleep(self.poll_interval)
        stdout, stderr = process.communicate()
        exit_code = process.returncode
        if terminal_status == "completed" and exit_code != 0:
            terminal_status, reason = "failed", f"Worker exited with code {exit_code}."
        return ExecutionResult(terminal_status, exit_code, reason, stdout, stderr, heartbeats)

    def _stop(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, 15)
            process.wait(timeout=self.kill_grace)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.wait()
