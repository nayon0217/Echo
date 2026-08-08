import "dotenv/config";
import express from "express";
import { sendLanguageMenu, sendText } from "./whatsapp.js";
import {
  LANGUAGE_BY_ID,
  WELCOME_AFTER_LANGUAGE,
  CHECKING_MESSAGE,
  uiString,
} from "./languages.js";
import { isHealthy, PIPELINE_URL } from "./pipeline.js";
import { KIND, classify } from "./media.js";
import { handleText, handleImage, handleVoice, handleUnsupported } from "./handlers.js";

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const VERIFY_TOKEN = process.env.VERIFY_TOKEN || "echo_verify_token";

// Per-sender state, keyed by phone number. In memory on purpose — policy.md §11.
const sessions = new Map();

const seenMessageIds = new Set();
const MAX_SEEN_IDS = 1000;

function alreadyHandled(id) {
  if (!id) return false;
  if (seenMessageIds.has(id)) return true;
  seenMessageIds.add(id);
  if (seenMessageIds.size > MAX_SEEN_IDS) {
    seenMessageIds.delete(seenMessageIds.values().next().value);
  }
  return false;
}

function sessionFor(from) {
  let s = sessions.get(from);
  if (!s) {
    s = { language: null, lastMessage: null };
    sessions.set(from, s);
  }
  return s;
}

app.get("/", (_req, res) => res.send("ECHO WhatsApp bot is running."));

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

app.post("/webhook", async (req, res) => {
  res.sendStatus(200);

  try {
    const value = req.body?.entry?.[0]?.changes?.[0]?.value;
    const messages = value?.messages;
    if (!Array.isArray(messages) || messages.length === 0) {
      return;
    }

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

async function handleMessage(message) {
  const from = message.from;
  const session = sessionFor(from);
  session.lastMessage = message;

  const c = classify(message);
  console.log(`[route] ${from} (${c.type}) -> ${c.kind}`);

  if (c.kind === KIND.UNSUPPORTED) {
    await handleUnsupported(c);
    return;
  }

  if (c.kind === KIND.CONTROL) {
    await handleLanguageChoice(from, session, c);
    return;
  }

  // Ask for reply language first. Do not hold/auto-verify the current message —
  // verification starts on the *next* message after they choose.
  if (!session.language) {
    console.log(`[route] ${from} has no reply language yet -> menu`);
    await sendLanguageMenu(from);
    return;
  }

  await runAndReply(from, c, session);
}

/** Record the chosen reply language. Do not start verification yet. */
async function handleLanguageChoice(from, session, c) {
  const chosen = LANGUAGE_BY_ID[c.replyId];
  if (!chosen) {
    await sendLanguageMenu(from);
    return;
  }

  session.language = chosen;
  console.log(`[route] ${from} chose ${chosen.title}`);

  await sendText(from, uiString(WELCOME_AFTER_LANGUAGE, chosen.code));
}

async function runAndReply(from, c, session) {
  const code = session?.language?.code || "en";
  await sendText(from, uiString(CHECKING_MESSAGE, code));

  const result = await dispatch(c, session);

  if (result.reply) {
    await sendText(from, result.reply);
  }
}

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
