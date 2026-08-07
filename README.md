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

Current corpus: **38 tier-1 MOM documents, ~293 chunks** (Work Permit rules, levy,
salary, housing, medical, sector requirements). Tier-1 SPF ScamAlert is deferred
until the scam path is built (see the note in `sources.yaml`).

<<<<<<< HEAD
## Message routing

Every inbound message is classified by [`src/media.js`](./src/media.js) and dispatched
to a handler in [`src/handlers.js`](./src/handlers.js):

| WhatsApp type | Kind | What happens |
|---|---|---|
| `text` | text | Runs the translate pipeline below |
| `image` | image | Stub — replies "can't check images yet" |
| `audio` | voice | Downloads, transcribes with Whisper, replies in the worker's chosen language. `audio.voice` distinguishes an in-app recording from an uploaded file |
| `interactive`, `button` | control | Language selection |
| everything else | unsupported | Logged and ignored, no reply |

All three content kinds converge on `processText()`, which is where the verification
stages (policy.md §1 stages 3–10) will live — so voice and image inherit them once
transcription and image parsing land.

**First contact is gated behind the language menu.** Until a sender picks a reply
language, any content message gets the menu instead — but the message is held in
`session.pending` and processed as soon as they choose, so nothing has to be sent
twice. Ignored media never triggers the menu.

The reply language is asked for rather than inferred on purpose. A forwarded scam is
often written in English regardless of what the worker actually reads, so answering in
the *detected* language would reply in English to someone who can't read it. Detected
language and reply language are separate: the first is what we translate from, the
second is what we answer in.

## Verification pipeline (Python)

`pipeline/translate.py` implements stage 2 of [`policy.md`](./policy.md) §1: detect the
message's language and translate it to English, the pivot language the corpus is
indexed in. Output is schema-enforced via structured outputs — never parsed out of
free text. `app/webhook.py` exposes it over HTTP so the Node layer can call it.

```bash
source .venv/bin/activate
uvicorn app.webhook:app --reload --port 8000
```

The port must match `PIPELINE_URL` in `.env`. Bind to localhost only — this process
holds the Claude API key.

Try it without the bot:

```bash
python -m pipeline.translate "এটা কি সত্যি?"        # prints the parsed JSON
curl localhost:8000/health
curl -X POST localhost:8000/translate \
  -H "Content-Type: application/json" -d '{"text":"இது உண்மையா?"}'
```

The model is set by `CLAUDE_MODEL` in `.env` (currently `claude-sonnet-5`) and the key
by `CLAUDE_API_KEY`.

### Voice notes

[`pipeline/asr.py`](./pipeline/asr.py) is stage 1: faster-whisper transcribes the note
in its original language, then `translate_transcript()` renders it both into English
(the retrieval pivot) and into the language the worker picked — one API call for both.

Between those two steps sits **abstention gate 1** (policy.md §7): Whisper's
token-weighted mean log-probability. Below `MIN_MEAN_LOGPROB`, the transcript is
returned untranslated and the bot asks for a re-record rather than pushing a
mis-heard claim into verification. This is the only genuine model-confidence signal in
the pipeline — Claude does not expose logprobs.

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
same `translate_transcript()` call the voice path uses, so an image and a voice note
converge after their first stage.

There is no separate OCR engine on purpose. These images are photographed at an angle,
in bad light, half-cropped, and often mix scripts; reading them well needs the layout
and the surrounding context together, which a bare OCR pass throws away.

```bash
python -m pipeline.vision screenshot.png              # extracted text + confidence
curl -X POST localhost:8000/extract \
  -F "file=@job-ad.jpg" -F "target_language=ta"       # read + translate
```

**The image gate is weaker than the voice one, and it is worth knowing why.** Gate 1
uses Whisper's own log-probability — a real model-confidence signal. Vision models
expose no equivalent through the API, so `Extraction.confidence` is Claude's
*self-report*: it is asked how well it could read the image and it answers. That
catches genuinely unreadable images well and does not catch a confidently misread
digit. Below `MIN_CONFIDENCE` (0.6) the image comes back untranslated and the bot asks
for a sharper photo.

Measured on a rendered notice put through increasing Gaussian blur: radius 3 → 0.97
confidence and a correct read; radius 5 → 0.75, still correct; radius 8 → 0.40, and the
model misread `$800` as `$600`. The gate rejected exactly the case that was wrong.
That is one data point on one image, not a calibration — like the ASR threshold, this
one gets tuned on the golden set in phase 4 (policy.md §7).

If the picture can't be read but the worker typed a caption with it, the caption is
used instead: those are their own words, carry no OCR risk, and are usually where the
actual question is.

Not built yet: AI-generation detection (specs.md §5). A forged "MOM letter" that reads
cleanly is transcribed and translated without comment.
=======
## Verification pipeline (LLM)

Claude-backed, text stages built so far (`policy.md` §1). Every LLM call uses
schema-enforced structured output (forced tool-use, `temperature=0`) — no
free-text parsing.

