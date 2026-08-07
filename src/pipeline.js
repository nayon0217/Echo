import "dotenv/config";

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
