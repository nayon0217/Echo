# ECHO — WhatsApp Bot

Voice-first AI verification bot for migrant workers in Singapore. See [`specs.md`](./specs.md) for the full project vision.

**Current step:** a user messages the bot (e.g. "Hi Echo!") and gets back an interactive menu to pick one of four languages (English, Bengali, Tamil, Mandarin).

## Stack

- Node.js 22 + Express — WhatsApp messaging layer (`src/`)
- Python 3.11+ — verification pipeline (`db/`, `pipeline/`, `ingest/`, `eval/`); see [`policy.md`](./policy.md)
- Postgres 16 + `pg_trgm` — the fact-checking corpus (FTS + trigram retrieval)
- WhatsApp Cloud API (Meta-hosted)

## Database

The verification corpus lives in Postgres. Schema is in [`db/schema.sql`](./db/schema.sql)
(`documents` + `chunks`, with a generated `tsvector` and GIN/trigram indexes).

### Start Postgres (Docker)

```bash
docker compose up -d db     # Postgres 16 on localhost:5432; schema auto-applied on first boot
```

Credentials and connection config live in `.env` (`POSTGRES_*`, or a single
`DATABASE_URL`). Defaults are `echo` / `echo` / `echo`.

### Python setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Apply / inspect the schema

The schema auto-applies the first time the Docker volume is created. To re-apply
it to an existing database (idempotent) or inspect it:

```bash
python -m db.init_db          # apply db/schema.sql
python -m db.init_db --check  # list tables + row counts
```

To wipe the data and get a fresh schema on next start: `docker compose down -v`.

## Corpus ingestion

The fact-check corpus is curated in [`ingest/sources.yaml`](./ingest/sources.yaml)
(one entry per official document, with `authority_tier`). `ingest/fetch.py`
fetches each URL with trafilatura, chunks it heading-aware (200–400 tokens,
50-token overlap, list-safe — see `ingest/chunker.py`), and upserts into Postgres
keyed on `source_url`.

```bash
python -m ingest.fetch --tier 1 --dry-run   # fetch + chunk, print stats, no writes
python -m ingest.fetch --tier 1             # ingest all tier-1 sources
python -m ingest.fetch                       # ingest every tier
python -m ingest.fetch --limit 3 --dry-run  # quick smoke test
```

Re-running upserts (replaces a document's chunks in place) rather than
duplicating, so editing `sources.yaml` and re-running is safe.

Current corpus: **38 tier-1 MOM documents, ~293 chunks** (Work Permit rules, levy,
salary, housing, medical, sector requirements). Tier-1 SPF ScamAlert is deferred
until the scam path is built (see the note in `sources.yaml`).

## Setup

1. Install dependencies:

   ```bash
   nvm use 22
   npm install
   ```

2. Confirm `.env` has your credentials (already present):

   - `number_ID` — WhatsApp phone number ID
   - `access_token` — Meta access token
   - `VERIFY_TOKEN` — any string; must match what you enter in the Meta dashboard
   - `PORT` — local port (default 3000)

3. Start the server:

   ```bash
   npm start
   # or, auto-reload on file changes:
   npm run dev
   ```

## Connect the webhook to Meta

Meta needs a public HTTPS URL to deliver messages. In development, tunnel your local port:

```bash
npx ngrok http 3000
```

Then in the **Meta App Dashboard → WhatsApp → Configuration → Webhook**:

- **Callback URL:** `https://<your-ngrok-subdomain>.ngrok-free.app/webhook`
- **Verify token:** the same value as `VERIFY_TOKEN` in `.env`
- Subscribe to the **`messages`** field.

## Test

From one of your registered test numbers, send any message (e.g. "Hi Echo!") to the bot number.
You should receive the "Choose language" list. Tapping a language sends back a confirmation.

## Files

- `src/index.js` — Express server + webhook (verify + receive)
- `src/whatsapp.js` — WhatsApp Cloud API send helpers
- `src/languages.js` — the four language options
- `db/schema.sql` — Postgres corpus schema (documents + chunks)
- `db/connection.py` — psycopg connection helpers (config from `.env`)
- `db/init_db.py` — apply / inspect the schema (`python -m db.init_db`)
- `docker-compose.yml` — Postgres 16 + `pg_trgm`
- `ingest/sources.yaml` — curated official-source list with authority tiers
- `ingest/fetch.py` — fetch → chunk → upsert into Postgres
- `ingest/chunker.py` — heading-aware, list-safe chunking
