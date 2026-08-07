import "dotenv/config";

<<<<<<< HEAD
// The Python verification pipeline (app/webhook.py). Node owns the Meta webhook
// and acks it fast; anything needing Claude or Postgres goes through here.
const PIPELINE_URL = process.env.PIPELINE_URL || "http://127.0.0.1:8000";

// Claude calls take a couple of seconds; cap the wait so a hung pipeline doesn't
// hold the handler open indefinitely.
const TIMEOUT_MS = Number(process.env.PIPELINE_TIMEOUT_MS) || 20000;

/**
 * Detect a message's language and translate it to English.
 *
 * Returns the parsed pipeline response, or null if the pipeline is unreachable,
 * timed out, or rejected the text — callers degrade rather than throw, since a
 * worker is waiting on the other end.
 */
export async function translate(text) {
  try {
    const res = await fetch(`${PIPELINE_URL}/translate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });

    if (!res.ok) {
      // Don't log the body — it echoes message content (policy.md §11).
      console.error(`[pipeline] /translate failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`[pipeline] /translate unreachable: ${err.name}`);
    return null;
  }
}

// Transcription is far slower than translation — a long voice note through
// large-v3 on CPU can take a while — so it gets its own, much larger budget.
const TRANSCRIBE_TIMEOUT_MS = Number(process.env.PIPELINE_TRANSCRIBE_TIMEOUT_MS) || 180000;

// Extension hints Whisper's decoder; WhatsApp voice notes are OGG/Opus.
const EXT_BY_MIME = {
  "audio/ogg": ".ogg",
  "audio/opus": ".ogg",
  "audio/mpeg": ".mp3",
  "audio/mp4": ".m4a",
  "audio/aac": ".aac",
  "audio/amr": ".amr",
  "audio/wav": ".wav",
};

function filenameFor(mimeType) {
  const base = (mimeType || "").split(";")[0].trim();
  return `voice${EXT_BY_MIME[base] || ".ogg"}`;
}

/**
 * Transcribe a voice note and translate it into `targetLanguage`.
 *
 * @param {Buffer} buffer   the audio bytes
 * @param {string} mimeType e.g. "audio/ogg; codecs=opus"
 * @param {string} targetLanguage ISO 639-1 reply language
 * @returns {Promise<object|null>} the pipeline response, or null on failure
 */
