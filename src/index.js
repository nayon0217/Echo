import "dotenv/config";
import express from "express";
import { sendLanguageMenu, sendText } from "./whatsapp.js";
import { LANGUAGE_BY_ID } from "./languages.js";
import { isHealthy, PIPELINE_URL } from "./pipeline.js";
import { KIND, classify } from "./media.js";
import { handleText, handleImage, handleVoice, handleUnsupported } from "./handlers.js";

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const VERIFY_TOKEN = process.env.VERIFY_TOKEN || "echo_verify_token";

// Per-sender state, keyed by phone number: the language they picked and the last
// message we saw from them. In memory on purpose — policy.md §11 commits to no
// personal data retention, so this dies with the process and is never written to
// Postgres.
const sessions = new Map();

// Meta retries webhooks and can redeliver, so the same message id may arrive more
// than once. Bounded so it can't grow without limit.
const seenMessageIds = new Set();
const MAX_SEEN_IDS = 1000;

function alreadyHandled(id) {
  if (!id) return false;
  if (seenMessageIds.has(id)) return true;
  seenMessageIds.add(id);
  if (seenMessageIds.size > MAX_SEEN_IDS) {
    // Drop the oldest insertion; Set preserves insertion order.
    seenMessageIds.delete(seenMessageIds.values().next().value);
  }
  return false;
}

function sessionFor(from) {
  let s = sessions.get(from);
  if (!s) {
    // `pending` holds a message that arrived before the sender picked a reply
    // language, so their first forward isn't dropped while we ask.
    s = { language: null, lastMessage: null, pending: null };
    sessions.set(from, s);
  }
  return s;
}

app.get("/", (_req, res) => res.send("ECHO WhatsApp bot is running."));

// Webhook verification handshake — Meta calls this once when you save the webhook URL.
app.get("/webhook", (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  const challenge = req.query["hub.challenge"];

  if (mode === "subscribe" && token === VERIFY_TOKEN) {
    console.log("[webhook] verified");
    return res.status(200).send(challenge);
  }
  return res.sendStatus(403);
});

// Incoming messages and status updates.
app.post("/webhook", async (req, res) => {
  // Always ack fast so Meta doesn't retry.
  res.sendStatus(200);

  try {
    const value = req.body?.entry?.[0]?.changes?.[0]?.value;
    const messages = value?.messages;
    if (!Array.isArray(messages) || messages.length === 0) {
      return; // status callback (delivered/read), not a user message
    }

    // One webhook delivery can carry several messages; process oldest first so a
    // burst of forwards is handled in the order the worker sent them.
    const ordered = [...messages].sort(
      (a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0),
    );

    for (const message of ordered) {
      if (alreadyHandled(message.id)) {
        console.log(`[webhook] duplicate ${message.id}, skipping`);
        continue;
      }
      await handleMessage(message);
    }
  } catch (err) {
    console.error("[webhook] handler error:", err);
  }
});

/** Classify one message and dispatch it to the handler for its kind. */
async function handleMessage(message) {
  const from = message.from;
  const session = sessionFor(from);
  session.lastMessage = message;

  const c = classify(message);
  console.log(`[route] ${from} (${c.type}) -> ${c.kind}`);

  // Ignored media stays ignored whether or not onboarding has happened — we never
  // want a forwarded video to trigger a language prompt.
  if (c.kind === KIND.UNSUPPORTED) {
    await handleUnsupported(c);
    return;
  }

  // Menu taps and button presses drive the conversation rather than carrying
  // content, so they're handled here instead of in a media handler.
  if (c.kind === KIND.CONTROL) {
    await handleLanguageChoice(from, session, c);
    return;
  }

  // Content, but we don't yet know which language to answer in. The detected
  // language of a forwarded message is not a safe substitute: scams targeting
  // migrant workers are often written in English, so answering in the message's
  // own language would reply in English to someone who can't read it. Ask, and
  // hold the message so they don't have to send it twice.
  if (!session.language) {
    session.pending = c;
    console.log(`[route] ${from} has no reply language yet -> menu (holding ${c.kind})`);
    await sendLanguageMenu(from);
    return;
  }

  await runAndReply(from, c, session);
}

/** Record the chosen reply language, then pick up whatever was waiting on it. */
async function handleLanguageChoice(from, session, c) {
  const chosen = LANGUAGE_BY_ID[c.replyId];
  if (!chosen) {
    await sendLanguageMenu(from);
    return;
  }

  session.language = chosen;
  console.log(`[route] ${from} chose ${chosen.title}`);

  const pending = session.pending;
  session.pending = null;

  await sendText(
    from,
    pending
      ? `Great — I'll reply in ${chosen.title}. Let me check that message now.`
      : `Great — I'll reply in ${chosen.title}. Send me a voice note, image, or message to check.`,
  );

  if (pending) {
    await runAndReply(from, pending, session);
  }
}

/** Dispatch a classified message and send whatever the handler produced. */
async function runAndReply(from, c, session) {
  const result = await dispatch(c, session);

  // A null reply means deliberate silence.
  if (result.reply) {
    await sendText(from, result.reply);
  }
}

/** @returns {Promise<import("./handlers.js").Result>} */
function dispatch(c, session) {
  switch (c.kind) {
    case KIND.TEXT:
      return handleText(c, session);
    case KIND.IMAGE:
      return handleImage(c, session);
    case KIND.VOICE:
      return handleVoice(c, session);
    default:
      return handleUnsupported(c, session);
  }
}

app.listen(PORT, async () => {
  console.log(`ECHO bot listening on http://localhost:${PORT}`);
  if (await isHealthy()) {
    console.log(`[pipeline] connected at ${PIPELINE_URL}`);
  } else {
    console.warn(
      `[pipeline] NOT reachable at ${PIPELINE_URL} — start it with:\n` +
        `           uvicorn app.webhook:app --reload --port 8000`,
    );
  }
});
