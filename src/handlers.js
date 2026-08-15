/**
 * One handler per media kind. Each takes a Classification (src/media.js) plus the
 * sender's session and returns a Result, so index.js only has to dispatch and send.
 *
 * All three kinds converge on `verifyText()`: text goes straight in, voice arrives
 * via transcription, and image via caption or extracted text. That join point runs
 * claim extraction + DB retrieval + LLM true/false (policy.md §1 stages 3–9).
 */

import {
  transcribe,
  extractImage,
  processMessage,
  formatReply,
  readContract,
  askContract,
} from "./pipeline.js";
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

/** Keep complete sentences that fit `limit` words. Never append an ellipsis. */
function fitWords(text, limit) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return trimmed;
  const words = trimmed.split(/\s+/);
  if (words.length <= limit) return trimmed;

  const sentences = trimmed.split(/(?<=[.!?。！？။])\s+/).filter(Boolean);
  const kept = [];
  let count = 0;
  for (const sentence of sentences) {
    const n = sentence.split(/\s+/).filter(Boolean).length;
    if (count + n > limit) break;
    kept.push(sentence);
    count += n;
  }
  return kept.length ? kept.join(" ") : sentences[0];
}

const CONTRACT_WELCOME =
  "I've read your contract. Ask me anything about it — your salary, deductions, " +
  "notice period, working hours.\n\n" +
  "I'll only tell you what the contract itself says. Send \"done\" when you want " +
  "to go back to checking messages.";

/**
 * Shared success path once a usable employment document has been transcribed.
 * @param {{ text: string }} result
 * @param {string} [filename]
 */
function acceptContract(result, filename = "") {
  return {
    reply: CONTRACT_WELCOME,
    translation: null,
    contract: { text: result.text, filename },
  };
}

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
    // Keep the "heard" line as complete sentences, never mid-cut with "…".
    if (opts.heard) {
      const heard = String(opts.heard).trim();
      const short = fitWords(heard, 40);
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
 * Image — screenshot, job ad, "official" letter, or a photographed contract page.
 *
 * Contract photos often arrive as WhatsApp images (camera / gallery), not as
 * document uploads. Try the contract reader first; if it is not an employment
 * document, fall through to claim verification.
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

  const mime = media.mimeType || c.mimeType;
  const contractResult = await readContract(media.buffer, mime);
  if (contractResult?.is_usable) {
    console.log("[handler] image is an employment contract -> contract mode");
    return acceptContract(contractResult);
  }
  if (contractResult?.is_contract) {
    console.log(`[handler] contract image unusable (confidence ${contractResult.confidence})`);
    return {
      reply:
        "I can see this is a contract, but I couldn't read it clearly enough to answer " +
        "questions about it safely. Could you send a sharper copy — the PDF if you " +
        "have it, or photos taken straight on in good light?",
      translation: null,
      verification: null,
    };
  }

  const extracted = await extractImage(media.buffer, mime, target);
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
 * A document — how an employment contract usually arrives (specs.md §2).
 *
 * Reads it once, then hands the text back for the caller to hold in the session. From
 * that point the sender is in contract mode and their plain messages are answered
 * against it (see handleContractQuestion) rather than run through verification.
 *
 * The contract never leaves memory: not written to disk, not sent to Postgres, gone
 * when the process stops. That is what keeps policy.md §11 intact — this is the most
 * sensitive document the bot will ever hold.
 *
 * @param {import("./media.js").Classification} c
 * @param {{language: {code: string, title: string}|null}} session
 * @returns {Promise<Result & {contract?: {text: string, filename: string}}>}
 */
export async function handleDocument(c, session) {
  console.log(`[handler] document (${c.mimeType})${c.filename ? ` "${c.filename}"` : ""}`);

  const media = await downloadMedia(c.mediaId);
  if (!media) {
    return {
      reply: "I couldn't download that file. Could you send it again?",
      translation: null,
    };
  }

  const result = await readContract(media.buffer, media.mimeType || c.mimeType);
  if (!result) {
    return {
      reply: "Sorry — I couldn't read that just now. Please try again in a moment.",
      translation: null,
    };
  }

  // Not an employment document at all. Say what we do handle rather than failing vaguely.
  if (!result.is_contract) {
    return {
      reply:
        "That doesn't look like an employment contract. I can read contracts, offer " +
        "letters, IPAs, and work permit letters — send one of those and you can ask " +
        "me questions about it.",
      translation: null,
    };
  }

  // It is a contract, but not read well enough to answer questions against. Refusing
  // here matters more than elsewhere: a misread salary figure would be quoted back to
  // someone who has no other way to check it.
  if (!result.is_usable) {
    console.log(`[handler] contract unusable (confidence ${result.confidence})`);
    return {
      reply:
        "I can see this is a contract, but I couldn't read it clearly enough to answer " +
        "questions about it safely. Could you send a sharper copy — the PDF if you " +
        "have it, or photos taken straight on in good light?",
      translation: null,
    };
  }

  return acceptContract(result, c.filename || "");
}

/**
 * Answer a question against the contract held for this sender.
 *
 * Grounded strictly in the document. When the contract doesn't cover the question we
 * say so rather than filling the gap — a plausible invention about someone's pay is
 * worse than "your contract doesn't say", because they cannot check it.
 *
 * @param {string} question
 * @param {{contract: {text: string}, language: {code: string}|null}} session
 * @returns {Promise<Result>}
 */
export async function handleContractQuestion(question, session) {
  const target = session?.language?.code || "en";

  const result = await askContract(session.contract.text, question, target);
  if (!result) {
    return {
      reply: "Sorry — I couldn't check your contract just now. Please try again in a moment.",
      translation: null,
    };
  }

  // Nothing about the answer's content is logged (policy.md §11).
  console.log(`[handler] contract question answered (answerable=${result.answerable})`);

  let reply = result.answer_target || result.answer_en;

  // The quote is what makes the answer checkable — a worker can hold it against the
  // page in front of them. Only useful when there is an actual answer.
  if (result.answerable && result.quote) {
    reply += `\n\nYour contract says:\n"${result.quote}"`;
  }

  // We were given the contract, not the law. Say so rather than implying we checked.
  if (result.needs_legal_check) {
    reply +=
      "\n\nI can only tell you what your contract says — I can't tell you whether " +
      "that's allowed under Singapore law. The MOM helpline is 1800 339 5505.";
  }

  return { reply, translation: result };
}

/**
 * Video, sticker, location, contacts, reaction, and anything else.
 * Ignored by product decision — logged, never replied to.
 *
 * @param {import("./media.js").Classification} c
 * @returns {Promise<Result>}
 */
export async function handleUnsupported(c) {
  console.log(`[handler] ignoring unsupported type: ${c.type}`);
  return SILENT;
}
