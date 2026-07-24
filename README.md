# ECHO — WhatsApp Bot

Voice-first AI verification bot for migrant workers in Singapore. See [`specs.md`](./specs.md) for the full project vision.

**Current step:** a user messages the bot (e.g. "Hi Echo!") and gets back an interactive menu to pick one of four languages (English, Bengali, Tamil, Mandarin).

## Stack

- Node.js 22 + Express
- WhatsApp Cloud API (Meta-hosted)

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
