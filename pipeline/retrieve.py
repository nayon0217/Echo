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
FINAL_LIMIT = 10        # room for all four source families when they score
TRGM_THRESHOLD = 0.1    # min trigram similarity for the fuzzy fallback

# Caps when diversifying so NGO/news sources can appear alongside MOM/CPF/IRAS.
_TIER_CAPS = {1: 6, 2: 3, 3: 2}

# Four corpus families from policy.md §3 (SPF ScamAlert still deferred in ingest).
# Diversifying by family keeps CPF/IRAS from being crowded out by MOM, and keeps
# TWC2/MWC + CNA/ST in the verifier context when they match.
_SOURCE_FAMILIES = (
    ("mom", frozenset({"mom"})),
    ("gov_other", frozenset({"cpf", "iras", "spf", "scamalert"})),
    ("ngo", frozenset({"twc2", "mwc", "migrant workers' centre", "migrant workers centre"})),
    ("news", frozenset({"cna", "st", "straits times", "the straits times"})),
)
_FAMILY_CAPS = {"mom": 4, "gov_other": 2, "ngo": 2, "news": 2}

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
    "Given a claim about Singapore migrant-worker rules, produce 4 Postgres full-text search "
    "queries that would find relevant corpus documents. "
    "The corpus has four source families — write queries that can hit each where relevant:\n"
    "1. MOM: Work Permit, levy, salary, medical insurance, housing, security bond\n"
    "2. CPF / IRAS: employer CPF, Skills Development Levy, tax clearance for work-pass holders\n"
    "3. TWC2 / MWC: IPA, kickbacks, salary claims, worker advice MOM pages omit\n"
    "4. CNA / Straits Times: Budget, announced policy changes (dating only)\n"
    'Use official terminology (e.g. "Work Permit", "levy", "S Pass", "security bond", "IPA") '
    "and also common worker terms when useful (e.g. \"kickback\", \"deduction for others\"). "
    "Vary specificity: one broad, two targeted, one covering a figure/date if present. "
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
    """Execute queries, dedupe by chunk, and diversify across source families.

    Reserves slots for MOM, CPF/IRAS, TWC2/MWC, and CNA/ST so one family does not
    crowd out the others. Final order for the verifier is still authority_tier then
    score (tier-1 first).
    """
    # Score scales differ: ts_rank_cd for a strong FTS hit is often ~0.01, while
    # trigram similarity for the same chunk is ~0.2. Whichever method "wins" the
    # per-chunk merge, gate 2 must see the best score among chunks we actually
    # return — otherwise fuzzy overwrites make top_score collapse to a leftover
    # weak FTS hit and we abstain on claims we already retrieved correctly.
    best: dict[int, RetrievedChunk] = {}
    for q in queries:
        for hit in _search(q):
            existing = best.get(hit.chunk_id)
            if existing is None or hit.score > existing.score:
                best[hit.chunk_id] = hit

    ranked = _select_diverse(list(best.values()), limit=limit)
    top_score = max((c.score for c in ranked), default=0.0)

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


def _source_family(source_name: str) -> str:
    key = (source_name or "").strip().lower()
    for family, names in _SOURCE_FAMILIES:
        if key in names:
            return family
    return "mom"  # unknown sources sit with the primary gov bucket


def _select_diverse(chunks: list[RetrievedChunk], *, limit: int) -> list[RetrievedChunk]:
    """Prefer strong hits from every source family and authority tier that scored."""
    if not chunks:
        return []
    by_score = sorted(chunks, key=lambda c: -c.score)

    by_family: dict[str, list[RetrievedChunk]] = {f: [] for f, _ in _SOURCE_FAMILIES}
    by_family["other"] = []
    for c in by_score:
        fam = _source_family(c.source_name)
        by_family.setdefault(fam, []).append(c)

    picked: list[RetrievedChunk] = []
    seen: set[int] = set()

    # Pass 1 — reserve slots across the four corpus families.
    for family, _ in _SOURCE_FAMILIES:
        cap = _FAMILY_CAPS.get(family, 2)
        for c in by_family.get(family, [])[:cap]:
            if c.chunk_id in seen:
                continue
            picked.append(c)
            seen.add(c.chunk_id)
            if len(picked) >= limit:
                break
        if len(picked) >= limit:
            break

    # Pass 2 — top up by authority tier so tier-1 density stays high.
    if len(picked) < limit:
        buckets: dict[int, list[RetrievedChunk]] = {1: [], 2: [], 3: []}
        for c in by_score:
            if c.chunk_id in seen:
                continue
            tier = c.authority_tier if c.authority_tier in buckets else 3
            buckets[tier].append(c)
        for tier, cap in _TIER_CAPS.items():
            for c in buckets[tier][:cap]:
                if c.chunk_id in seen:
                    continue
                picked.append(c)
                seen.add(c.chunk_id)
                if len(picked) >= limit:
                    break
            if len(picked) >= limit:
                break

    for c in by_score:
        if len(picked) >= limit:
            break
        if c.chunk_id not in seen:
            picked.append(c)
            seen.add(c.chunk_id)

    return sorted(picked[:limit], key=lambda c: (c.authority_tier, -c.score))


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
