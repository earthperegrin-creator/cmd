"""Local Codex adapter for side-effect-free registry resolution proposals."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


CodexBinaryFn = Callable[[], str]
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "resolution-plan.v1.json"
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


class CodexResolutionError(RuntimeError):
    """Raised when the local model adapter cannot return a structured plan."""


class CodexResolutionProposal:
    """Callable proposal adapter compatible with resolve_registry_request."""

    def __init__(
        self,
        *,
        model: str,
        codex_binary_fn: CodexBinaryFn,
        schema_path: Path = DEFAULT_SCHEMA_PATH,
        reasoning_effort: str = "low",
        timeout_seconds: int = 120,
    ):
        if not model.strip():
            raise ValueError("model must be non-empty")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.model = model.strip()
        self.codex_binary_fn = codex_binary_fn
        self.schema_path = schema_path
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.last_usage: dict[str, Any] = {}

    def reset_usage(self) -> None:
        self.last_usage = {}

    def __call__(self, prompt: str) -> Mapping[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodexResolutionError("resolution prompt must be non-empty")
        if not self.schema_path.exists():
            raise CodexResolutionError("ResolutionPlan schema is missing")
        with tempfile.TemporaryDirectory(prefix="cmd-registry-resolver-") as tmpdir:
            output_path = Path(tmpdir) / "resolution-plan.json"
            command = [
                self.codex_binary_fn(),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--disable", "plugins",
                "--disable", "apps",
                "--disable", "tool_suggest",
                "--skip-git-repo-check",
                "-C", tmpdir,
                "-m", self.model,
                "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
                "--sandbox", "read-only",
                "--output-schema", str(self.schema_path),
                "--output-last-message", str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env={**os.environ, "NO_COLOR": "1"},
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CodexResolutionError(f"resolution model could not run: {error}") from error
            if completed.returncode != 0 or not output_path.exists():
                detail = (completed.stderr or completed.stdout or "unknown resolver failure").strip()
                raise CodexResolutionError(f"resolution model failed: {detail[-1200:]}")
            try:
                proposal = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CodexResolutionError(f"resolution model returned invalid JSON: {error}") from error
        if not isinstance(proposal, dict):
            raise CodexResolutionError("resolution model must return one JSON object")
        token_match = re.search(
            r"tokens used\s*\n?\s*([\d,]+)",
            f"{completed.stdout}\n{completed.stderr}",
            flags=re.I,
        )
        self.last_usage = {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "tokens": int(token_match.group(1).replace(",", "")) if token_match else 0,
        }
        return proposal
