# ECHO

Voice-first WhatsApp bot for migrant workers in Singapore. A worker picks a
language, then forwards a message, voice note, or image; ECHO checks claims
against official sources and replies in that language as **text plus a voice
note**. They can also send an employment contract and ask questions about it.

Vision and pipeline design: [`specs.md`](./specs.md), [`policy.md`](./policy.md).

## Stack

| Layer | Tech |
|---|---|
| WhatsApp | Node 22, Express (`src/`), Meta Cloud API |
| Pipeline | Python 3.10+, FastAPI (`app/webhook.py`), Claude |
| Speech | faster-whisper (ASR), edge-tts (OGG/Opus voice notes) |
| Corpus | Postgres 16 + `pg_trgm`, curated in [`ingest/sources.yaml`](./ingest/sources.yaml) |

Two processes: the Node bot on `:3000` and the Python service on `:8000`
(`PIPELINE_URL`).

## Setup

**1. Node**

```bash
nvm use 22
npm install
```

**2. Python**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. `.env`**

| Variable | Purpose |
|---|---|
| `number_ID`, `access_token` | WhatsApp Cloud API |
| `VERIFY_TOKEN` | Must match the Meta webhook verify token |
| `PORT` | Bot port (default `3000`) |
| `PIPELINE_URL` | Pipeline (default `http://127.0.0.1:8000`) |
| `CLAUDE_API_KEY`, `CLAUDE_MODEL` | Anthropic |
| `POSTGRES_*` or `DATABASE_URL` | Corpus DB (defaults `echo` / `echo` / `echo`) |

Optional: `WHISPER_MODEL` (`large-v3` by default, ~3 GB; use `base` while iterating).

**4. Run (four terminals)**

```bash
docker compose up -d db

source .venv/bin/activate
python -m ingest.fetch          # once, after a fresh DB
uvicorn app.webhook:app --host 127.0.0.1 --port 8000

npm start                       # or npm run dev

npx ngrok http 3000
```

In **Meta App Dashboard → WhatsApp → Configuration → Webhook**:

- Callback URL: `https://<ngrok-subdomain>.ngrok-free.app/webhook`
- Verify token: same as `VERIFY_TOKEN`
- Subscribe to **`messages`**

Send a message from a registered test number. You should get the language list
(English, Bahasa Indonesia, Burmese, Bengali, Tamil, Mandarin).

Schema re-apply / inspect: `python -m db.init_db` / `--check`. Wipe the volume
with `docker compose down -v`.

If logs show Meta `401` / code `190`, paste a fresh `access_token` and restart
`npm start`. If port 8000 is in use, stop the old uvicorn process.

## Test

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest          # Python unit tests, no network
npm test        # Node routing + handlers, Meta stubbed
pytest --live   # real Claude + Whisper (costs tokens)
```

`WHISPER_MODEL=large-v3 pytest --live` for number-accurate non-English audio.
