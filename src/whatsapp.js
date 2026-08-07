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

/**
 * Download media (voice note, image) by its id.
 *
 * Inbound messages carry a media *id*, not the bytes, so this is two authenticated
 * calls: resolve the id to a short-lived URL, then fetch that URL. The second call
 * needs the bearer token too — Meta's media CDN returns 401 without it, which is the
 * usual reason this appears to "just not work".
 *
 * @returns {Promise<{buffer: Buffer, mimeType: string}|null>} null on any failure
 */
export async function downloadMedia(mediaId) {
  if (!mediaId) return null;

  try {
    const metaRes = await fetch(`https://graph.facebook.com/${GRAPH_VERSION}/${mediaId}`, {
      headers: { Authorization: `Bearer ${ACCESS_TOKEN}` },
      signal: AbortSignal.timeout(15000),
    });
    if (!metaRes.ok) {
      console.error(`[whatsapp] media lookup failed: ${metaRes.status}`);
      return null;
    }

    const { url, mime_type: mimeType } = await metaRes.json();
    if (!url) {
      console.error("[whatsapp] media lookup returned no url");
      return null;
    }

    const fileRes = await fetch(url, {
      headers: { Authorization: `Bearer ${ACCESS_TOKEN}` },
      signal: AbortSignal.timeout(30000),
    });
    if (!fileRes.ok) {
      console.error(`[whatsapp] media download failed: ${fileRes.status}`);
      return null;
    }

    const buffer = Buffer.from(await fileRes.arrayBuffer());
    console.log(`[whatsapp] downloaded ${buffer.length} bytes (${mimeType})`);
    return { buffer, mimeType: mimeType || "application/octet-stream" };
  } catch (err) {
    console.error(`[whatsapp] media download error: ${err.name}`);
    return null;
  }
}
