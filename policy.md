# ECHO — Policy Verification Pipeline: Build Plan

**Scope:** The policy fact-checking path only. Scam-pattern path is stubbed but designed for.
**Optimisation target:** correctness, specifically *precision on confident verdicts*. See §9 for why this is the right target rather than accuracy.

---

## 1. Pipeline overview

```
inbound audio
  → [1] transcribe            (faster-whisper, returns confidence)
  → [2] translate to English  (pivot language for retrieval)
  → [3] route                 (multi-label: policy? scam? neither?)
  → [4] extract atomic claims (per claim, not per transcript)
  → [5] generate FTS queries  (3-5 per claim)
  → [6] retrieve              (Postgres FTS + trigram, union + rerank)
  → [7] verdict pass          (LLM, cites chunk IDs only)
  → [8] citation audit pass   (each citation checked in isolation)
  → [9] abstention gates      (downgrade to "insufficient" on any failure)
  → [10] compose reply        (reasoning narrative, not a label)
```

Stages 7 and 8 are the "2 runs." Stage 9 is what actually protects you.

---

## 2. Tech stack

| Stage | Choice | Rationale |
|---|---|---|
| Runtime | Python 3.11+, FastAPI, uvicorn | Async webhook handling; WhatsApp needs a fast 200 OK, then process in a background task |
| Queue | `asyncio` background task for hackathon; Redis + RQ if it grows | Meta retries webhooks that don't ack quickly |
| Database | Postgres 16, `pg_trgm` extension | FTS is built in; no separate vector service to run or debug |
| ASR | `faster-whisper` large-v3 (CTranslate2) | 4× faster than reference Whisper, same weights, exposes per-segment logprob |
| Translation | LLM call (not a separate MT model) | One less dependency; quality is adequate for pivot-to-English |
| LLM calls | Structured output / JSON schema enforced, `temperature=0` | Free-text parsing is a correctness leak. Never regex an LLM response |
| Ingestion | `trafilatura` for HTML → text, plus manual YAML for the top ~40 docs | Manual curation beats scraping at this corpus size |
| Eval | Plain `pytest` + CSV golden set + a confusion-matrix script | Resist eval frameworks; you need to read every failure by hand |
| Tracing | JSONL log per request, one line per stage | You must be able to answer "was that a retrieval failure or a reasoning failure?" |

**Deliberately not using:** a vector DB, LangChain/LlamaIndex, a reranker model. At a few hundred documents each adds a failure surface without adding recall. Revisit pgvector only if §9 evals show retrieval recall is your bottleneck.

---

## 3. The corpus (this is where correctness actually comes from)

Ranked by marginal correctness per hour spent, corpus quality beats every model choice below it. Budget accordingly.

### Sources, by authority tier

| Tier | Source | Notes |
|---|---|---|
| 1 | MOM work pass pages, press releases, advisories (mom.gov.sg) | Canonical for permit/levy/pass rules |
| 1 | SPF Scam Alert bulletins (scamalert.sg) | Named scam typologies with modus operandi |
| 1 | CPF, IRAS levy pages | Only the pages touching migrant workers |
| 2 | TWC2, Migrant Workers' Centre advisories | Fills gaps MOM doesn't address; flag as tier 2 |
| 3 | Straits Times / CNA reporting on policy changes | Use only for *dating* changes, never as sole support |

Start with **40 hand-picked documents**, not 400 scraped ones. Coverage of the claims people actually make matters far more than volume, and you can only know what those claims are from your golden set (§8).

### Chunking

Split on headings, then on paragraph boundaries, targeting 200–400 tokens with 50-token overlap. Do **not** use fixed character windows — policy documents have list structures where a split mid-list produces a chunk that reads as a complete but false statement.

Every chunk keeps its parent document's metadata. This matters for §7's temporal check.

### Schema

```sql
create extension if not exists pg_trgm;

create table documents (
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

create table chunks (
  id          bigserial primary key,
  document_id bigint not null references documents(id) on delete cascade,
  ordinal     int not null,
  heading     text,
  content     text not null,
  tsv         tsvector generated always as (to_tsvector('english', content)) stored
);

create index chunks_tsv_idx  on chunks using gin (tsv);
create index chunks_trgm_idx on chunks using gin (content gin_trgm_ops);
create index chunks_doc_idx  on chunks (document_id);
```

