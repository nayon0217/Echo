/**
 * One handler per media kind. Each takes a Classification (src/media.js) plus the
 * sender's session and returns a Result, so index.js only has to dispatch and send.
 *
 * The image and voice handlers are stubs, but real ones: real signatures returning
 * real Results, per policy.md §4 ("not a `pass`"). The dispatch, reply, and error
 * paths are therefore already exercised — filling them in means replacing a body,
 * not wiring a new path.
 *
 * All three kinds converge on `processText()`: text goes straight in, voice arrives
 * via transcription, and image via caption or extracted text. That is the join point
 * where the verification stages (policy.md §1 stages 3-10) will eventually live, so
 * building them once serves all three.
 */

import { translate, transcribe, extractImage } from "./pipeline.js";
import { downloadMedia } from "./whatsapp.js";

/**
 * @typedef {object} Result
 * @property {string|null} reply  text to send back; null means send nothing
 * @property {object|null} translation  the pipeline's output, when it ran
 */

/** Nothing to say — used for ignored media. */
const SILENT = { reply: null, translation: null };

/**
 * The shared pipeline entry point for any message we have text for.
 *
 * Today: detect language and translate to English. Later this is where routing
 * (§4), claim extraction (§5), retrieval (§6), and the verdict passes (§7) go, so
 * voice and image get them for free once transcription and parsing land.
 *
 * @param {string} text
 * @returns {Promise<Result>}
 */
export async function processText(text) {
  if (!text || !text.trim()) {
    return { reply: "That message looked empty — could you send it again?", translation: null };
  }

  const result = await translate(text);

  if (!result) {
    return {
      reply: "Sorry — I couldn't process that just now. Please try again in a moment.",
      translation: null,
    };
  }

  if (result.unintelligible) {
    return { reply: "I couldn't read that message. Could you send it again?", translation: null };
  }

  // Language code only — never the message body (policy.md §11).
  console.log(`[handler] detected ${result.language_code}`);

  return {
    reply: `Detected language: ${result.language_name}\n\nIn English:\n${result.text_en}`,
    translation: result,
  };
}

/**
 * Typed message — the path that works today.
 *
 * @param {import("./media.js").Classification} c
 * @returns {Promise<Result>}
 */
export async function handleText(c) {
  return processText(c.text);
}

/**
 * Voice note or audio file: download, transcribe, translate into the worker's
 * chosen reply language (policy.md §1 stages 1-2).
 *
 * Abstention gate 1 (§7) lives on the Python side — a transcript Whisper isn't
 * confident about comes back untranslated, and we ask for a re-record rather than
 * pushing a mis-heard claim downstream. Getting that wrong is the failure mode §9
 * calls catastrophic, so it fails closed.
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
    };
  }

  const result = await transcribe(media.buffer, media.mimeType || c.mimeType, target);
  if (!result) {
    return {
      reply: "Sorry — I couldn't listen to that just now. Please try again in a moment.",
      translation: null,
    };
  }

  // Gate 1 failed: Whisper heard something, but not well enough to trust.
  if (!result.is_confident) {
    console.log(`[handler] gate 1 failed (logprob ${result.mean_logprob})`);
    return {
      reply:
        "I couldn't hear that clearly enough to be sure what it said. " +
        "Could you record it again somewhere quieter, and speak a little closer to the phone?",
      translation: null,
    };
  }

  if (result.unintelligible || !result.text_target) {
    return {
      reply: "I couldn't make out what that voice note said. Could you send it again?",
      translation: null,
    };
  }

  console.log(`[handler] transcribed ${result.duration_seconds}s of ${result.spoken_language}`);

  // Interim reply, same as the text path: show what was heard. Once the verification
  // stages land this becomes the reasoning narrative, delivered as audio (specs.md §2).
  return { reply: `Here's what I heard:\n\n${result.text_target}`, translation: result };
}

/**
 * Image — screenshot, job ad, "official" letter: download, read the text out of it,
 * translate into the worker's chosen reply language (policy.md §1 stages 1-2).
 *
 * The abstention gate lives on the Python side, same shape as voice: an image the
 * model couldn't read confidently comes back untranslated and we ask for a clearer
 * photo. That gate is weaker than the voice one — the confidence is self-reported
 * rather than a logprob (see pipeline/vision.py) — so it catches an unreadable photo
 * but not a confidently misread digit.
 *
 * A caption is the worker's own words and is not subject to any of that, so it is the
 * fallback whenever the picture itself yields nothing usable.
 *
 * Still missing (specs.md §5): AI-generation detection. A forged "MOM letter" that
 * reads perfectly will be transcribed and translated without comment.
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
    return withCaptionFallback(c, {
      reply: "I couldn't download that image. Could you send it again?",
      translation: null,
    });
  }

  const result = await extractImage(media.buffer, media.mimeType || c.mimeType, target);
  if (!result) {
    return withCaptionFallback(c, {
      reply: "Sorry — I couldn't read that just now. Please try again in a moment.",
      translation: null,
    });
  }

  // No text in the picture at all — a photo of a person or a place. Distinct from
  // "couldn't read it", and worth saying so rather than asking for a retake.
  if (!result.has_text) {
    return withCaptionFallback(c, {
      reply:
        "I couldn't find any text in that image. I can check messages, job ads, " +
        "and letters — if there's writing in it, a closer photo would help.",
      translation: null,
    });
  }

  // Gate failed: there is text, but not read well enough to trust.
  if (result.untranscribable) {
    console.log(`[handler] image gate failed (confidence ${result.confidence})`);
    return withCaptionFallback(c, {
      reply:
        "I can see there's writing in that image, but I couldn't read it clearly " +
        "enough to be sure. Could you send a sharper photo, taken straight on and " +
        "in better light?",
      translation: null,
    });
  }

  if (result.unintelligible || !result.text_target) {
    return withCaptionFallback(c, {
      reply: "I couldn't make sense of the writing in that image. Could you send it again?",
      translation: null,
    });
  }

  console.log(`[handler] read ${result.detected_language} text from image`);

  return { reply: `Here's what that image says:\n\n${result.text_target}`, translation: result };
}

/**
 * When the picture yields nothing, fall back to the caption the worker typed.
 *
 * The caption is often where the actual question lives ("is this real?"), and it costs
 * nothing to use text we already have rather than making someone re-send a photo.
 *
 * @param {import("./media.js").Classification} c
 * @param {Result} failure  what to reply if there is no caption to fall back to
 * @returns {Promise<Result>}
 */
async function withCaptionFallback(c, failure) {
  if (!c.caption || !c.caption.trim()) return failure;

  console.log("[handler] image unreadable, falling back to caption");
  const result = await processText(c.caption);

  // If the caption path also failed, the image message is the more useful one.
  if (!result.translation) return failure;

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
