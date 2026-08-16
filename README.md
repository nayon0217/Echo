# ECHO — WhatsApp Bot

Voice-first AI verification bot for migrant workers in Singapore. See [`specs.md`](./specs.md) for the project vision and [`policy.md`](./policy.md) for the verification pipeline.

A worker picks a reply language, then forwards a text, voice note, or image. The bot
translates it, checks claims against official Singapore sources (MOM / CPF / IRAS,
TWC2 / MWC, CNA / ST), and replies in that language as **text plus a voice note**.
Scam-looking messages get a short warning. They can also send their employment
contract and ask questions about *that document*.

## Stack

- Node.js 22 + Express — WhatsApp messaging layer (`src/`)
- Python 3.10+ — verification pipeline (`db/`, `pipeline/`, `ingest/`, `eval/`); see [`policy.md`](./policy.md)
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

Current corpus: **73 documents** across tiers 1–3 (MOM/CPF/IRAS, TWC2/MWC,
CNA/ST). Tier-1 SPF ScamAlert is still deferred (see the note in `sources.yaml`).
Retrieval diversifies hits across those source families so CPF/IRAS and NGO/news
are not crowded out by MOM.

## Languages

The first message from a new sender is a WhatsApp **list** of reply languages
(the Cloud API allows at most 3 reply buttons, so six options use a list):

English, Bahasa Indonesia, Burmese, Bengali, Tamil, Mandarin.

The reply language is asked for rather than inferred on purpose. A forwarded
scam is often written in English regardless of what the worker actually reads,
so answering in the *detected* language would reply in English to someone who
can't read it. Detected language and reply language are separate: the first is
what we translate from, the second is what we answer in.

Until a sender picks a language, any content message gets the menu instead.
After they choose, ECHO sends a short welcome — verification starts on the
*next* message they send. Ignored media never triggers the menu.

## Message routing

Every inbound message is classified by [`src/media.js`](./src/media.js) and dispatched
from [`src/index.js`](./src/index.js) to a handler in [`src/handlers.js`](./src/handlers.js).

| WhatsApp type | Kind | What happens |
|---|---|---|
| `text` | text | If it looks like a question about *their* contract (see below), prompt for the file. Otherwise translate → route → claims → DB retrieve → LLM true/false |
| `image` | image | Claude vision. While waiting for a contract upload, try the contract reader first; otherwise (or if it is not a contract) verify like text. A new image **while a contract is already held** is fact-checked, not treated as a replacement document |
| `audio` | voice | Whisper ASR → translate → same verification path. `audio.voice` marks an in-app recording |
| `document` | document | Employment contract PDF (or similar). Read once, hold in memory, answer any pending question |
| `interactive`, `button` | control | Language selection |
| everything else | unsupported | Logged and ignored, no reply |

Forwarded voice notes still go through verification even in contract mode, so the
worker can check a suspicious message without losing the held document.

### Contract questions vs policy claims

[`src/contractQuestion.js`](./src/contractQuestion.js) separates “what does *my*
contract say?” from MOM policy rumours. Hints include `contract`, `my salary`,
notice period, deductions, hours, leave. Levy / ScamShield rumours stay on the
policy path.

Flow when no document is held yet:

1. Typed contract-like question → remember it as `pendingContractQuestion` and
   ask them to send the PDF or page photos (`SEND_CONTRACT_PROMPT`).
2. Usable upload → hold the text in the in-memory session, answer the held
   question, then keep the document for follow-ups.
3. Later typed messages are `/contract/ask` against **that same text** until
   they send `done` (or `exit`, `stop`, `back`, `cancel`, `finish`).
4. Restarting `npm start` clears the session; they must send the contract again.

Send `done` while waiting for an upload to drop the pending question without
entering contract mode.

## Verification pipeline (Python)

Claude-backed stages (`policy.md` §1). Every LLM call uses schema-enforced structured
output — no free-text parsing.

