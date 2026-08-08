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
import {
  handleText,
  handleImage,
  handleVoice,
  handleDocument,
  handleContractQuestion,
  handleUnsupported,
} from "./handlers.js";

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
    // `contract` holds the text of an employment contract the sender uploaded, and is
    // the single most sensitive thing this process keeps. It lives here and nowhere
    // else — never on disk, never in Postgres — and dies with the process, which is
    // what lets policy.md §11 ("no personal data retention") stand unamended.
    s = { language: null, lastMessage: null, contract: null };
    sessions.set(from, s);
  }
  return s;
}

// Typed by the worker to leave contract mode. Kept short and matched loosely because
// they may be typing in a second language on a phone keyboard.
const EXIT_CONTRACT_WORDS = new Set([
  "done",
  "exit",
  "stop",
  "back",
  "finish",
  "finished",
  "cancel",
]);

function wantsToLeaveContractMode(text) {
  if (!text) return false;
  const cleaned = text.trim().toLowerCase().replace(/[.!?]+$/, "");
  return EXIT_CONTRACT_WORDS.has(cleaned);
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

  // Holding a contract puts the sender in contract mode: their typed messages are
  // questions about that document, not new claims to verify. Deterministic — no
  // per-message classifier call, and they leave with one word.
  //
  // Only TEXT is captured. A forwarded voice note or image is still content to check,
  // so those fall through to the normal path and the contract stays held.
  if (session.contract && c.kind === KIND.TEXT) {
    if (wantsToLeaveContractMode(c.text)) {
      session.contract = null;
      console.log(`[route] ${from} left contract mode`);
      await sendText(
        from,
        "Okay — I've forgotten your contract. Send me a message, voice note, or image " +
          "to check, or send the contract again any time.",
      );
      return;
    }

    console.log(`[route] ${from} -> contract question`);
    const result = await handleContractQuestion(c.text, session);
    if (result.reply) await sendText(from, result.reply);
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

  // A document handler that read a contract successfully hands it back here to be
  // held. This is the only place a contract enters the session.
  if (result.contract) {
    session.contract = result.contract;
    console.log(`[route] ${from} entered contract mode`);
  }

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
    case KIND.DOCUMENT:
      return handleDocument(c, session);
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
