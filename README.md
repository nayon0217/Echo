# ECHO — WhatsApp Bot

Voice-first AI verification bot for migrant workers in Singapore. See [`specs.md`](./specs.md) for the full project vision.

**Current step:** a worker forwards a text message, a voice note, or an image in any
language; the bot reads it, detects the language, and replies in the language they
picked from the menu. Verification of the claim itself is not built yet.

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

Current corpus: **73 documents, ~666 chunks** across tiers 1–3 (MOM/CPF/IRAS,
TWC2/MWC, CNA/ST). Tier-1 SPF ScamAlert is still deferred (see the note in
`sources.yaml`).

## Message routing

Every inbound message is classified by [`src/media.js`](./src/media.js) and dispatched
to a handler in [`src/handlers.js`](./src/handlers.js):

| WhatsApp type | Kind | What happens |
|---|---|---|
| `text` | text | Translate → claim extraction → DB retrieve → LLM true/false |
| `image` | image | Claude vision → same verification path as text |
| `audio` | voice | Whisper ASR → translate → same verification path. `audio.voice` marks an in-app recording |
| `interactive`, `button` | control | Language selection |
| everything else | unsupported | Logged and ignored, no reply |

All three content kinds converge on `verifyText()` after they have English text, so
voice and image inherit claim extraction and verification automatically.

**First contact is gated behind the language menu.** Until a sender picks a reply
language, any content message gets the menu instead. After they choose, ECHO only
sends a short welcome — verification starts on the *next* message they send.
Ignored media never triggers the menu.

The reply language is asked for rather than inferred on purpose. A forwarded scam is
often written in English regardless of what the worker actually reads, so answering in
the *detected* language would reply in English to someone who can't read it. Detected
language and reply language are separate: the first is what we translate from, the
second is what we answer in.

## Verification pipeline (Python)

Claude-backed stages (`policy.md` §1). Every LLM call uses schema-enforced structured
output — no free-text parsing.

| Stage | Module | What it does |
|---|---|---|
| 1 ASR / vision | `pipeline/asr.py`, `pipeline/vision.py` | voice → transcript; image → extracted text |
| 2 translate | `pipeline/translate.py` | detect language, translate to English (retrieval pivot) |
| 3 route | `pipeline/router.py` | multi-label: policy claim? scam signals? neither? |
| 4 claims | `pipeline/claims.py` | atomic, independently checkable assertions |
| 5–6 retrieve | `pipeline/retrieve.py` | FTS query gen + Postgres FTS/trigram |
| 7–9 verify | `pipeline/verify.py` | LLM verdict + citation audit + abstention gates |
| 10 compose | `pipeline/compose.py` | reasoning narrative reply (not a bare label) |
| — | `pipeline/scam.py` | scam-path stub (warning + hotline) |
| — | `pipeline/pipeline.py` | orchestrator (`process_message`) |
| — | `pipeline/trace.py` | JSONL timings/verdicts (never transcripts) |
| — | `eval/golden.csv`, `eval/run_eval.py` | golden set + recall@8 / confusion matrix |

`app/webhook.py` exposes `/transcribe`, `/extract`, `/translate`, and `/process` so the
Node layer can call the full stack on one port (`PIPELINE_URL`).

```bash
source .venv/bin/activate
uvicorn app.webhook:app --reload --port 8000
```

The port must match `PIPELINE_URL` in `.env`. Bind to localhost only — this process
holds the Claude API key.

Try it without the bot:

```bash
python -m pipeline.translate "এটা কি সত্যি?"        # prints the parsed JSON
python -m pipeline.pipeline "MOM raised the levy to $900"
python -m eval.run_eval --retrieval-only            # phase 2: recall@8
python -m eval.run_eval --limit 5                   # phase 3–4 smoke
curl localhost:8000/health
curl -X POST localhost:8000/translate \
  -H "Content-Type: application/json" -d '{"text":"இது உண்மையா?"}'
curl -X POST localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"text":"MOM raised the work permit levy to $900 in 2026"}'
```