| Stage | Module | What it does |
|---|---|---|
| 1 ASR / vision | `pipeline/asr.py`, `pipeline/vision.py` | voice → transcript; image → extracted text |
| 2 translate | `pipeline/translate.py` | detect language, translate to English (retrieval pivot) |
| 3 route | `pipeline/router.py` | multi-label: policy claim? scam signals? neither? |
| 4 claims | `pipeline/claims.py` | atomic, independently checkable assertions |
| 5–6 retrieve | `pipeline/retrieve.py` | FTS query gen + Postgres FTS/trigram, diversified across source families |
| 7–9 verify | `pipeline/verify.py` | LLM verdict + citation audit + abstention gates |
| 10 compose | `pipeline/compose.py` | reasoning narrative in easy English, then localise; ≤75 words, **complete sentences** (never chopped with “…”) |
| — | `pipeline/scam.py` | scam-path stub (warning + MOM / ScamShield hotlines + “Why:” flags) |
| — | `pipeline/pipeline.py` | orchestrator (`process_message`) |
| — | `pipeline/trace.py` | JSONL timings/verdicts (never transcripts) |
| — | `eval/golden.csv`, `eval/run_eval.py` | golden set + recall@8 / confusion matrix |

[`app/webhook.py`](./app/webhook.py) exposes `/health`, `/transcribe`, `/extract`,
`/translate`, `/process`, `/speak`, `/contract`, and `/contract/ask` so the Node
layer can call the full stack on one port (`PIPELINE_URL`).

```bash
source .venv/bin/activate
uvicorn app.webhook:app --reload --host 127.0.0.1 --port 8000
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

The model is set by `CLAUDE_MODEL` in `.env` and the key by `CLAUDE_API_KEY`
(or `ANTHROPIC_API_KEY`). Adaptive thinking is not used — Claude Sonnet 4.5
rejects it with HTTP 400.

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

### Voice replies

[`pipeline/tts.py`](./pipeline/tts.py) speaks every content-bearing reply, because
specs.md §2 is built on the premise that *"reading is the barrier"* — a text-only bot
asks the worker to do the one thing they came here to avoid. Each reply is sent
twice: as text, and as a WhatsApp voice note in the language they picked.

```bash
python -m pipeline.tts "Your salary is 800 dollars a month." --language ta --out reply.ogg
curl -X POST localhost:8000/speak -H "Content-Type: application/json" \
  -d '{"text":"Possible scam.","language":"bn"}' --output reply.ogg
```

**Why not Puter.js.** It is a browser SDK — it ships as `<script src="https://js.puter.com/v2/">`
and authenticates against a logged-in browser user. There is no server-side package (the
`puter` npm entry is an unrelated 570-byte stub), so using it would mean driving a
headless browser per reply. `edge-tts` needs no API key, no account, and no browser, and
has neural voices for all six languages — which gTTS and macOS `say` do not cover
between them.

**Why OGG/Opus.** WhatsApp renders audio as a proper voice note — waveform, inline
playback — only for OGG with the Opus codec. Anything else arrives as a file attachment
the worker has to open. edge-tts returns MP3, so `tts.py` transcodes with PyAV, which
faster-whisper already pulls in; no ffmpeg binary needed.

Voices are one per language, chosen for the Singapore context rather than the largest
speaker population — `id-ID` (Bahasa Indonesia), `my-MM` (Burmese), `bn-BD` (most
Bengali speakers here are Bangladeshi, and the two accents differ audibly), `ta-IN`,
`zh-CN`, `en-SG`. **These are defaults, not decisions:** specs.md's open checklist
item — validate intelligibility with native speakers, not a demo — still stands.

Two deliberate limits:

- **Compose caps replies at `MAX_WORDS` (75)** and keeps whole sentences (no “…”)
  so they speak cleanly. TTS has a second cap of `MAX_CHARS` (900), truncated at a
  sentence boundary. The text alongside always carries the whole answer.
- **The transient "🔎 Checking your message…" acknowledgement is not spoken.** It is
  superseded within seconds by the real answer, so a voice note for it lands as clutter
  just as the answer arrives.

**Failure is always text-only, never silent.** The text is sent first, and synthesis,
upload, and send are each best-effort after that — losing the voice note degrades a
reply, but losing the reply fails the worker.

### Employment contracts

[`pipeline/contract.py`](./pipeline/contract.py) implements specs.md §2 "Contract
parsing": a worker sends their employment contract — as a PDF, or as photos of the
pages — and then asks questions about it in their own language.

Two calls, split deliberately. `read_contract()` runs once when the document arrives;
`answer_question()` runs per question against the extracted text. That means the PDF is
uploaded once and every follow-up costs one text-only call, which is what makes it a
conversation rather than a one-shot.

```bash
python -m pipeline.contract contract.pdf
python -m pipeline.contract contract.pdf --ask "how much can they deduct?" --language ta

