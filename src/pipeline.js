import "dotenv/config";

// The Python verification pipeline (app/webhook.py). Node owns the Meta webhook
// and acks it fast; anything needing Whisper, Claude, or Postgres goes through here.
const PIPELINE_URL = process.env.PIPELINE_URL || "http://127.0.0.1:8000";

// Claude calls take a couple of seconds; cap the wait so a hung pipeline doesn't
// hold the handler open indefinitely.
const TIMEOUT_MS = Number(process.env.PIPELINE_TIMEOUT_MS) || 20000;

// Full claim verification (route + extract + retrieve + LLM T/F) can take longer.
const PROCESS_TIMEOUT_MS = Number(process.env.PIPELINE_PROCESS_TIMEOUT_MS) || 120000;

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

/**
 * Run claim extraction + DB retrieval + LLM true/false verification.
 *
 * @param {string} text  original or English pivot text
 * @param {string|null} language  worker's reply language code
 * @param {{ text_en?: string, source_language?: string }} [opts]
 * @returns {Promise<object>}
 */
export async function processMessage(text, language, opts = {}) {
  const body = {
    text,
    language: language || null,
    with_verify: true,
  };
  if (opts.text_en) body.text_en = opts.text_en;
  if (opts.source_language) body.source_language = opts.source_language;
  if (opts.mediaKind) body.media_kind = opts.mediaKind;

  const res = await fetch(`${PIPELINE_URL}/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(PROCESS_TIMEOUT_MS),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`pipeline ${res.status}: ${detail}`);
  }
  return res.json();
}

const MOM_HOTLINE = "the MOM hotline (6438 5122)";
const SCAM_CONTACT = "the MOM hotline (6438 5122) or ScamShield (1799)";

function reframeClaim(text) {
  return (text || "").trim().replace(/[.?!]+$/, "");
}

function formatAiDetection(status) {
  switch (status) {
    case "ai_generated":
      return "⚠️ This may be AI-made. Do not trust it without checking.";
    case "likely_ai":
      return "⚠️ This might be AI-made. Be careful before you share or act.";
    case "not_ai":
      return "✅ This does not look AI-made. The information can still be wrong.";
    default:
      return null;
  }
}

/** Fallback claim formatter when the Python compose stage did not return `reply`. */
function formatClaim(c, mediaKind = null) {
  const cited = (c.cited_sources ?? [])[0];
  const url = cited?.source_url;
  const sourceName = cited?.source_name || "MOM";

  if (c.verdict === "supported") {
    let msg = `✅ True.\nThis matches ${sourceName}.`;
    if (url) msg += `\nRead more: ${url}`;
    return msg;
  }

  if (c.verdict === "refuted") {
    const headline =
      mediaKind === "voice" ? "❌ This voice message is false." : "❌ False.";
    let msg = `${headline}\nNo clear evidence that ${reframeClaim(c.text)}.`;
    if (url) msg += `\nRead more: ${url}`;
    return msg;
  }

  return (
    "🤔 I can't confirm this.\n" +
    `There is not enough official information. To be safe, call ${MOM_HOTLINE}.`
  );
}

/**
 * Prefer stage-10 `reply` from Python; fall back to local templates.
 * @param {object} result
 * @param {{ mediaKind?: "voice"|"image"|"text" }} [opts]
 */
export function formatReply(result, opts = {}) {
  const mediaKind = opts.mediaKind || null;

  // Stage 10 compose (Python) already includes AI/scam text and localisation.
  if (result?.reply && typeof result.reply === "string" && result.reply.trim()) {
    return result.reply.trim();
  }

  const parts = [];
  const aiMsg = formatAiDetection(result?.ai_detection);
  if (aiMsg) parts.push(aiMsg);

  const claims = result?.claims ?? [];
  if (claims.length === 1) {
    parts.push(formatClaim(claims[0], mediaKind));
  } else if (claims.length > 1) {
    parts.push(
      claims
        .slice(0, 2)
        .map((c, i) => `${i + 1}. ${formatClaim(c, mediaKind)}`)
        .join("\n\n"),
    );
  }

  if (result?.scam?.is_scam_suspected) {
    parts.push(
      `⚠️ Possible scam.\nDo not send money or click links. If unsure, call ${SCAM_CONTACT}.`,
    );
  }

  if (parts.length === 0) {
    if (result?.notice) return result.notice;
    return `I could not find anything to check. If unsure, call ${MOM_HOTLINE}.`;
  }

  return parts.join("\n\n");
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

// Reading a multi-page contract at high effort is the slowest call in the pipeline.
const CONTRACT_TIMEOUT_MS = Number(process.env.PIPELINE_CONTRACT_TIMEOUT_MS) || 180000;

const DOC_EXT_BY_MIME = {
  "application/pdf": ".pdf",
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
};

/**
 * Transcribe an employment contract so we can hold it and answer questions about it.
 *
 * The extracted text comes back rather than being stored server-side: the pipeline is
 * stateless, and the contract lives only in this process's memory (policy.md §11).
 *
 * @param {Buffer} buffer   the document bytes
 * @param {string} mimeType "application/pdf", or an image type for a photographed page
 * @returns {Promise<object|null>} the pipeline response, or null on failure
 */
export async function readContract(buffer, mimeType) {
  try {
    const type = (mimeType || "").split(";")[0].trim() || "application/pdf";
    const form = new FormData();
    form.append("file", new Blob([buffer], { type }), `contract${DOC_EXT_BY_MIME[type] || ".pdf"}`);

    const res = await fetch(`${PIPELINE_URL}/contract`, {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(CONTRACT_TIMEOUT_MS),
    });

    if (!res.ok) {
      console.error(`[pipeline] /contract failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`[pipeline] /contract unreachable: ${err.name}`);
    return null;
  }
}

