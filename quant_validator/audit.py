"""quant_validator.audit: append-only audit log helpers.

Two JSONL files per thesis:
  theses/<id>/audit_log.jsonl       — every pipeline event with timestamp
  theses/<id>/user_interactions.jsonl — every user input the orchestrator received

JSONL = JSON Lines. One JSON object per line. Append-only by design.
Easy to read line-by-line; easy to grep; survives concurrent writes if you're
disciplined about one writer at a time (the orchestrator).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


# ═══════════════════════════════════════════════════════════════
# Audit log — pipeline events

def log_step_start(thesis_id: str, step: int, name: str, mode: str | None = None) -> None:
    _append_jsonl(
        Path(f"theses/{thesis_id}/audit_log.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "step_start",
            "step": step,
            "step_name": name,
            "mode": mode,
        },
    )


def log_step_complete(
    thesis_id: str,
    step: int,
    name: str,
    *,
    status: str,
    metrics: dict[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """status: 'pass' | 'fail' | 'warning' | 'skipped'"""
    _append_jsonl(
        Path(f"theses/{thesis_id}/audit_log.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "step_complete",
            "step": step,
            "step_name": name,
            "status": status,
            "metrics": metrics or {},
            "note": note,
        },
    )


def log_override_applied(
    thesis_id: str,
    failure_key: str,
    reason: str,
    computed_value: Any = None,
    threshold: Any = None,
) -> None:
    _append_jsonl(
        Path(f"theses/{thesis_id}/audit_log.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "override_applied",
            "failure_key": failure_key,
            "reason": reason,
            "computed_value": computed_value,
            "threshold": threshold,
        },
    )


def log_pipeline_complete(
    thesis_id: str, decision: str, stopped_at_step: int | None
) -> None:
    _append_jsonl(
        Path(f"theses/{thesis_id}/audit_log.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "pipeline_complete",
            "decision": decision,
            "stopped_at_step": stopped_at_step,
        },
    )


# ═══════════════════════════════════════════════════════════════
# User interactions

def log_user_question(
    thesis_id: str,
    step: int,
    question: str,
    options: list[str] | None = None,
) -> None:
    _append_jsonl(
        Path(f"theses/{thesis_id}/user_interactions.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "question_asked",
            "step": step,
            "question": question,
            "options": options or [],
        },
    )


def log_user_response(thesis_id: str, step: int, response: str) -> None:
    _append_jsonl(
        Path(f"theses/{thesis_id}/user_interactions.jsonl"),
        {
            "timestamp": _now_iso(),
            "event_type": "response_received",
            "step": step,
            "response": response,
        },
    )


# ═══════════════════════════════════════════════════════════════
# Read helpers (for re-summarizing past runs)

def read_audit_log(thesis_id: str) -> list[dict[str, Any]]:
    path = Path(f"theses/{thesis_id}/audit_log.jsonl")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_user_interactions(thesis_id: str) -> list[dict[str, Any]]:
    path = Path(f"theses/{thesis_id}/user_interactions.jsonl")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