curl -X POST localhost:8000/contract -F "file=@contract.pdf"
curl -X POST localhost:8000/contract/ask -H "Content-Type: application/json" \
  -d '{"contract_text":"...","question":"what is my notice period?","target_language":"bn"}'
```

**The contract never touches disk.** The pipeline is stateless — it returns the text,
and the Node layer holds it in the in-memory session for the life of the process. It is
never written to disk or Postgres and dies with the process. That is what lets
policy.md §11 ("no personal data retention") stand unamended: an employment contract
carries the worker's name, passport number, employer, and salary, and is the most
sensitive thing this bot handles. The cost is that a restart forgets it and the worker
re-uploads.

**Two boundaries the code enforces, both deliberate:**

- **Answers are grounded in the document only.** Asked something the contract doesn't
  cover, it returns `answerable: false` and says so rather than filling the gap. A
  plausible invention about someone's pay is worse than "your contract doesn't say",
  because they have no way to check it. Answerable questions come back with a verbatim
  `quote` of the clause, so the answer can be held against the page.
- **It will not say whether a term is legal.** That would mix the worker's personal
  document into the MOM corpus path. Legality questions set `needs_legal_check` and
  the bot points to the MOM helpline instead of guessing. Policy fact-checks of
  forwarded claims still run on `/process`; they are a separate conversation.

The read gate is stricter than the image gate — `MIN_CONFIDENCE` 0.7 vs 0.6 — because a
misread salary figure gets quoted back as fact to someone who can't verify it.

WhatsApp photos go to `handleImage`; PDFs sent as files go to `handleDocument`. Both
can enter contract mode when the reader marks the file as a usable employment
document.

## Setup

1. Install Node dependencies:

   ```bash
   nvm use 22
   npm install
   ```

2. Confirm `.env` has:

   - `number_ID` — WhatsApp phone number ID
   - `access_token` — Meta access token
   - `VERIFY_TOKEN` — any string; must match what you enter in the Meta dashboard
   - `PORT` — local bot port (default 3000)
   - `PIPELINE_URL` — Python service (default `http://127.0.0.1:8000`)
   - `CLAUDE_API_KEY` — Anthropic key used by the pipeline
   - `POSTGRES_*` or `DATABASE_URL` — corpus database

3. Start the bot (needs the pipeline running too — next section):

   ```bash
   npm start
   # or, auto-reload on file changes:
   npm run dev
   ```

## Run the bot + pipeline together

The WhatsApp bot (Node) and the verification pipeline (Python) run as two
processes. The bot POSTs each forwarded message to the pipeline service and
replies with the composed verdict (or the contract answer).

```bash
# 1. Postgres (corpus)
docker compose up -d db

# 2. Pipeline service (Python) — the bot calls this
source .venv/bin/activate
uvicorn app.webhook:app --host 127.0.0.1 --port 8000

# 3. WhatsApp bot (Node), in another terminal
npm start

# 4. Public HTTPS tunnel for Meta (another terminal)
npx ngrok http 3000
```

Flow: a user messages the bot → picks a language → sends a voice note, image, or
text. Voice goes through Whisper ASR first; then every *policy* path runs translate
→ route → claim extraction → Postgres retrieve → LLM true/false. Scam-looking
messages get a warning; messages with no checkable claim get the MOM hotline
template. Contract-like questions prompt for the file, then Q&A stays on that
document. The bot talks to the pipeline at `PIPELINE_URL`.

> Note: outbound replies require a valid WhatsApp `access_token` in `.env`.
> A `401 Authentication Error` (Meta code 190) in the logs means the token has
> expired or cannot be decrypted — paste a fresh token from the Meta dashboard
> and restart `npm start`. Port 8000 already in use: stop the old uvicorn
> process before starting another.

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

Typical contract-mode check:

1. After choosing a language, send *“What is my salary, according to my contract?”*
   — the bot should ask you to send the contract, not fact-check MOM policy.
2. Send the PDF (or page photos).
3. You should get an answer from the document, plus “I'll keep this contract…”.
4. Ask a follow-up (*“What does the passport section say?”*) — same held text,
   no re-upload.