/**
 * Answer one question about a contract we're already holding.
 *
 * @param {string} contractText  text from a prior readContract() call
 * @param {string} question      the worker's question
 * @param {string} targetLanguage ISO 639-1 reply language
 * @returns {Promise<object|null>} the pipeline response, or null on failure
 */
export async function askContract(contractText, question, targetLanguage) {
  try {
    const res = await fetch(`${PIPELINE_URL}/contract/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contract_text: contractText,
        question,
        target_language: targetLanguage,
      }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });

    if (!res.ok) {
      // Don't log the body — it echoes contract content (policy.md §11).
      console.error(`[pipeline] /contract/ask failed: ${res.status}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`[pipeline] /contract/ask unreachable: ${err.name}`);
    return null;
  }
}

// Synthesis is a network round trip to the voice service plus a transcode. Kept short:
// the text reply has already been sent, so a slow voice note is the thing to drop.
const SPEAK_TIMEOUT_MS = Number(process.env.PIPELINE_SPEAK_TIMEOUT_MS) || 30000;

/**
 * Speak a reply, returning OGG/Opus bytes for upload to WhatsApp.
 *
 * Returns null on any failure. Every caller must treat that as "send the text only" —
 * losing the voice note is a degraded reply, but losing the reply is a failed one.
 *
 * @param {string} text
 * @param {string} language ISO 639-1 code the worker chose
 * @returns {Promise<Buffer|null>}
 */
export async function speak(text, language) {
  try {
    const res = await fetch(`${PIPELINE_URL}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, language }),
      signal: AbortSignal.timeout(SPEAK_TIMEOUT_MS),
    });

    if (!res.ok) {
      // Don't log the body — it echoes reply content (policy.md §11).
      console.error(`[pipeline] /speak failed: ${res.status}`);
      return null;
    }
    return Buffer.from(await res.arrayBuffer());
  } catch (err) {
    console.error(`[pipeline] /speak unreachable: ${err.name}`);
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
