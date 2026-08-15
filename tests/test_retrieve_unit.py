"""Unit tests for multi-family retrieval diversification."""

from pipeline.retrieve import RetrievedChunk, _select_diverse, _source_family, retrieve


def _chunk(
    chunk_id: int,
    source_name: str,
    tier: int,
    score: float,
    *,
    method: str = "lexical",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=chunk_id,
        title=f"{source_name} doc",
        source_name=source_name,
        source_url=f"https://example.com/{chunk_id}",
        authority_tier=tier,
        heading=None,
        content="sample",
        score=score,
        method=method,
    )


def test_source_family_mapping():
    assert _source_family("MOM") == "mom"
    assert _source_family("CPF") == "gov_other"
    assert _source_family("IRAS") == "gov_other"
    assert _source_family("TWC2") == "ngo"
    assert _source_family("MWC") == "ngo"
    assert _source_family("CNA") == "news"
    assert _source_family("ST") == "news"


def test_select_diverse_keeps_all_four_families():
    # Flood of high-scoring MOM chunks would otherwise crowd everything else out.
    chunks = [_chunk(i, "MOM", 1, 10 - i * 0.01) for i in range(1, 9)]
    chunks += [
        _chunk(20, "CPF", 1, 1.0),
        _chunk(21, "TWC2", 2, 0.9),
        _chunk(22, "CNA", 3, 0.8),
    ]
    picked = _select_diverse(chunks, limit=10)
    families = {_source_family(c.source_name) for c in picked}
    assert families >= {"mom", "gov_other", "ngo", "news"}


def test_top_score_uses_best_ranked_even_when_fuzzy_overwrites(monkeypatch):
    """Fuzzy similarity often beats ts_rank numerically; gate 2 must see that score."""

    def fake_search(_query: str):
        return [
            _chunk(1, "MOM", 1, 0.05, method="lexical"),
            _chunk(1, "MOM", 1, 0.27, method="fuzzy"),  # same chunk, higher fuzzy
            _chunk(2, "MOM", 1, 0.002, method="lexical"),
        ]

    monkeypatch.setattr("pipeline.retrieve._search", fake_search)
    result = retrieve(["salary payment"])
    assert result.top_score == 0.27
    assert result.chunks[0].score == 0.27