5. Send `done` to leave contract mode.

### Automated tests

The suite exists so the text and voice features can be verified **without** access to
the Meta webhook. Everything except the two calls to Meta's servers is covered.

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest                # unit: no network, no model weights, ~0.5s
npm test              # Node: routing + handlers, fetch stubbed in-process
pytest --live         # real Claude API + real Whisper, ~2 min, costs tokens
```

`pytest` and `npm test` are free and fast — run them on every change. `pytest --live`
is the acceptance check: it answers "does translation actually work", using real API
calls and real audio.

If `npm test` hangs, a leftover Node listener on the ephemeral test port is usually
the cause. Kill it and rerun (`npm test` already passes `--test-force-exit`).

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
- `tests/test_contract_unit.py` — the contract usability gate, PDF vs image blocks, prompt rules
- `tests/test_tts_unit.py` — voice selection, emoji/layout stripping, the length cap
- `tests/test_compose_unit.py` — 75-word budget and complete-sentence fitting
- `tests/test_retrieve_unit.py` — FTS query gen and source-family diversification
- `tests/test_router_unit.py` — policy vs scam vs neither
- `tests/routing.test.js` — boots the real Express app: onboarding, contract prompt/hold, `done`
- `tests/contractQuestion.test.js` — which phrases are contract questions vs policy
- `tests/whatsapp.test.js` — media upload and the text-plus-voice reply, incl. degradation
- `tests/test_translate_live.py` — real translation: languages, detail preservation, prompt injection
- `tests/test_voice_live.py` — real audio → Whisper → Claude, through the FastAPI route
- `tests/test_vision_live.py` — real images → Claude vision → Claude, through the FastAPI route
- `tests/test_contract_live.py` — real multi-page PDF → Q&A; abstention and the legal boundary
- `tests/test_tts_live.py` — real synthesis in all six languages, decoded to verify the codec
- `tests/media.test.js` — inbound message classification against real Meta payload shapes
- `tests/handlers.test.js` — handlers + pipeline client + media download, with `fetch` stubbed

## Files

- `src/index.js` — Express server + webhook, per-sender session, language gate, contract mode
- `src/media.js` — classifies an inbound message: text / image / voice / document / control / unsupported
- `src/handlers.js` — one handler per kind; text, voice, and image converge on `verifyText()`
- `src/contractQuestion.js` — heuristic: personal contract Q vs MOM policy claim
- `src/whatsapp.js` — WhatsApp Cloud API send helpers (text, list menu, voice note)
- `src/pipeline.js` — HTTP client for the Python pipeline + reply formatting
- `src/languages.js` — six language options and UI strings (welcome, checking, send-contract)
- `pipeline/asr.py` — faster-whisper speech to text + confidence (policy.md §1 stage 1)
- `pipeline/vision.py` — read text out of an image + confidence (stage 1, image path)
- `pipeline/llm.py` — Claude client, schema-enforced structured output
- `pipeline/translate.py` · `router.py` · `claims.py` · `retrieve.py` · `verify.py` · `scam.py` — stages 2–9 + scam stub
- `pipeline/compose.py` — stage 10: worker-facing reply + localisation
- `pipeline/contract.py` — read an employment contract + grounded Q&A (specs.md §2)
- `pipeline/tts.py` — speak a reply as an OGG/Opus voice note (specs.md §2)
- `pipeline/pipeline.py` — orchestrator (message → verified claims)
- `app/webhook.py` — FastAPI service (`/transcribe`, `/extract`, `/translate`, `/process`, `/speak`, `/contract`, `/contract/ask`)
- `app/api.py` — re-exports `app.webhook:app` for compatibility
- `db/schema.sql` — Postgres corpus schema (documents + chunks)
- `db/connection.py` — psycopg connection helpers (config from `.env`)
- `db/init_db.py` — apply / inspect the schema (`python -m db.init_db`)
- `docker-compose.yml` — Postgres 16 + `pg_trgm`
- `ingest/sources.yaml` — curated official-source list with authority tiers
- `ingest/fetch.py` — fetch → chunk → upsert into Postgres
- `ingest/chunker.py` — heading-aware, list-safe chunking
- `tests/` — see [Automated tests](#automated-tests); `conftest.py` synthesises voice fixtures