| Stage | Module | What it does |
|---|---|---|
| 2 translate | `pipeline/translate.py` | detect language, translate to English (retrieval pivot) |
| 3 route | `pipeline/router.py` | multi-label: policy claim? scam signals? unintelligible? |
| 4 claims | `pipeline/claims.py` | extract atomic, self-contained, checkable claims |
| 5 queries | `pipeline/retrieve.py` | 3–5 official-terminology FTS queries per claim |
| 6 retrieve | `pipeline/retrieve.py` | run queries over Postgres FTS + trigram, dedupe, rerank tier-1 first, attach the matching source document(s) to each claim |
| 7–9 verify | `pipeline/verify.py` | Pass A verdict → Pass B per-citation audit → abstention gates → `supported`/`refuted`/`insufficient` |
| (scam stub) | `pipeline/scam.py` | real warning message; merged in when router flags a scam |

Each claim comes back with a **verdict**, `reasoning`, and `cited_sources` (only
the citations that survived the audit + gates). The design optimises for
*precision on confident verdicts*: it returns `insufficient` (and points to the
MOM hotline) rather than risk a confident-wrong answer — e.g. a fabricated
"\$300 renewal fee" is marked `insufficient`, not refuted from absence.

Abstention gates (`policy.md` §7): top retrieval score below floor, audit
stripped all citations, all citations tier-3, or a cited doc is future-dated /
superseded. Tune the floor with `RETRIEVAL_SCORE_FLOOR` in `.env`.

Test the pieces alone:

```bash
python -m pipeline.retrieve "MOM raised the work permit levy to \$900 in 2026"
python -m pipeline.verify   "Employers must pay a monthly levy for each Work Permit holder"
```

Set `CLAUDE_API_KEY` and `CLAUDE_MODEL` in `.env`. Then, given a forwarded
message, return the extracted claims (each with generated search queries):

```bash
python -m pipeline.pipeline "MOM raised the work permit levy to \$900 in 2026"
echo "levy naik jadi 900 dollar tahun 2026" | python -m pipeline.pipeline
```

Routing behaviour: a message can be a policy claim, a scam, both, or neither.
Scam messages get a warning; messages with no checkable claim get the MOM hotline
template. Audio transcription (stage 1) and retrieval/verification (stages 6–10)
are not wired yet — the pipeline currently stops after claim extraction.
>>>>>>> 2d5c287 (LLM layer added)

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
uvicorn app.api:app --host 127.0.0.1 --port 8000

# 3. WhatsApp bot (Node), in another terminal
npm start
```

Flow: a user messages the bot → picks a language → any text they then send is
run through the pipeline (translate → route → claims) and the extracted claims
are sent back. Scam-looking messages get a warning; messages with no checkable
claim get the MOM hotline template. The bot talks to the pipeline at
`PIPELINE_URL` (default `http://127.0.0.1:8000`).

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

<<<<<<< HEAD
- `src/index.js` — Express server + webhook (verify + receive), per-sender state, dispatch
- `src/media.js` — classifies an inbound message: text / image / voice / control / unsupported
- `src/handlers.js` — one handler per kind: text, voice, image, ignored
- `src/whatsapp.js` — WhatsApp Cloud API send helpers
- `src/pipeline.js` — HTTP client for the Python pipeline
- `src/languages.js` — the four language options
- `pipeline/asr.py` — faster-whisper speech to text + confidence (policy.md §1 stage 1)
- `pipeline/vision.py` — read text out of an image + confidence (stage 1, image path)
- `pipeline/translate.py` — detect language + translate (policy.md §1 stage 2)
- `app/webhook.py` — FastAPI service exposing the pipeline on port 8000
=======
- `src/index.js` — Express server + webhook (verify + receive + route to pipeline)
- `src/whatsapp.js` — WhatsApp Cloud API send helpers
- `src/pipeline.js` — calls the Python pipeline service, formats the claims reply
- `src/languages.js` — the four language options
- `app/api.py` — FastAPI pipeline service (`POST /process`)
>>>>>>> 2d5c287 (LLM layer added)
- `db/schema.sql` — Postgres corpus schema (documents + chunks)
- `db/connection.py` — psycopg connection helpers (config from `.env`)
- `db/init_db.py` — apply / inspect the schema (`python -m db.init_db`)
- `docker-compose.yml` — Postgres 16 + `pg_trgm`
- `ingest/sources.yaml` — curated official-source list with authority tiers
- `ingest/fetch.py` — fetch → chunk → upsert into Postgres
- `ingest/chunker.py` — heading-aware, list-safe chunking
<<<<<<< HEAD
- `tests/` — see [Automated tests](#automated-tests); `conftest.py` synthesises voice fixtures
=======
- `pipeline/llm.py` — Claude client, schema-enforced structured output
- `pipeline/translate.py` · `router.py` · `claims.py` · `retrieve.py` · `verify.py` · `scam.py` — stages 2–9 + scam stub
- `pipeline/pipeline.py` — orchestrator + CLI (message → extracted claims)
>>>>>>> 2d5c287 (LLM layer added)