The model is set by `CLAUDE_MODEL` in `.env` and the key by `CLAUDE_API_KEY`.

### Voice notes

[`pipeline/asr.py`](./pipeline/asr.py) is stage 1: faster-whisper transcribes the note
in its original language, then `translate_transcript()` renders it both into English
(the retrieval pivot) and into the language the worker picked — one API call for both.
The English pivot is then sent to `/process` for claim extraction and true/false.

Between ASR and translate sits **abstention gate 1** (policy.md §7): Whisper's
token-weighted mean log-probability. Below `MIN_MEAN_LOGPROB`, the transcript is
returned untranslated and the bot asks for a re-record rather than pushing a
mis-heard claim into verification.

```bash
python -m pipeline.asr voice.ogg                      # transcript + confidence
curl -X POST localhost:8000/transcribe \
  -F "file=@voice.ogg" -F "target_language=bn"        # transcribe + translate
```

`WHISPER_MODEL` defaults to `large-v3` per policy.md §2 — the first run downloads
~3 GB. Set `WHISPER_MODEL=base` (~145 MB) while iterating; accuracy on non-English
audio drops sharply, so tune gate thresholds against the model you'll actually ship.
`WHISPER_DEVICE` and `WHISPER_COMPUTE_TYPE` default to `cpu` / `int8`.

WhatsApp voice notes are OGG/Opus, decoded via PyAV — no ffmpeg binary needed.

### Images

[`pipeline/vision.py`](./pipeline/vision.py) is stage 1 for images: Claude reads the
text out of a screenshot, a photo of a letter, or a poster. It then goes through the
same `translate_transcript()` call the voice path uses, then `/process` for verification.

There is no separate OCR engine on purpose. These images are photographed at an angle,
in bad light, half-cropped, and often mix scripts; reading them well needs the layout
and the surrounding context together, which a bare OCR pass throws away.

```bash
python -m pipeline.vision screenshot.png              # extracted text + confidence
curl -X POST localhost:8000/extract \
  -F "file=@job-ad.jpg" -F "target_language=ta"       # read + translate
```

**The image gate is weaker than the voice one.** Gate 1 uses Whisper's own
log-probability. Vision models expose no equivalent, so `Extraction.confidence` is
Claude's *self-report*. Below `MIN_CONFIDENCE` (0.6) the image comes back untranslated
and the bot asks for a sharper photo.

If the picture can't be read but the worker typed a caption with it, the caption is
used instead.

Not built yet: AI-generation detection (specs.md §5). A forged "MOM letter" that reads
cleanly is transcribed and verified without a synthetic-media flag.


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

## Run the bot + pipeline together

The WhatsApp bot (Node) and the verification pipeline (Python) run as two
processes. The bot POSTs each forwarded message to the pipeline service and
replies with the extracted claims.

```bash
# 1. Postgres (corpus)
docker compose up -d db

# 2. Pipeline service (Python) — the bot calls this
source .venv/bin/activate
uvicorn app.webhook:app --host 127.0.0.1 --port 8000

# 3. WhatsApp bot (Node), in another terminal
npm start
```

Flow: a user messages the bot → picks a language → sends a voice note, image, or
text. Voice goes through Whisper ASR first; then every path runs translate → route →
claim extraction → Postgres retrieve → LLM true/false and the bot replies with the
verdict. Scam-looking messages get a warning; messages with no checkable claim get
the MOM hotline template. The bot talks to the pipeline at `PIPELINE_URL`
(default `http://127.0.0.1:8000`).

> Note: outbound replies require a valid WhatsApp `access_token` in `.env`.
> A `401 Authentication Error` in the logs means the token has expired — refresh
> it in the Meta dashboard.

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

### Automated tests

