"""Fetch curated sources, chunk them, and upsert into Postgres (policy.md §13).

    python -m ingest.fetch                 # ingest every source in sources.yaml
    python -m ingest.fetch --tier 1        # only tier-1 sources
    python -m ingest.fetch --dry-run       # fetch + chunk, print stats, no DB writes
    python -m ingest.fetch --limit 3       # first N sources (quick smoke test)

Upserts on `documents.source_url`: re-running replaces a document's chunks in
place rather than duplicating. Fetches are polite (real UA, small delay).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import trafilatura
import yaml
from trafilatura.settings import use_config

# Allow running both as `python -m ingest.fetch` and `python ingest/fetch.py`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import connect  # noqa: E402
from ingest.chunker import chunk_markdown  # noqa: E402

SOURCES_PATH = Path(__file__).parent / "sources.yaml"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 (+ECHO MIL fact-check bot)"
)
REQUEST_DELAY_S = 0.7

_TRAFILATURA_CFG = use_config()
_TRAFILATURA_CFG.set("DEFAULT", "USER_AGENTS", USER_AGENT)
_TRAFILATURA_CFG.set("DEFAULT", "DOWNLOAD_TIMEOUT", "30")


def load_sources(
    tier: int | None,
    limit: int | None,
    *,
    only_new: bool = False,
    source_name: str | None = None,
) -> list[dict[str, Any]]:
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    sources = data.get("sources", [])
    if tier is not None:
        sources = [s for s in sources if int(s.get("authority_tier", 0)) == tier]
    if source_name:
        want = source_name.strip().lower()
        sources = [s for s in sources if str(s.get("source_name", "")).lower() == want]
    if only_new:
        existing = _existing_urls()
        sources = [s for s in sources if s.get("source_url") not in existing]
    if limit is not None:
        sources = sources[:limit]
    return sources


def _existing_urls() -> set[str]:
    """URLs already present in documents — used by --only-new."""
    try:
        from db.connection import fetch_all

        return {r["source_url"] for r in fetch_all("select source_url from documents")}
    except Exception as exc:
        print(f"warning: could not read existing URLs ({exc}); ingesting all matches")
        return set()


def fetch_document(url: str) -> tuple[str, dict[str, Any]] | None:
    """Return (markdown, metadata) for a URL, or None if it can't be fetched."""
    html = trafilatura.fetch_url(url, config=_TRAFILATURA_CFG)
    if not html:
        return None
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        favor_recall=True,
        config=_TRAFILATURA_CFG,
    )
    if not markdown or not markdown.strip():
        return None
    meta = trafilatura.extract_metadata(html, default_url=url)
    meta_dict = meta.as_dict() if meta else {}
    return markdown, meta_dict


def upsert_document(cur, src: dict[str, Any], title: str, meta: dict[str, Any]) -> int:
    published = meta.get("date") or src.get("published_date")
    cur.execute(
        """
        insert into documents (source_name, source_url, authority_tier, title, published_date, effective_date)
        values (%(source_name)s, %(source_url)s, %(authority_tier)s, %(title)s, %(published_date)s, %(effective_date)s)
        on conflict (source_url) do update set
            source_name    = excluded.source_name,
            authority_tier = excluded.authority_tier,
            title          = excluded.title,
            published_date = coalesce(excluded.published_date, documents.published_date),
            effective_date = coalesce(excluded.effective_date, documents.effective_date),
            retrieved_at   = now()
        returning id
        """,
        {
            "source_name": src["source_name"],
            "source_url": src["source_url"],
            "authority_tier": int(src["authority_tier"]),
            "title": title,
            "published_date": published or None,
            "effective_date": src.get("effective_date") or None,
        },
    )
    return cur.fetchone()["id"]


def replace_chunks(cur, document_id: int, chunks) -> None:
    cur.execute("delete from chunks where document_id = %s", (document_id,))
    for ch in chunks:
        cur.execute(
            """
            insert into chunks (document_id, ordinal, heading, content)
            values (%s, %s, %s, %s)
            """,
            (document_id, ch.ordinal, ch.heading, ch.content),
        )


def _slug_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").strip().title() or url


def run(
    tier: int | None,
    limit: int | None,
    dry_run: bool,
    *,
    only_new: bool = False,
    source_name: str | None = None,
) -> int:
    sources = load_sources(tier, limit, only_new=only_new, source_name=source_name)
    if not sources:
        print("No sources matched. Check sources.yaml / --tier / --only-new.", file=sys.stderr)
        return 1

    label = f"tier {tier}" if tier is not None else "all tiers"
    if source_name:
        label += f", source={source_name}"
    if only_new:
        label += ", new only"
    print(f"Ingesting {len(sources)} sources ({label}){' [dry-run]' if dry_run else ''}\n")

    ok = failed = total_chunks = 0
    conn_ctx = None if dry_run else connect()
    conn = conn_ctx.__enter__() if conn_ctx else None
    cur = conn.cursor() if conn else None

    try:
        for i, src in enumerate(sources, 1):
            url = src["source_url"]
            try:
                result = fetch_document(url)
            except Exception as exc:
                print(f"[{i:>2}/{len(sources)}] FETCH ERROR  {url}\n      {exc}")
                failed += 1
                continue

            if not result:
                print(f"[{i:>2}/{len(sources)}] EMPTY/404    {url}")
                failed += 1
                continue

            markdown, meta = result
            title = src.get("title") or meta.get("title") or _slug_title(url)
            chunks = chunk_markdown(markdown)
            if not chunks:
                print(f"[{i:>2}/{len(sources)}] NO CHUNKS    {url}")
                failed += 1
                continue

            total_chunks += len(chunks)
            ok += 1

            if dry_run:
                print(f"[{i:>2}/{len(sources)}] {len(chunks):>2} chunks   {title[:60]}")
            else:
                doc_id = upsert_document(cur, src, title, meta)
                replace_chunks(cur, doc_id, chunks)
                conn.commit()
                print(f"[{i:>2}/{len(sources)}] doc {doc_id:>3} · {len(chunks):>2} chunks  {title[:55]}")

            time.sleep(REQUEST_DELAY_S)
    finally:
        if cur:
            cur.close()
        if conn_ctx:
            conn_ctx.__exit__(None, None, None)

    print(f"\nDone. {ok} ingested, {failed} failed, {total_chunks} chunks total.")
    return 0 if ok and not failed else (0 if ok else 1)


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest ECHO fact-check corpus into Postgres.")
    p.add_argument("--tier", type=int, choices=[1, 2, 3], help="only sources of this authority tier")
    p.add_argument("--limit", type=int, help="only the first N matching sources")
    p.add_argument("--dry-run", action="store_true", help="fetch + chunk but do not write to the DB")
    p.add_argument(
        "--only-new",
        action="store_true",
        help="skip URLs already present in the documents table",
    )
    p.add_argument("--source-name", type=str, help="filter by source_name (e.g. TWC2, CNA)")
    args = p.parse_args()
    return run(
        args.tier,
        args.limit,
        args.dry_run,
        only_new=args.only_new,
        source_name=args.source_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