`superseded_by` is not optional. A confidently-cited obsolete advisory is one of the worst outputs this system can produce.

---

## 4. Router (stage 3)

Single LLM call, multi-label, schema-enforced:

```json
{
  "contains_policy_claim": true,
  "contains_scam_signals": true,
  "scam_signals": ["claimed_authority", "urgency", "payment_request", "bypass_normal_channel"],
  "language_detected": "bn",
  "unintelligible": false
}
```

Route to the policy path if `contains_policy_claim`. Route to the scam handler (stub: returns a generic warning) if `contains_scam_signals`. Both can fire. If neither fires, reply with the "I can't check this, here's the MOM hotline" template.

Build the scam stub as a real function signature returning a real message — not a `pass`. That way the compose stage (§10) is already written to merge two result objects when you fill it in.

---

## 5. Claim extraction (stage 4)

Output a list of atomic, independently checkable assertions. Each must be a complete sentence with its subject restored (pronouns resolved), because it will be retrieved and verified without the surrounding transcript.

```json
{
  "claims": [
    {"text": "The Ministry of Manpower has increased the work permit levy in 2026.", "type": "policy_change"},
    {"text": "Workers must pay a $300 processing fee to renew a work permit.", "type": "fee"}
  ]
}
```

Reject claims that are instructions, threats, or opinions — those belong to the scam path. A claim that can't be phrased as "X is true / X is false" is not a claim.

---

## 6. Retrieval (stages 5–6)

**The single biggest retrieval failure is vocabulary mismatch.** A worker says "the government paper fee went up"; the corpus says "levy rates for Work Permit holders." Searching the raw claim text will return nothing, and nothing looks identical to *refuted* if you're not careful.

So: generate queries with an LLM before searching.

```python
QUERY_PROMPT = """Given this claim, produce 4 Postgres full-text search queries
that would find official Singapore government documents relevant to verifying it.
Use official terminology (e.g. "Work Permit", "levy", "S Pass"), not colloquial phrasing.
Vary specificity: one broad, two targeted, one covering the specific figure or date."""
```

Then execute against both indexes and union:

```sql
-- lexical
select c.id, c.content, d.source_name, d.effective_date, d.authority_tier,
       ts_rank_cd(c.tsv, query) as score
from chunks c
join documents d on d.id = c.document_id
   , websearch_to_tsquery('english', %(q)s) query
where c.tsv @@ query
  and d.superseded_by is null
order by score desc
limit 8;

-- fuzzy fallback, catches ASR-mangled entity names
select c.id, c.content, similarity(c.content, %(q)s) as score
from chunks c
where c.content %% %(q)s
order by score desc
limit 4;
```

Dedupe by chunk id, keep top 8 overall, sort by `authority_tier` then score so tier-1 sources reach the model first.

**Record the top score.** It feeds the abstention gate in §7.

---

## 7. Two-pass verification (stages 7–9)

### Pass A — verdict

Input: one claim + up to 8 numbered chunks. Output schema:

```json
{
  "verdict": "supported | refuted | insufficient",
  "cited_chunk_ids": [12, 47],
  "reasoning": "..."
}
```

Constrain `cited_chunk_ids` to the IDs actually supplied. Instruct explicitly: *absence of a statement in these passages is not evidence the claim is false — return `insufficient`.* Without that line, models reliably return `refuted` for anything they can't find, which is exactly the confident-wrong output you can't afford.

### Pass B — citation audit

For **each** cited chunk, a separate call with no other context:

> Does this passage, on its own, entail the claim, contradict it, or neither?

Drop every citation that comes back "neither." This is the pass that catches the topically-similar-but-irrelevant retrieval, which is the dominant failure mode of naive RAG fact-checking.

You can use a cheap/small model here — it's a narrow NLI-shaped task. A DeBERTa-MNLI cross-encoder is a valid, near-free alternative if you want it off the LLM budget entirely.

### Abstention gates (apply in order, any failure → `insufficient`)

