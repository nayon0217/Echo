-- ECHO — policy verification corpus schema (policy.md §3)
--
-- Two tables: `documents` (curated official sources) and `chunks`
-- (200–400 token passages with heading-aware splits). Retrieval runs over
-- `chunks` using Postgres full-text search (tsvector/GIN) plus trigram
-- similarity for ASR-mangled entity names.
--
-- Safe to run repeatedly: every object uses IF NOT EXISTS.

create extension if not exists pg_trgm;

create table if not exists documents (
  id             bigserial primary key,
  source_name    text not null,
  source_url     text not null,
  authority_tier smallint not null check (authority_tier between 1 and 3),
  title          text not null,
  published_date date,
  effective_date date,
  superseded_by  bigint references documents(id),
  retrieved_at   timestamptz not null default now()
);

-- Ingestion keys off the canonical URL; keep it unique so re-runs upsert
-- instead of duplicating a source (see ingest/fetch.py).
create unique index if not exists documents_source_url_key
  on documents (source_url);

create table if not exists chunks (
  id          bigserial primary key,
  document_id bigint not null references documents(id) on delete cascade,
  ordinal     int not null,
  heading     text,
  content     text not null,
  tsv         tsvector generated always as (to_tsvector('english', content)) stored
);

create index if not exists chunks_tsv_idx  on chunks using gin (tsv);
create index if not exists chunks_trgm_idx on chunks using gin (content gin_trgm_ops);
create index if not exists chunks_doc_idx  on chunks (document_id);

-- A document's chunks are ordered; a chunk's position within its parent is
-- unique so re-chunking an updated document can upsert cleanly.
create unique index if not exists chunks_doc_ordinal_key
  on chunks (document_id, ordinal);
