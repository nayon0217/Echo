import "dotenv/config";
import express from "express";
import { sendLanguageMenu, sendText } from "./whatsapp.js";
import { LANGUAGE_BY_ID } from "./languages.js";

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const VERIFY_TOKEN = process.env.VERIFY_TOKEN || "echo_verify_token";

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
    const message = value?.messages?.[0];
    if (!message) return; // status callback (delivered/read), not a user message

    const from = message.from;

    // User tapped a language in the list menu.
    if (message.type === "interactive") {
      const reply = message.interactive?.list_reply;
      const chosen = reply && LANGUAGE_BY_ID[reply.id];
      if (chosen) {
        console.log(`[webhook] ${from} chose ${chosen.title}`);
        await sendText(
          from,
          `Great — I'll reply in ${chosen.title}. Send me a voice note, image, or message to check.`,
        );
        return;
      }
    }

    // Any other inbound message (e.g. "Hi Echo!") opens the language menu.
    console.log(`[webhook] message from ${from} (${message.type}) -> sending language menu`);
    await sendLanguageMenu(from);
  } catch (err) {
    console.error("[webhook] handler error:", err);
  }
});

app.listen(PORT, () => {
  console.log(`ECHO bot listening on http://localhost:${PORT}`);
});