The suite exists so the text and voice features can be verified **without** access to
the Meta webhook. Everything except the two calls to Meta's servers is covered.

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest                # unit: no network, no model weights, ~0.5s
npm test              # Node: routing + handlers, fetch stubbed in-process, ~0.1s
pytest --live         # real Claude API + real Whisper, ~2 min, costs tokens
```

`pytest` and `npm test` are free and fast — run them on every change. `pytest --live`
is the acceptance check: it answers "does translation actually work", using real API
calls and real audio.

Voice fixtures are synthesised at run time with macOS `say` and transcoded to
OGG/Opus with PyAV — the same container WhatsApp sends. No ffmpeg, no committed audio.
They are cached in `tests/fixtures/audio/` (gitignored), so only the first run pays for
synthesis. Image fixtures are rendered with Pillow and can be degraded on demand (blur,
downscale, rotate) to exercise the extraction gate — a synthetic image is the point,
since we know exactly what text is in it.

**Live runs default to `WHISPER_MODEL=base`**, which is the model cached locally; the
policy.md default (`large-v3`) is a ~3 GB download. `base` mis-hears numbers in
non-English audio, so the tests that assert number fidelity there skip unless you run
against large-v3:

```bash
WHISPER_MODEL=large-v3 pytest --live
```

What each file covers:

- `tests/test_asr_unit.py` — abstention gate 1 (policy.md §7) as pure logic
- `tests/test_translate_unit.py` — input validation, the `<message>` envelope, schema enforcement
- `tests/test_webhook_unit.py` — status-code mapping, the gate-1 short circuit, temp-file cleanup
- `tests/test_vision_unit.py` — the image gate, media-type and size validation, prompt shape
- `tests/test_translate_live.py` — real translation: 4 languages, detail preservation, prompt injection
- `tests/test_voice_live.py` — real audio → Whisper → Claude, through the FastAPI route
- `tests/test_vision_live.py` — real images → Claude vision → Claude, through the FastAPI route
- `tests/media.test.js` — inbound message classification against real Meta payload shapes
- `tests/handlers.test.js` — handlers + pipeline client + media download, with `fetch` stubbed

## Files

- `src/index.js` — Express server + webhook (verify + receive), per-sender state, dispatch
- `src/media.js` — classifies an inbound message: text / image / voice / control / unsupported
- `src/handlers.js` — one handler per kind: text, voice, image, ignored → verifyText
- `src/whatsapp.js` — WhatsApp Cloud API send helpers
- `src/pipeline.js` — HTTP client for the Python pipeline + reply formatting
- `src/languages.js` — the four language options
- `pipeline/asr.py` — faster-whisper speech to text + confidence (policy.md §1 stage 1)
- `pipeline/vision.py` — read text out of an image + confidence (stage 1, image path)
- `pipeline/llm.py` — Claude client, schema-enforced structured output
- `pipeline/translate.py` · `router.py` · `claims.py` · `retrieve.py` · `verify.py` · `scam.py` — stages 2–9 + scam stub
- `pipeline/pipeline.py` — orchestrator (message → verified claims)
- `app/webhook.py` — FastAPI service (`/transcribe`, `/extract`, `/translate`, `/process`)
- `app/api.py` — re-exports `app.webhook:app` for compatibility
- `db/schema.sql` — Postgres corpus schema (documents + chunks)
- `db/connection.py` — psycopg connection helpers (config from `.env`)
- `db/init_db.py` — apply / inspect the schema (`python -m db.init_db`)
- `docker-compose.yml` — Postgres 16 + `pg_trgm`
- `ingest/sources.yaml` — curated official-source list with authority tiers
- `ingest/fetch.py` — fetch → chunk → upsert into Postgres
- `ingest/chunker.py` — heading-aware, list-safe chunking
- `tests/` — see [Automated tests](#automated-tests); `conftest.py` synthesises voice fixtures
- `pipeline/llm.py` — Claude client, schema-enforced structured output
- `pipeline/translate.py` · `router.py` · `claims.py` · `retrieve.py` · `verify.py` · `scam.py` — stages 2–9 + scam stub
- `pipeline/pipeline.py` — orchestrator (message → verified claims)
