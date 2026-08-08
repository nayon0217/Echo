/**
 * One handler per media kind. Each takes a Classification (src/media.js) plus the
 * sender's session and returns a Result, so index.js only has to dispatch and send.
 *
 * All three kinds converge on `verifyText()`: text goes straight in, voice arrives
 * via transcription, and image via caption or extracted text. That join point runs
 * claim extraction + DB retrieval + LLM true/false (policy.md §1 stages 3–9).
 */

import { transcribe, extractImage, processMessage, formatReply } from "./pipeline.js";
import { downloadMedia } from "./whatsapp.js";
import { HEARD_PREFIX, IMAGE_TEXT_PREFIX, uiString } from "./languages.js";

/**
 * @typedef {object} Result
 * @property {string|null} reply  text to send back; null means send nothing
 * @property {object|null} translation  ASR/vision output when that stage ran
 * @property {object|null} verification  /process pipeline result when verification ran
 */

/** Nothing to say — used for ignored media. */
const SILENT = { reply: null, translation: null, verification: null };

/**
 * Shared verification entry: English (or raw) text → claims → retrieve → T/F reply.
 *
 * @param {string} text
 * @param {{language?: {code: string, title: string}|null}} [session]
 * @param {{ text_en?: string, source_language?: string, heard?: string, heardPrefix?: string, mediaKind?: "voice"|"image"|"text" }} [opts]
 * @returns {Promise<Result>}
 */
export async function verifyText(text, session = {}, opts = {}) {
  const pivot = (opts.text_en || text || "").trim();
  if (!pivot) {
    return {
      reply: "That message looked empty — could you send it again?",
      translation: null,
      verification: null,
    };
  }

  const lang = session?.language?.code || null;

  try {
    const verification = await processMessage(text || pivot, lang, {
      text_en: opts.text_en || pivot,
      source_language: opts.source_language,
      mediaKind: opts.mediaKind || "text",
    });

    let reply = formatReply(verification, { mediaKind: opts.mediaKind || "text" });
    // Keep the "heard" line short so the whole WhatsApp message stays readable.
    if (opts.heard) {
      const heard = String(opts.heard).trim();
      const short =
        heard.split(/\s+/).length > 40
          ? heard.split(/\s+/).slice(0, 40).join(" ") + "…"
          : heard;
      const prefix = opts.heardPrefix || uiString(HEARD_PREFIX, lang || "en");
      reply = `${prefix}\n${short}\n\n${reply}`;
    }

    console.log(
      `[handler] verified ${verification?.claims?.length ?? 0} claim(s) ` +
        `(lang=${lang || "en"})`,
    );

    return { reply, translation: null, verification };
  } catch (err) {
    console.error(`[handler] verify failed: ${err.message || err}`);
    return {
      reply:
        "Sorry — I couldn't check that right now. Please try again shortly, " +
        "or call the MOM hotline 6438 5122.",
      translation: null,
      verification: null,
    };
  }
}

/** @deprecated use verifyText — kept as the named join point tests may import */
export async function processText(text, session = {}) {
  return verifyText(text, session);
}

/**
 * Typed message — translate + claim extraction + T/F against the corpus.
 *
 * @param {import("./media.js").Classification} c
 * @param {{language: {code: string, title: string}|null}} [session]
 * @returns {Promise<Result>}
 */
export async function handleText(c, session) {
  return verifyText(c.text, session, { mediaKind: "text" });
}

/**
 * Voice note or audio file: download → Whisper → translate → claims → T/F.
 *
 * Abstention gate 1 (§7) lives on the Python side — a transcript Whisper isn't
 * confident about comes back untranslated, and we ask for a re-record rather than
 * pushing a mis-heard claim downstream.
 *
 * @param {import("./media.js").Classification} c
 * @param {{language: {code: string, title: string}|null}} session
 * @returns {Promise<Result>}
 */