export async function transcribe(buffer, mimeType, targetLanguage) {
  try {
    const form = new FormData();
    form.append("file", new Blob([buffer], { type: mimeType }), filenameFor(mimeType));
    form.append("target_language", targetLanguage);

    const res = await fetch(`${PIPELINE_URL}/transcribe`, {
      method: "POST",
      body: form, // no Content-Type header — fetch sets the multipart boundary
      signal: AbortSignal.timeout(TRANSCRIBE_TIMEOUT_MS),
    });

    if (!res.ok) {
      console.error(`[pipeline] /transcribe failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`[pipeline] /transcribe unreachable: ${err.name}`);
    return null;
  }
}

// Reading an image is one Claude call plus a translation, so it sits between the
// translate and transcribe budgets.
const EXTRACT_TIMEOUT_MS = Number(process.env.PIPELINE_EXTRACT_TIMEOUT_MS) || 60000;

const IMAGE_EXT_BY_MIME = {
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "image/gif": ".gif",
};

function imageFilenameFor(mimeType) {
  const base = (mimeType || "").split(";")[0].trim();
  return `image${IMAGE_EXT_BY_MIME[base] || ".jpg"}`;
}

/**
 * Read the text out of an image and translate it into `targetLanguage`.
 *
 * @param {Buffer} buffer   the image bytes
 * @param {string} mimeType e.g. "image/jpeg"
 * @param {string} targetLanguage ISO 639-1 reply language
 * @returns {Promise<object|null>} the pipeline response, or null on failure
 */
export async function extractImage(buffer, mimeType, targetLanguage) {
  try {
    const form = new FormData();
    // The pipeline reads the media type off the upload, so send the real one rather
    // than letting it default — a PNG announced as JPEG is rejected upstream.
    const type = (mimeType || "").split(";")[0].trim() || "image/jpeg";
    form.append("file", new Blob([buffer], { type }), imageFilenameFor(type));
    form.append("target_language", targetLanguage);

    const res = await fetch(`${PIPELINE_URL}/extract`, {
      method: "POST",
      body: form, // no Content-Type header — fetch sets the multipart boundary
      signal: AbortSignal.timeout(EXTRACT_TIMEOUT_MS),
    });

    if (!res.ok) {
      console.error(`[pipeline] /extract failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`[pipeline] /extract unreachable: ${err.name}`);
    return null;
  }
}

/** True if the pipeline is up. Used at boot to warn early rather than at first message. */
export async function isHealthy() {
  try {
    const res = await fetch(`${PIPELINE_URL}/health`, {
      signal: AbortSignal.timeout(3000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export { PIPELINE_URL };
=======
// The Python verification pipeline runs as a local FastAPI service (app/api.py).
// The bot POSTs forwarded messages here and formats the result for WhatsApp.
const PIPELINE_URL = process.env.PIPELINE_URL || "http://127.0.0.1:8000";

// Calls the pipeline's /process endpoint. Returns the parsed result, or throws.
export async function processMessage(text, language) {
  const res = await fetch(`${PIPELINE_URL}/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, language }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`pipeline ${res.status}: ${detail}`);
  }
  return res.json();
}

const MOM_HOTLINE = "the MOM hotline (6438 5122)";
const SCAM_CONTACT = "the MOM hotline (6438 5122) or ScamShield (1799)";

// Reframes a claim as the object of "There is no evidence that …".
// Casing is left unchanged: claims often start with proper nouns ("Work Permit",
// "MOM") that read wrong when lowercased, and a capital after "that" is fine.
function reframeClaim(text) {
  return (text || "").trim().replace(/[.?!]+$/, "");
}

// Synthetic-media detection templates. Not yet produced by the pipeline; fires
// when a `result.ai_detection` field is present ("ai_generated" | "likely_ai" | "not_ai").
function formatAiDetection(status) {
  switch (status) {
    case "ai_generated":
      return (
        "⚠️ This appears to be AI-generated.\n" +
        "We found signs like an artificial voice/image, and unusual details or editing patterns. " +
        "AI can create convincing fake content, so don't trust it without checking."
      );
    case "likely_ai":
      return (
        "⚠️ This is probably AI-generated.\n" +
        "We found several signs of AI, but we are not completely certain. " +
        "Please be careful and check before sharing or acting on it."
      );
    case "not_ai":
      return (
        "✅ This does not appear to be AI-generated.\n" +
        "That doesn't mean the information is true. Real photos and voice recordings can still contain false information. " +
        "If you'd like, we can check whether the claim itself is true."
      );
    default:
      return null;
  }
}

// Formats a single verified claim using the reply templates.
function formatClaim(c) {
  const cited = (c.cited_sources ?? [])[0];
  const url = cited?.source_url;
  const sourceName = cited?.source_name || "MOM";

  if (c.verdict === "supported") {
    let msg = `✅ This claim is true.\nIt matches information from ${sourceName}.`;
    if (url) msg += ` You can read the official announcement here: ${url}`;
    return msg;
  }

  if (c.verdict === "refuted") {
    let msg = `❌ The claim is false.\nThere is no evidence that ${reframeClaim(c.text)}.`;
    if (url) msg += ` You can read more here: ${url}`;
    return msg;
  }

  // insufficient
  return (
    "🤔 I couldn't confirm this claim.\n" +
    `There isn't enough official information to say if it's true or false. To be safe, contact ${MOM_HOTLINE}.`
  );
}

// Builds the WhatsApp reply text from a pipeline result.
export function formatReply(result) {
  const parts = [];

  // Synthetic-media verdict first, if available.
  const aiMsg = formatAiDetection(result?.ai_detection);
  if (aiMsg) parts.push(aiMsg);

  // Scam warning.
  if (result?.scam?.is_scam_suspected) {
    parts.push(
      `⚠️ This is a scam.\nDo not send money or click any links. If you're unsure, contact ${SCAM_CONTACT}.`,
    );
  }

  const claims = result?.claims ?? [];
  if (claims.length === 1) {
    parts.push(formatClaim(claims[0]));
  } else if (claims.length > 1) {
    parts.push(claims.map((c, i) => `${i + 1}. ${c.text}\n${formatClaim(c)}`).join("\n\n"));
  }

  // Nothing checkable and no scam/AI signal.
  if (parts.length === 0) {
    if (result?.notice) return result.notice;
    return "I couldn't find anything to check in that message. If you're unsure, contact the MOM hotline (6438 5122).";
  }

  return parts.join("\n\n");
}
>>>>>>> 2d5c287 (LLM layer added)
