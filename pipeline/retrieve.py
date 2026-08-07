"""Stages 5-6 — retrieval (policy.md §6).

Stage 5 generates official-terminology FTS queries for a claim; stage 6 executes
them against Postgres (full-text + trigram), unions the hits, dedupes by chunk,
and reranks tier-1 sources first.

The single biggest retrieval failure is vocabulary mismatch: a worker says "the
government paper fee went up" while the corpus says "levy rates for Work Permit
holders". So we translate each claim into official terminology *before* searching,
and keep a trigram fallback for ASR-mangled entity names.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import fetch_all  # noqa: E402
from pipeline.llm import structured_call  # noqa: E402

LEXICAL_LIMIT = 8       # top FTS hits per query
FUZZY_LIMIT = 4         # top trigram hits per query
FINAL_LIMIT = 8         # chunks kept overall, after dedupe + rerank
TRGM_THRESHOLD = 0.1    # min trigram similarity for the fuzzy fallback

# policy.md §6 — lexical retrieval over the generated tsvector.
_LEXICAL_SQL = """
select c.id as chunk_id, c.content, c.heading,
       d.id as document_id, d.source_name, d.source_url, d.title,
       d.effective_date, d.authority_tier,
       ts_rank_cd(c.tsv, query) as score
from chunks c
join documents d on d.id = c.document_id,
     websearch_to_tsquery('english', %(q)s) query
where c.tsv @@ query
  and d.superseded_by is null
order by score desc
limit %(limit)s
"""

# Fuzzy fallback: catches ASR-mangled entity names the lexical index misses.
# similarity() is used directly (a seq scan is fine at this corpus size), which
# avoids escaping the `%%` trigram operator in the driver.
_FUZZY_SQL = """
select c.id as chunk_id, c.content, c.heading,
       d.id as document_id, d.source_name, d.source_url, d.title,
       d.effective_date, d.authority_tier,
       similarity(c.content, %(q)s) as score
from chunks c
join documents d on d.id = c.document_id
where d.superseded_by is null
  and similarity(c.content, %(q)s) > %(threshold)s
order by score desc
limit %(limit)s
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
            "description": "3-5 Postgres full-text search queries using official Singapore government terminology.",
        }
    },
    "required": ["queries"],
}

_SYSTEM = (
    "Given a claim, produce 4 Postgres full-text search queries that would find official Singapore "
    "government documents relevant to verifying it. "
    'Use official terminology (e.g. "Work Permit", "levy", "S Pass", "security bond"), not colloquial phrasing. '
    "Vary specificity: one broad, two targeted, and one covering the specific figure or date if present. "
    "Return only short keyword queries, not questions or full sentences."
)


def generate_queries(claim_text: str) -> list[str]:
    """Return 3-5 official-terminology FTS queries for a claim (stage 5)."""
    result = structured_call(
        system=_SYSTEM,
        user=claim_text,
        schema=_SCHEMA,
        tool_name="record_queries",
        tool_description="Record the full-text search queries generated for this claim.",
        max_tokens=512,
    )
    queries = [q.strip() for q in result.get("queries", []) if isinstance(q, str) and q.strip()]
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    title: str
    source_name: str
    source_url: str
    authority_tier: int
    heading: str | None
    content: str
    score: float
    method: str  # "lexical" or "fuzzy"
    effective_date: object | None = None  # datetime.date or None (for the temporal gate)


@dataclass
class Source:
    """A document surfaced for a claim, with its best-matching snippet."""

    document_id: int
    title: str
    source_name: str
    source_url: str
    authority_tier: int
    score: float
    snippet: str


@dataclass
class RetrievalResult:
    queries: list[str] = field(default_factory=list)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    top_score: float = 0.0


def _snippet(content: str, heading: str | None, limit: int = 240) -> str:
    """A short, human-readable excerpt of a chunk (drops a duplicated heading)."""
    text = content.strip()
    if heading and text.startswith(heading):
        text = text[len(heading):].strip()
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _search(query: str) -> list[RetrievedChunk]:
    """Run the lexical + fuzzy queries for one search string and tag the method."""
    hits: list[RetrievedChunk] = []
    for sql, method, params in (
        (_LEXICAL_SQL, "lexical", {"q": query, "limit": LEXICAL_LIMIT}),
        (_FUZZY_SQL, "fuzzy", {"q": query, "limit": FUZZY_LIMIT, "threshold": TRGM_THRESHOLD}),
    ):
        for row in fetch_all(sql, params):
            hits.append(
                RetrievedChunk(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    title=row["title"],
                    source_name=row["source_name"],
                    source_url=row["source_url"],
                    authority_tier=row["authority_tier"],
                    heading=row["heading"],
                    content=row["content"],
                    score=float(row["score"]),
                    method=method,
                    effective_date=row.get("effective_date"),
                )
            )
    return hits


def retrieve(queries: list[str], *, limit: int = FINAL_LIMIT) -> RetrievalResult:
    """Execute queries, dedupe by chunk, and rerank tier-1 first (stage 6).

    Dedupe keeps the highest-scoring appearance of each chunk. Final ordering is
    by authority tier (tier-1 first) then score, so the most authoritative
    sources reach the verifier first.
    """
    best: dict[int, RetrievedChunk] = {}
    for q in queries:
        for hit in _search(q):
            existing = best.get(hit.chunk_id)
            if existing is None or hit.score > existing.score:
                best[hit.chunk_id] = hit

    ranked = sorted(best.values(), key=lambda c: (c.authority_tier, -c.score))[:limit]
    # policy.md §7 gate 2 uses the top lexical score; fall back to any top score.
    lexical_scores = [c.score for c in ranked if c.method == "lexical"]
    top_score = max(lexical_scores) if lexical_scores else (max((c.score for c in ranked), default=0.0))

    # One Source per document (its best chunk), preserving rank order.
    sources: list[Source] = []
    seen_docs: set[int] = set()
    for c in ranked:
        if c.document_id in seen_docs:
            continue
        seen_docs.add(c.document_id)
        sources.append(
            Source(
                document_id=c.document_id,
                title=c.title,
                source_name=c.source_name,
                source_url=c.source_url,
                authority_tier=c.authority_tier,
                score=round(c.score, 4),
                snippet=_snippet(c.content, c.heading),
            )
        )

    return RetrievalResult(queries=queries, chunks=ranked, sources=sources, top_score=round(top_score, 4))


def retrieve_for_claim(claim_text: str, *, limit: int = FINAL_LIMIT) -> RetrievalResult:
    """Stage 5 + 6 for a single claim: generate queries, then retrieve."""
    queries = generate_queries(claim_text)
    if not queries:
        return RetrievalResult()
    return retrieve(queries, limit=limit)


def main() -> int:
    import json

    if len(sys.argv) < 2:
        print('usage: python -m pipeline.retrieve "<claim text>"', file=sys.stderr)
        return 1
    claim = " ".join(sys.argv[1:])
    result = retrieve_for_claim(claim)
    print(json.dumps({
        "queries": result.queries,
        "top_score": result.top_score,
        "sources": [s.__dict__ for s in result.sources],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
