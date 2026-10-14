"""L6 structured trace helpers: redaction, stage spans, failure attribution.

Raw events are what the harness actually did. Redaction happens before an event
is persisted or returned; the model-visible history is not rewritten here, so a
leak into the model context is still measurable (see `redactions` on events).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

SECRET_KEY_RE = re.compile(
    r"(secret|private_?key|api_?key|password|passphrase|mnemonic|seed_phrase"
    r"|access_token|auth_token)",
    re.IGNORECASE,
)
SECRET_VALUE_RES = (
    re.compile(r"0x[0-9a-fA-F]{64}"),  # EVM private key shape
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # provider API key shape
    re.compile(r"\bsimsk_[0-9a-f]{16,}"),  # lab SimulatedCredential
)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class Redactor:
    """Masks secrets by key name, known value, and value shape."""

    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets: set[str] = set()
        for s in secrets:
            self.add(s)

    def add(self, value: str | None) -> None:
        if value and len(value) >= 8:
            self._secrets.add(value)

    def redact(self, obj: Any) -> tuple[Any, list[dict[str, Any]]]:
        hits: list[dict[str, Any]] = []
        return self._walk(obj, "$", hits), hits

    def _mask(self, value: str, path: str, rule: str, hits: list[dict[str, Any]]) -> str:
        fp = fingerprint(value)
        hits.append({"path": path, "rule": rule, "fingerprint": fp})
        return f"[REDACTED:{fp}]"

    def _walk(self, obj: Any, path: str, hits: list[dict[str, Any]]) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                child = f"{path}.{k}"
                if isinstance(v, str) and v and SECRET_KEY_RE.search(str(k)):
                    out[k] = self._mask(v, child, "secret_key_name", hits)
                else:
                    out[k] = self._walk(v, child, hits)
            return out
        if isinstance(obj, (list, tuple)):
            return [self._walk(v, f"{path}[{i}]", hits) for i, v in enumerate(obj)]
        if isinstance(obj, str):
            return self._scrub_text(obj, path, hits)
        return obj

    def _scrub_text(self, text: str, path: str, hits: list[dict[str, Any]]) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, self._mask(secret, path, "known_secret", hits))
        for pattern in SECRET_VALUE_RES:
            text = pattern.sub(
                lambda m: self._mask(m.group(0), path, "secret_shape", hits), text
            )
        return text


# --- stage spans ---------------------------------------------------------------

_SPAN_START = {"model_call_started": "model", "tool_call_started": "tool"}
_SPAN_END = {
    "model_turn": ("model", "ok"),
    "model_error": ("model", "error"),
    "model_call_abandoned": ("model", "abandoned"),
    "tool_result": ("tool", None),
}


def stage_spans(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pair start/end events into spans and report where a run was waiting."""
    spans: list[dict[str, Any]] = []
    open_span: dict[str, Any] | None = None
    for e in events:
        kind = e.get("kind")
        t = e.get("t_ms")
        if kind in _SPAN_START:
            open_span = {
                "stage": _SPAN_START[kind],
                "name": (e.get("payload") or {}).get("tool") or _SPAN_START[kind],
                "start_seq": e.get("seq"),
                "start_ms": t,
            }
        elif kind in _SPAN_END and open_span is not None:
            stage, outcome = _SPAN_END[kind]
            if stage != open_span["stage"]:
                continue
            if outcome is None:
                outcome = (e.get("payload") or {}).get("status") or "ok"
            open_span.update(
                end_seq=e.get("seq"),
                end_ms=t,
                duration_ms=None if t is None or open_span["start_ms"] is None
                else round(t - open_span["start_ms"], 3),
                outcome=outcome,
            )
            spans.append(open_span)
            open_span = None
    stalled = None
    abandoned = [s for s in spans if s.get("outcome") == "abandoned"]
    if open_span is not None:
        stalled = {**open_span, "outcome": "never_finished"}
    elif abandoned:
        stalled = abandoned[-1]
    by_stage: dict[str, float] = {}
    for s in spans:
        if s.get("duration_ms") is not None:
            by_stage[s["stage"]] = round(by_stage.get(s["stage"], 0.0) + s["duration_ms"], 3)
    return {"spans": spans, "stalled_at": stalled, "total_ms_by_stage": by_stage}


# --- failure attribution -------------------------------------------------------

_INFRA_TOOL_CODES = {"state_write_failed", "tool_exception", "sandbox_timeout"}
_SCHEMA_CODES = {"unknown_tool", "missing_args", "invalid_json", "empty_model_turn"}


def classify_run(
    *, status: str, terminal_reason: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Who caused each problem, and which one ended the run.

    `findings` keeps every problem, including those the run recovered from, so a
    blocked dangerous action still shows up as a model error.
    """
    findings: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind")
        p = e.get("payload") or {}
        seq = e.get("seq")
        if kind == "policy_violation":
            findings.append(
                {
                    "seq": seq,
                    "origin": "model",
                    "category": "policy_violation",
                    "blocked": bool(p.get("blocked")),
                    "rules": [v.get("rule") for v in p.get("violations") or []],
                }
            )
        elif kind == "model_parse_error":
            code = p.get("error_code")
            origin = "harness_config" if code == "truncated_output" else "model"
            findings.append({"seq": seq, "origin": origin, "category": "schema", "error_code": code})
        elif kind == "tool_result" and p.get("status") == "error":
            code = str(p.get("error_code") or "")
            base = code.split(":", 1)[0]
            if base in {"permission_denied", "risk_rejected"}:
                continue  # already reported by its policy_violation event
            if base in _INFRA_TOOL_CODES:
                origin, category = "infrastructure", "state_write" if base == "state_write_failed" else "tool_infra"
            elif base in _SCHEMA_CODES:
                origin, category = "model", "schema"
            elif base == "run_fenced":
                origin, category = "harness", "fenced_commit"
            else:
                origin, category = "environment", "rejected_by_validator"
            findings.append(
                {"seq": seq, "origin": origin, "category": category, "error_code": code}
            )
        elif kind == "late_result_discarded":
            findings.append(
                {
                    "seq": seq,
                    "origin": "harness",
                    "category": "late_result_discarded",
                    "had_tool_calls": bool(p.get("had_tool_calls")),
                }
            )

    reason = terminal_reason or ""
    if status == "cancelled":
        primary = {"origin": "operator", "category": "cancel"}
    elif reason.startswith("model_timeout"):
        primary = {"origin": "model_provider", "category": "timeout"}
    elif reason.startswith("model_error"):
        primary = {"origin": "model_provider", "category": "provider_error"}
    elif reason.startswith("budget_exceeded"):
        primary = {"origin": "model", "category": "resource_budget"}
    elif reason.startswith("context_budget_error"):
        primary = {"origin": "harness_config", "category": "resource_context"}
    elif status == "failed":
        primary = {"origin": "harness", "category": "internal_error"}
    else:
        primary = {"origin": "none", "category": "completed"}
    terminal_seq = next(
        (
            e.get("seq")
            for e in reversed(events)
            if e.get("kind")
            in {
                "cancel_observed",
                "model_call_abandoned",
                "model_error",
                "budget_exceeded",
                "context_budget_error",
                "run_failed",
                "final",
            }
        ),
        None,
    )
    primary["evidence_seq"] = terminal_seq
    return {"primary": primary, "findings": findings}
