"""Production request tracing (policy.md §2, §11).

One JSONL line per request with verdicts, retrieval scores, and stage timings.
Never logs transcripts, audio, or message bodies.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "pipeline.jsonl"


def _trace_path() -> Path:
    return Path(os.getenv("PIPELINE_TRACE_PATH") or _DEFAULT_PATH)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def append_trace(event: dict[str, Any]) -> None:
    """Append one JSON object as a line. Failures are swallowed — tracing must not break replies."""
    try:
        path = _trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        # Defence in depth: never persist keys that could hold message content.
        for banned in ("input", "text", "text_en", "transcript", "audio", "message"):
            payload.pop(banned, None)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — tracing is best-effort
        print(f"[trace] write failed: {exc}")


def trace_pipeline_result(
    result: Any,
    *,
    request_id: str | None = None,
    media_kind: str | None = None,
) -> str:
    """Write a privacy-safe summary of a PipelineResult. Returns the request_id used."""
    rid = request_id or new_request_id()
    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    routing = d.get("routing") or {}
    claims_out = []
    for c in d.get("claims") or []:
        claims_out.append(
            {
                "type": c.get("type"),
                "verdict": c.get("verdict"),
                "top_score": c.get("top_score"),
                "gates_triggered": c.get("gates_triggered") or [],
                "n_cited": len(c.get("cited_sources") or []),
                "n_retrieved": len(c.get("sources") or []),
            }
        )
    append_trace(
        {
            "request_id": rid,
            "media_kind": media_kind,
            "language_detected": routing.get("language_detected"),
            "contains_policy_claim": routing.get("contains_policy_claim"),
            "contains_scam_signals": routing.get("contains_scam_signals"),
            "scam_signals": (d.get("scam") or {}).get("signals") if d.get("scam") else [],
            "n_claims": len(claims_out),
            "claims": claims_out,
            "has_notice": bool(d.get("notice")),
            "stage_ms": d.get("stage_ms") or {},
        }
    )
    return rid
