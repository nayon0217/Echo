"""Pipeline orchestrator: message in, composed reply out (policy.md §1).

Runs:
    stage 2 translate -> stage 3 route -> stage 4 claims
    -> stage 5–6 retrieve -> stages 7–9 verify -> stage 10 compose

    python -m pipeline.pipeline "MOM raised the work permit levy to $900 in 2026"
    echo "levy naik jadi 900 dollar" | python -m pipeline.pipeline
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.claims import Claim, extract_claims  # noqa: E402
from pipeline.compose import compose_from_result  # noqa: E402
from pipeline.retrieve import Source, generate_queries, retrieve_for_claim  # noqa: E402
from pipeline.router import Routing, route  # noqa: E402
from pipeline.scam import ScamResult, handle_scam  # noqa: E402
from pipeline.trace import new_request_id, trace_pipeline_result  # noqa: E402
from pipeline.translate import (  # noqa: E402
    Translation,
    translate_to_english,
    translation_from_english,
)
from pipeline.verify import verify_claim  # noqa: E402

MOM_HOTLINE = "MOM hotline 6438 5122"
MediaKind = Optional[Literal["voice", "image", "text"]]


def _sources_for_chunks(chunks) -> list[Source]:
    """Collapse cited chunks into one Source per document, preserving order."""
    sources: list[Source] = []
    seen: set[int] = set()
    for c in chunks:
        if c.document_id in seen:
            continue
        seen.add(c.document_id)
        text = c.content.strip()
        if c.heading and text.startswith(c.heading):
            text = text[len(c.heading) :].strip()
        snippet = " ".join(text.split())[:240]
        sources.append(
            Source(
                document_id=c.document_id,
                title=c.title,
                source_name=c.source_name,
                source_url=c.source_url,
                authority_tier=c.authority_tier,
                score=round(c.score, 4),
                snippet=snippet,
            )
        )
    return sources


def _translation_dict(t: Translation) -> dict[str, Any]:
    d = t.model_dump()
    d["source_language"] = t.source_language
    d["was_translated"] = t.was_translated
    return d


@dataclass
class ExtractedClaim:
    text: str
    type: str
    verdict: str = "insufficient"
    reasoning: str = ""
    queries: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    cited_sources: list[Source] = field(default_factory=list)
    gates_triggered: list[str] = field(default_factory=list)
    top_score: float = 0.0


@dataclass
class PipelineResult:
    input: str
    translation: Translation
    routing: Routing
    claims: list[ExtractedClaim] = field(default_factory=list)
    scam: ScamResult | None = None
    notice: str | None = None
    reply: str | None = None
    request_id: str | None = None
    stage_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["translation"] = _translation_dict(self.translation)
        return d


def process_message(
    text: str,
    *,
    text_en: str | None = None,
    source_language: str | None = None,
    media_kind: MediaKind = None,
    reply_language: str | None = None,
    with_queries: bool = True,
    with_retrieval: bool = True,
    with_verify: bool = True,
    with_compose: bool = True,
) -> PipelineResult:
    """Run stages 2–10 and return routing, claims, verdicts, and composed reply.

    If `text_en` is provided (voice/image path already translated), stage 2 is
    skipped and that English pivot is used directly.
    """
    if with_verify:
        with_retrieval = True
    if with_retrieval:
        with_queries = True
    text = (text or "").strip()
    stage_ms: dict[str, int] = {}
    request_id = new_request_id()

    def timed(name, fn, *args, **kwargs):
        t0 = time.perf_counter()
        out = fn(*args, **kwargs)
        stage_ms[name] = round((time.perf_counter() - t0) * 1000)
        return out

    # Stage 2 — translate to English (pivot), or reuse ASR/vision English.
    if text_en and text_en.strip():
        translation = translation_from_english(
            text_en.strip(), source_language=source_language or "en"
        )
        stage_ms["translate"] = 0
    else:
        translation = timed("translate", translate_to_english, text)

    # Stage 3 — route.
    routing: Routing = timed(
        "route", route, translation.text_en, language_detected=translation.source_language
    )

    result = PipelineResult(
        input=text or translation.text_en,
        translation=translation,
        routing=routing,
        stage_ms=stage_ms,
        request_id=request_id,
    )

    if translation.unintelligible or routing.unintelligible:
        result.notice = (
            "I couldn't understand that message clearly. Please re-record or resend it, "
            f"or call the {MOM_HOTLINE}."
        )
        if with_compose:
            result.reply = timed(
                "compose",
                compose_from_result,
                result,
                media_kind=media_kind,
                reply_language=reply_language,
            )
        trace_pipeline_result(result, request_id=request_id, media_kind=media_kind)
        return result

    # Scam path (stub) — runs in parallel to the policy path; both can fire.
    if routing.contains_scam_signals:
        result.scam = timed("scam", handle_scam, routing.scam_signals)

    # Stage 4 — claim extraction (only on the policy path).
    if routing.contains_policy_claim:
        raw_claims: list[Claim] = timed("claims", extract_claims, translation.text_en)

        def build_claims() -> list[ExtractedClaim]:
            out: list[ExtractedClaim] = []
            for c in raw_claims:
                claim = ExtractedClaim(text=c.text, type=c.type)
                if with_retrieval:
                    rr = retrieve_for_claim(c.text)
                    claim.queries = rr.queries
                    claim.sources = rr.sources
                    claim.top_score = rr.top_score
                    if with_verify:
                        v = verify_claim(c.text, rr.chunks, rr.top_score)
                        claim.verdict = v.verdict
                        claim.reasoning = v.reasoning
                        claim.gates_triggered = v.gates_triggered
                        claim.cited_sources = _sources_for_chunks(v.cited_chunks)
                elif with_queries:
                    claim.queries = generate_queries(c.text)
                out.append(claim)
            return out

        stage_name = "verify" if with_verify else ("retrieve" if with_retrieval else "queries")
        result.claims = (
            timed(stage_name, build_claims) if (with_retrieval or with_queries) else build_claims()
        )

    # Neither path fired -> hotline template.
    if not routing.contains_policy_claim and not routing.contains_scam_signals:
        result.notice = (
            "I can't verify this message — it doesn't contain a policy claim I can check. "
            f"If you're unsure, call the {MOM_HOTLINE}."
        )

    # Stage 10 — compose reply (policy.md §10), in the worker's language.
    if with_compose:
        result.reply = timed(
            "compose",
            compose_from_result,
            result,
            media_kind=media_kind,
            reply_language=reply_language,
        )

    trace_pipeline_result(result, request_id=request_id, media_kind=media_kind)
    return result


def main() -> int:
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("Provide a message as an argument or via stdin.", file=sys.stderr)
        return 1

    result = process_message(text)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