1. ASR mean logprob below threshold → don't verify at all; reply asking for a re-record
2. Top retrieval score below floor (tune on golden set, don't guess)
3. Pass B stripped all citations
4. Verdict is `supported`/`refuted` but all surviving citations are authority tier 3
5. A cited document's `effective_date` is in the future, or `superseded_by` is non-null

Gate 4 is what stops a news article about a *proposed* change being cited as proof the change happened.

---

## 8. Golden set and evaluation

**Build this before the pipeline.** 50 cases minimum, as a CSV:

| field | notes |
|---|---|
| `id` | |
| `transcript_en` | typed, clean — decouples eval from ASR |
| `audio_path` | optional, for later ASR-inclusive runs |
| `claims_expected` | list |
| `verdict_expected` | supported / refuted / insufficient |
| `must_cite_doc` | for supported/refuted cases, which document *should* be found |
| `provenance` | scripted / real-forwarded / MOM-genuine |

Composition target: ~15 genuine policy claims (verifiable true), ~15 false policy claims, ~15 with no checkable claim at all, ~5 true-but-obsolete (tests gate 5).

### Metrics to report

Report per-class precision and recall separately, plus **retrieval recall@8 measured independently** — the fraction of cases where `must_cite_doc` appeared in the retrieved set at all. Without that number you cannot tell whether a wrong verdict was a retrieval miss or a reasoning error, and you'll tune the wrong stage for days.

---

## 9. Why precision, not accuracy

The error costs are wildly asymmetric:

| Error | Consequence |
|---|---|
| False `supported` on a scam claim | A worker believes a scam is legitimate and loses money. **Catastrophic** |
| False `refuted` on a real policy | A worker ignores a genuine requirement. Serious |
| Unnecessary `insufficient` | A worker calls the MOM hotline. Mildly annoying, arguably correct behaviour anyway |

Tune for **precision on `supported` above 0.98**, accept whatever recall that costs. A system that abstains 60% of the time and is never confidently wrong is a *better* MIL tool than one that's 85% accurate with occasional confident errors — and it's more defensible in front of judges, because abstention is the behaviour PAUSE is trying to teach.

---

## 10. Compose (stage 10)

Never emit a bare label. The reply structure, per the spec's teaching goal:

1. What was claimed (restated plainly, so the worker knows what was checked)
2. What official sources say — with the source *named* ("MOM's website says…"), not just cited
3. The verdict, phrased as reasoning
4. Concrete next action — always the verified hotline for `insufficient`

Merge in the scam-path result if the router flagged both.

---

## 11. Privacy note

The spec commits to no personal data retention. That conflicts with logging transcripts for evaluation. Resolve it explicitly:

- **Production:** store the verdict, retrieval scores, and stage timings. Never the transcript or audio.
- **Golden set:** separate, consented, scripted or partner-supplied material only.
- Make this a documented decision, not an oversight — judges will ask.

---

## 12. Build sequence

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Golden set CSV, 50 rows | Every row hand-labelled |
| 1 | Corpus ingested, 40 docs chunked and indexed | FTS query from psql returns sensible chunks |
| 2 | Retrieval only | recall@8 measured against golden set |
| 3 | Pass A + Pass B on typed text | Confusion matrix produced |
| 4 | Abstention gates tuned | Precision on `supported` ≥ 0.98 |
| 5 | Whisper wired in front | Re-run eval on audio; measure the delta |
| 6 | WhatsApp webhook + compose | End-to-end demo |

Phases 0–4 are all text. Do not add audio until phase 5 — if ASR is in the loop while you're debugging retrieval, you'll spend the hackathon unable to attribute failures.

---

## 13. Repo layout

```
echo/
  ingest/
    sources.yaml          # curated doc list w/ metadata
    fetch.py              # trafilatura → chunks → Postgres
  db/
    schema.sql
  pipeline/
    asr.py                # faster-whisper, returns (text, confidence)
    router.py             # stage 3
    claims.py             # stage 4
    retrieve.py           # stages 5-6
    verify.py             # stages 7-8 + gates
    compose.py            # stage 10
  eval/
    golden.csv
    run_eval.py           # confusion matrix + retrieval recall
  app/
    webhook.py            # FastAPI, acks fast, processes in background
```