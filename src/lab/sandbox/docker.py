"""Docker SandboxExecutor. Untrusted code runs only inside the container."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from src.lab.sandbox.base import ProgramResult

DEFAULT_IMAGE = os.environ.get("TRADELIFE_SANDBOX_IMAGE", "python:3.12-alpine")
_RUNTIME_DIR = Path(__file__).resolve().parent
_MAX_CODE_CHARS = 20_000
_MAX_OUTPUT_CHARS = 256_000


def docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


class DockerSandbox:
    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        timeout_sec: float = 8.0,
        memory: str = "128m",
        cpus: str = "0.5",
    ):
        self.image = image
        self.timeout_sec = timeout_sec
        self.memory = memory
        self.cpus = cpus

    def available(self) -> bool:
        return docker_available()

    def run_program(
        self,
        code: str,
        snapshot: dict[str, Any],
        budget: dict[str, Any] | None = None,
    ) -> ProgramResult:
        if not self.available():
            return ProgramResult(
                error_code="sandbox_unavailable",
                retryable=False,
                infrastructure=True,
            )
        if len(code) > _MAX_CODE_CHARS:
            return ProgramResult(
                error_code="program_too_large",
                retryable=False,
                infrastructure=False,
                program_error={"type": "ValueError", "message": "code exceeds limit"},
            )
        job = {
            "code": code,
            "snapshot": snapshot,
            "budget": budget or {"max_simulations": 16},
        }
        name = f"tl-h3-{uuid.uuid4().hex[:12]}"
        work = Path(tempfile.mkdtemp(prefix="tl_h3_"))
        try:
            shutil.copy(_RUNTIME_DIR / "guest.py", work / "guest.py")
            shutil.copy(_RUNTIME_DIR / "snapshot_sim.py", work / "snapshot_sim.py")
            for path in work.iterdir():
                path.chmod(0o644)
            cmd = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,size=16m",
                "--memory",
                self.memory,
                "--memory-swap",
                self.memory,
                "--cpus",
                self.cpus,
                "--pids-limit",
                "64",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTHONUNBUFFERED=1",
                "-v",
                f"{work}:/work:ro",
                "-w",
                "/work",
                self.image,
                "python",
                "/work/guest.py",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    input=json.dumps(job, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                _force_rm(name)
                return ProgramResult(
                    error_code="sandbox_timeout",
                    retryable=False,
                    infrastructure=True,
                    payload={"container": name},
                )
            except OSError as exc:
                _force_rm(name)
                return ProgramResult(
                    error_code="sandbox_unavailable",
                    retryable=False,
                    infrastructure=True,
                    payload={"error": str(exc)},
                )
            raw_out = proc.stdout or ""
            if len(raw_out) > _MAX_OUTPUT_CHARS:
                return ProgramResult(
                    error_code="sandbox_output_too_large",
                    retryable=False,
                    infrastructure=True,
                )
            if proc.returncode != 0:
                return ProgramResult(
                    error_code="sandbox_infra_error",
                    retryable=False,
                    infrastructure=True,
                    payload={
                        "returncode": proc.returncode,
                        "stderr": (proc.stderr or "")[-2000:],
                    },
                )
            try:
                data = json.loads(raw_out.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                return ProgramResult(
                    error_code="sandbox_bad_output",
                    retryable=False,
                    infrastructure=True,
                    payload={"stderr": (proc.stderr or "")[-2000:]},
                )
            err = data.get("program_error")
            return ProgramResult(
                stdout=str(data.get("stdout") or ""),
                program_error=err,
                tool_trace=list(data.get("tool_trace") or []),
                proposed_action=data.get("proposed_action"),
                expanded_calls=int(data.get("expanded_calls") or 0),
                error_code="program_error" if err else None,
                retryable=bool(err),
                infrastructure=False,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
            _force_rm(name)


def _force_rm(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        check=False,
        timeout=8,
    )