export async function handleVoice(c, session) {
  const target = session?.language?.code || "en";
  console.log(
    `[handler] voice ${c.isVoiceNote ? "note" : "file"} (${c.mimeType}) -> reply in ${target}`,
  );

  const media = await downloadMedia(c.mediaId);
  if (!media) {
    return {
      reply: "I couldn't download that voice note. Could you send it again?",
      translation: null,
      verification: null,
    };
  }

  const asr = await transcribe(media.buffer, media.mimeType || c.mimeType, target);
  if (!asr) {
    return {
      reply: "Sorry — I couldn't listen to that just now. Please try again in a moment.",
      translation: null,
      verification: null,
    };
  }

  // Gate 1 failed: Whisper heard something, but not well enough to trust.
  if (!asr.is_confident) {
    console.log(`[handler] gate 1 failed (logprob ${asr.mean_logprob})`);
    return {
      reply:
        "I couldn't hear that clearly enough to be sure what it said. " +
        "Could you record it again somewhere quieter, and speak a little closer to the phone?",
      translation: null,
      verification: null,
    };
  }

  if (asr.unintelligible || !asr.text_en) {
    return {
      reply: "I couldn't make out what that voice note said. Could you send it again?",
      translation: null,
      verification: null,
    };
  }

  console.log(`[handler] transcribed ${asr.duration_seconds}s of ${asr.spoken_language}`);

  const result = await verifyText(asr.transcript || asr.text_en, session, {
    text_en: asr.text_en,
    source_language: asr.spoken_language,
    heard: asr.text_target || asr.text_en,
    heardPrefix: uiString(HEARD_PREFIX, target),
    mediaKind: "voice",
  });

  return { ...result, translation: asr };
}

/**
 * Image — screenshot, job ad, "official" letter: download → vision → claims → T/F.
 *
 * @param {import("./media.js").Classification} c
 * @param {{language: {code: string, title: string}|null}} session
 * @returns {Promise<Result>}
 */
export async function handleImage(c, session) {
  const target = session?.language?.code || "en";
  console.log(
    `[handler] image (${c.mimeType})${c.caption ? " with caption" : ""} -> reply in ${target}`,
  );

  const media = await downloadMedia(c.mediaId);
  if (!media) {
    return withCaptionFallback(c, session, {
      reply: "I couldn't download that image. Could you send it again?",
      translation: null,
      verification: null,
    });
  }

  const extracted = await extractImage(media.buffer, media.mimeType || c.mimeType, target);
  if (!extracted) {
    return withCaptionFallback(c, session, {
      reply: "Sorry — I couldn't read that just now. Please try again in a moment.",
      translation: null,
      verification: null,
    });
  }

  if (!extracted.has_text) {
    return withCaptionFallback(c, session, {
      reply:
        "I couldn't find any text in that image. I can check messages, job ads, " +
        "and letters — if there's writing in it, a closer photo would help.",
      translation: null,
      verification: null,
    });
  }

  if (extracted.untranscribable) {
    console.log(`[handler] image gate failed (confidence ${extracted.confidence})`);
    return withCaptionFallback(c, session, {
      reply:
        "I can see there's writing in that image, but I couldn't read it clearly " +
        "enough to be sure. Could you send a sharper photo, taken straight on and " +
        "in better light?",
      translation: null,
      verification: null,
    });
  }

  if (extracted.unintelligible || !extracted.text_en) {
    return withCaptionFallback(c, session, {
      reply: "I couldn't make sense of the writing in that image. Could you send it again?",
      translation: null,
      verification: null,
    });
  }

  console.log(`[handler] read ${extracted.detected_language} text from image`);

  const result = await verifyText(extracted.text_source || extracted.text_en, session, {
    text_en: extracted.text_en,
    source_language: extracted.detected_language,
    heard: extracted.text_target || extracted.text_en,
    heardPrefix: uiString(IMAGE_TEXT_PREFIX, target),
    mediaKind: "image",
  });

  return { ...result, translation: extracted };
}

/**
 * When the picture yields nothing, fall back to the caption the worker typed.
 *
 * @param {import("./media.js").Classification} c
 * @param {{language: {code: string, title: string}|null}} session
 * @param {Result} failure
 * @returns {Promise<Result>}
 */
async function withCaptionFallback(c, session, failure) {
  if (!c.caption || !c.caption.trim()) return failure;

  console.log("[handler] image unreadable, falling back to caption");
  const result = await verifyText(c.caption, session, { mediaKind: "image" });

  if (!result.verification) return failure;

  return {
    ...result,
    reply: `I couldn't read the image, but I checked what you wrote with it.\n\n${result.reply}`,
  };
}

/**
 * Video, document, sticker, location, contacts, reaction, and anything else.
 * Ignored by product decision — logged, never replied to.
 *
 * @param {import("./media.js").Classification} c
 * @returns {Promise<Result>}
 */
export async function handleUnsupported(c) {
  console.log(`[handler] ignoring unsupported type: ${c.type}`);
  return SILENT;
}
