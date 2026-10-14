"""Cooperative cancellation for one run.

Setting the token does not stop a model or tool call already in flight; the
harness stops waiting for it, fences commits for the run, and discards any late
result. That gap is what L6 makes observable.
"""

from __future__ import annotations

import threading
from typing import Optional


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: Optional[str] = None

    def cancel(self, reason: str = "operator_cancel") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()
