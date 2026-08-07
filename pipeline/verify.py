"""Stages 7-9 — two-pass verification + abstention gates (policy.md §7).

The optimisation target is *precision on confident verdicts*, so the design is
built to abstain (`insufficient`) rather than risk a confident-wrong answer.

  Pass A (verdict)          one claim + numbered chunks -> supported/refuted/insufficient
  Pass B (citation audit)   each cited chunk, in isolation -> entails/contradicts/neither
  Gates                     any failure downgrades the verdict to `insufficient`

Gate order (policy.md §7):
  1. ASR mean logprob below threshold        (not applicable to typed text yet)
  2. Top retrieval score below floor
  3. Pass B stripped all citations
  4. supported/refuted but all citations are authority tier 3
  5. a cited document is future-dated or superseded
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.llm import structured_call  # noqa: E402
from pipeline.retrieve import RetrievedChunk, retrieve_for_claim  # noqa: E402

# Floor on the top lexical retrieval score (ts_rank_cd). Deliberately low; the
# plan says tune this on the golden set rather than guess. Override via env.
RETRIEVAL_SCORE_FLOOR = float(os.getenv("RETRIEVAL_SCORE_FLOOR", "0.02"))

VERDICTS = ["supported", "refuted", "insufficient"]
RELATIONS = ["entails", "contradicts", "neither"]

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "cited_chunk_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "IDs of the provided chunks that directly support the verdict. Must be a subset of the given IDs.",
        },
        "reasoning": {"type": "string", "description": "Brief reasoning grounded only in the provided passages."},
    },
    "required": ["verdict", "cited_chunk_ids", "reasoning"],
}

_VERDICT_SYSTEM = (
    "You verify a single claim against numbered official Singapore government passages, for migrant workers. "
    "Decide: supported, refuted, or insufficient.\n"
    "- Use ONLY the provided passages. Do not use outside knowledge.\n"
    "- cited_chunk_ids must be a subset of the provided chunk IDs that directly justify your verdict.\n"
    "- CRITICAL: absence of a statement in these passages is NOT evidence the claim is false. "
    "If the passages do not clearly address the claim, return 'insufficient' with an empty cited_chunk_ids.\n"
    "- Only return 'supported' or 'refuted' when a passage directly and unambiguously does so."
)

_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {"type": "string", "enum": RELATIONS},
    },
    "required": ["relation"],
}

_AUDIT_SYSTEM = (
    "You judge whether a single passage, on its own, ENTAILS a claim (proves it true), "
    "CONTRADICTS it (proves it false), or NEITHER. "
    "Use only the passage; do not use outside knowledge. "
    "If the passage is merely topically related but does not settle the claim, answer 'neither'."
)


@dataclass
class CitationAudit:
    chunk_id: int
    relation: str


@dataclass
class VerdictResult:
    verdict: str
    reasoning: str
    cited_chunk_ids: list[int] = field(default_factory=list)
    cited_chunks: list[RetrievedChunk] = field(default_factory=list)
    audits: list[CitationAudit] = field(default_factory=list)
    gates_triggered: list[str] = field(default_factory=list)
    top_score: float = 0.0


def _format_chunks(chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for c in chunks:
        head = f" ({c.heading})" if c.heading else ""
        blocks.append(f"[{c.chunk_id}]{head}\n{c.content.strip()}")
    return "\n\n".join(blocks)


def _verdict_pass(claim: str, chunks: list[RetrievedChunk]) -> dict:
    user = f"Claim: {claim}\n\nPassages:\n\n{_format_chunks(chunks)}"
    return structured_call(
        system=_VERDICT_SYSTEM,
        user=user,
        schema=_VERDICT_SCHEMA,
        tool_name="record_verdict",
        tool_description="Record the verdict, cited chunk IDs, and reasoning for the claim.",
        max_tokens=800,
    )


def _audit_pass(claim: str, chunk: RetrievedChunk) -> str:
    user = f"Claim: {claim}\n\nPassage:\n{chunk.content.strip()}"
    result = structured_call(
        system=_AUDIT_SYSTEM,
        user=user,
        schema=_AUDIT_SCHEMA,
        tool_name="record_relation",
        tool_description="Record whether the passage entails, contradicts, or is neither for the claim.",
        max_tokens=64,
    )
    relation = result.get("relation")
    return relation if relation in RELATIONS else "neither"


def verify_claim(claim: str, chunks: list[RetrievedChunk], top_score: float) -> VerdictResult:
    """Run Pass A + Pass B + gates for one claim (stages 7-9)."""
    gates: list[str] = []

    # Gate 2 — nothing retrieved, or top score below the floor: don't verify.
    if not chunks or top_score < RETRIEVAL_SCORE_FLOOR:
        gates.append("2_retrieval_below_floor")
        return VerdictResult(
            verdict="insufficient",
            reasoning="No sufficiently relevant official source was found to verify this claim.",
            gates_triggered=gates,
            top_score=top_score,
        )

    # Pass A — verdict.
    raw = _verdict_pass(claim, chunks)
    verdict = raw.get("verdict") if raw.get("verdict") in VERDICTS else "insufficient"
    reasoning = (raw.get("reasoning") or "").strip()

    by_id = {c.chunk_id: c for c in chunks}
    # Constrain cited IDs to the ones actually supplied.
    cited_ids = [cid for cid in raw.get("cited_chunk_ids", []) if cid in by_id]

    # Pass B — audit each cited chunk in isolation; drop "neither".
    audits: list[CitationAudit] = []
    surviving: list[RetrievedChunk] = []
    for cid in cited_ids:
        chunk = by_id[cid]
        relation = _audit_pass(claim, chunk)
        audits.append(CitationAudit(chunk_id=cid, relation=relation))
        if relation in ("entails", "contradicts"):
            surviving.append(chunk)

    # Gate 5 — drop future-dated or superseded citations. (Retrieval already
    # excludes superseded docs, so this catches future effective_date.)
    today = date.today()
    kept: list[RetrievedChunk] = []
    for c in surviving:
        eff = c.effective_date
        if isinstance(eff, date) and eff > today:
            if "5_future_or_superseded_citation" not in gates:
                gates.append("5_future_or_superseded_citation")
            continue
        kept.append(c)
    surviving = kept

    # For a confident verdict, the surviving citations must hold up.
    if verdict in ("supported", "refuted"):
        # Gate 3 — Pass B (or gate 5) stripped every citation.
        if not surviving:
            gates.append("3_no_surviving_citations")
            verdict = "insufficient"
        # Gate 4 — all surviving citations are tier 3 (weak sourcing).
        elif all(c.authority_tier >= 3 for c in surviving):
            gates.append("4_all_citations_tier3")
            verdict = "insufficient"

    cited_chunks = surviving if verdict in ("supported", "refuted") else []
    return VerdictResult(
        verdict=verdict,
        reasoning=reasoning,
        cited_chunk_ids=[c.chunk_id for c in cited_chunks],
        cited_chunks=cited_chunks,
        audits=audits,
        gates_triggered=gates,
        top_score=top_score,
    )


def verify_for_claim(claim: str) -> VerdictResult:
    """Convenience: retrieve then verify a single claim end-to-end."""
    rr = retrieve_for_claim(claim)
    return verify_claim(claim, rr.chunks, rr.top_score)


def main() -> int:
    import json

    if len(sys.argv) < 2:
        print('usage: python -m pipeline.verify "<claim text>"', file=sys.stderr)
        return 1
    claim = " ".join(sys.argv[1:])
    v = verify_for_claim(claim)
    print(json.dumps({
        "claim": claim,
        "verdict": v.verdict,
        "reasoning": v.reasoning,
        "cited_chunk_ids": v.cited_chunk_ids,
        "cited_sources": [{"title": c.title, "url": c.source_url} for c in v.cited_chunks],
        "audits": [a.__dict__ for a in v.audits],
        "gates_triggered": v.gates_triggered,
        "top_score": v.top_score,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
