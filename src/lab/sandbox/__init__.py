"""Isolated program execution. Untrusted code never runs in the host agent."""

from src.lab.sandbox.disabled import DisabledSandbox, is_enabled
from src.lab.sandbox.docker import DockerSandbox, docker_available
from src.lab.sandbox.snapshot_sim import simulate_action

__all__ = [
    "DisabledSandbox",
    "DockerSandbox",
    "docker_available",
    "is_enabled",
    "simulate_action",
]
