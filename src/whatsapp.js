import "dotenv/config";
import { LANGUAGES } from "./languages.js";

const GRAPH_VERSION = process.env.GRAPH_API_VERSION || "v21.0";

// The .env file uses keys: number, number_ID, business_ID, access_token.
const PHONE_NUMBER_ID = process.env.number_ID;
const ACCESS_TOKEN = process.env.access_token;

if (!PHONE_NUMBER_ID || !ACCESS_TOKEN) {
  console.warn(
    "[whatsapp] Missing number_ID or access_token in .env — outbound messages will fail.",
  );
}

const API_BASE = `https://graph.facebook.com/${GRAPH_VERSION}/${PHONE_NUMBER_ID}/messages`;

async function send(payload) {
  const res = await fetch(API_BASE, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ messaging_product: "whatsapp", ...payload }),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    console.error("[whatsapp] send failed:", res.status, JSON.stringify(data));
  }
  return { ok: res.ok, status: res.status, data };
}

// Sends the "choose your language" list message that opens every conversation.
export function sendLanguageMenu(to) {
  return send({
    to,
    type: "interactive",
    interactive: {
      type: "list",
      header: { type: "text", text: "ECHO" },
      body: {
        text: "Hi! I'm ECHO. I can help you check suspicious voice notes, images, and job offers.\n\nWhich language should I reply in?",
      },
      footer: { text: "Tap the button below to choose" },
      action: {
        button: "Choose language",
        sections: [
          {
            title: "Languages",
            rows: LANGUAGES.map((lang) => ({
              id: lang.id,
              title: lang.title,
              description: lang.description,
            })),
          },
        ],
      },
    },
  });
}

export function sendText(to, body) {
  return send({ to, type: "text", text: { body } });
}
