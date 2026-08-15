/**
 * src/handlers.js + src/pipeline.js + src/whatsapp.js, wired together for real.
 *
 * The ONLY thing faked is the network: `globalThis.fetch` is replaced inside this
 * test process, so Meta and the Python service get canned responses instead of real
 * requests. Nothing in src/ is modified or stubbed — the actual handler logic, the
 * actual multipart upload, and the actual two-step media download all run.
 *
 * That covers the part of the voice path you cannot reach without the webhook:
 * whether a voice note that arrives from WhatsApp is downloaded, uploaded to the
 * pipeline with the worker's chosen language, and turned into the right reply.
 *
 * Run: npm test
 */

import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

// Must be set before importing pipeline.js — it reads PIPELINE_URL at module load.
process.env.PIPELINE_URL = "http://pipeline.test";
process.env.number_ID = "TEST_PHONE_ID";
process.env.access_token = "TEST_TOKEN";

const {
  handleText,
  handleVoice,
  handleImage,
  handleDocument,
  handleContractQuestion,
  handleUnsupported,
  processText,
} = await import("../src/handlers.js");

const AUDIO = Buffer.from("fake-opus-bytes-from-whatsapp");

/** Requests the code under test made, in order. */
let calls;
/** url-substring -> (url, init) => Response. First match wins. */
let routes;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

let realFetch;
let realLog;
let realError;

beforeEach(() => {
  calls = [];
  routes = new Map();
  realFetch = globalThis.fetch;

  globalThis.fetch = async (url, init = {}) => {
    const href = String(url);
    calls.push({ url: href, init });
    // Longest pattern first, so "/contract" can't shadow "/contract/ask".
    const matches = [...routes.keys()]
      .filter((p) => href.includes(p))
      .sort((a, b) => b.length - a.length);
    if (matches.length) return routes.get(matches[0])(href, init);

    throw new Error(`unrouted fetch: ${href}`);
  };

  // The handlers log per-message diagnostics; keep the test output readable.
  realLog = console.log;
  realError = console.error;
  console.log = () => {};
  console.error = () => {};
});

afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  console.error = realError;
});

/** Wire up a successful Meta media download for `mediaId`. */
function mockMediaDownload(mediaId, { mimeType = "audio/ogg; codecs=opus" } = {}) {
  routes.set(`graph.facebook.com/v21.0/${mediaId}`, () =>
    json({ url: "https://lookaside.fbsbx.com/whatsapp/media/xyz", mime_type: mimeType }),
  );
  routes.set("lookaside.fbsbx.com", () => new Response(AUDIO, { status: 200 }));
}

const voiceMessage = (mediaId = "MEDIA1") => ({
  kind: "voice",
  type: "audio",
  mediaId,
  mimeType: "audio/ogg; codecs=opus",
  isVoiceNote: true,
});


/** A successful /process verification response, overridable per test. */
const verified = (overrides = {}) => ({
  claims: [
    {
      text: "The work permit levy is going up to 800 dollars next month",
      type: "levy",
      verdict: "refuted",
      reasoning: "No matching MOM announcement.",
      cited_sources: [],
      gates_triggered: [],
      top_score: 0.01,
    },
  ],
  notice: null,
  scam: null,
  ...overrides,
});


// ---------------------------------------------------------------------------
// Text
// ---------------------------------------------------------------------------

describe("text messages", () => {
  test("sends the message to /process and replies with a T/F verdict", async () => {
    routes.set("/process", () => json(verified()));

    const result = await handleText(
      { kind: "text", text: "Levy naik jadi 800 dolar?" },
      { language: { code: "en", title: "English" } },
    );

    assert.match(result.reply, /false|couldn.t confirm/i);
    assert.equal(result.verification.claims[0].verdict, "refuted");

    const sent = JSON.parse(calls[0].init.body);
    assert.equal(sent.text, "Levy naik jadi 800 dolar?");
    assert.equal(sent.with_verify, true);
  });

  test("empty text never reaches the pipeline", async () => {
    const result = await processText("   ");
    assert.match(result.reply, /empty/i);
    assert.equal(calls.length, 0, "spent an API call on an empty message");
  });

  test("degrades gracefully when the pipeline is down", async () => {
    routes.set("/process", () => {
      throw new TypeError("fetch failed");
    });

    const result = await handleText({ kind: "text", text: "hello" }, { language: { code: "en" } });
    assert.match(result.reply, /try again|couldn.t check/i);
    assert.equal(result.verification, null);
  });

  test("degrades gracefully on a pipeline 502", async () => {
    routes.set("/process", () => json({ detail: "claude api error" }, 502));

    const result = await handleText({ kind: "text", text: "hello" }, { language: { code: "en" } });
    assert.match(result.reply, /try again|couldn.t check/i);
  });

  test("surfaces the pipeline notice when nothing is checkable", async () => {
    routes.set("/process", () =>
      json(
        verified({
          claims: [],
          notice:
            "I can't verify this message — it doesn't contain a policy claim I can check. If you're unsure, call the MOM hotline 6438 5122.",
        }),
      ),
    );

    const result = await handleText({ kind: "text", text: "🙂🙂🙂" }, { language: { code: "en" } });
    assert.match(result.reply, /can.t verify|hotline/i);
  });
});

// ---------------------------------------------------------------------------
// Voice
// ---------------------------------------------------------------------------

describe("voice notes", () => {
  test("downloads, transcribes, verifies, and replies with a T/F verdict", async () => {
    mockMediaDownload("MEDIA1");
    routes.set("/transcribe", () =>
      json({
        transcript: "Levy naik jadi 800 dolar bulan depan",
        spoken_language: "id",
        mean_logprob: -0.24,
        language_probability: 0.98,
        max_no_speech_prob: 0.01,
        duration_seconds: 3.4,
        is_confident: true,
        text_en: "The levy is going up to 800 dollars next month",
        text_target: "லெவி அடுத்த மாதம் 800 டாலராக உயர்கிறது",
        target_language: "ta",
        unintelligible: false,
      }),
    );
    routes.set("/process", () => json(verified()));

    const result = await handleVoice(voiceMessage(), {
      language: { code: "ta", title: "தமிழ்" },
    });

    assert.match(result.reply, /Heard:|கேட்டது:/);
    assert.match(result.reply, /800/);
    assert.match(result.reply, /false|couldn.t confirm|True|False/i);
    assert.equal(result.translation.spoken_language, "id");
    assert.equal(result.verification.claims[0].verdict, "refuted");

    const processBody = JSON.parse(calls.find((c) => c.url.includes("/process")).init.body);
    assert.equal(processBody.text_en, "The levy is going up to 800 dollars next month");
    assert.equal(processBody.source_language, "id");
    assert.equal(processBody.language, "ta");
  });

  test("uploads the audio bytes and the chosen language as multipart", async () => {
    mockMediaDownload("MEDIA1");
    let form;
    routes.set("/transcribe", (_url, init) => {
      form = init.body;
      return json({
        transcript: "hi",
        spoken_language: "en",
        mean_logprob: -0.2,
        language_probability: 0.99,
        max_no_speech_prob: 0.01,
        duration_seconds: 1,
        is_confident: true,
        text_en: "hi",
        text_target: "hi",
        target_language: "en",
        unintelligible: false,
      });
    });
    routes.set("/process", () => json(verified({ claims: [] , notice: "nothing to check" })));

    await handleVoice(voiceMessage(), { language: { code: "en", title: "English" } });

    assert.ok(form instanceof FormData, "audio must go up as multipart/form-data");
    assert.equal(form.get("target_language"), "en");

    const file = form.get("file");
    assert.equal(file.name, "voice.ogg", "extension hints Whisper's decoder");
    assert.equal(
      Buffer.from(await file.arrayBuffer()).toString(),
      AUDIO.toString(),
      "the bytes downloaded from Meta must be the bytes sent to Whisper",
    );
  });

  test("defaults to English when the worker has not picked a language yet", async () => {
    mockMediaDownload("MEDIA1");
    let target;
    routes.set("/transcribe", (_url, init) => {
      target = init.body.get("target_language");
      return json({
        transcript: "hi",
        spoken_language: "en",
        mean_logprob: -0.2,
        language_probability: 0.99,
        max_no_speech_prob: 0.01,
        duration_seconds: 1,
        is_confident: true,
        text_en: "hi",
        text_target: "hi",
        target_language: "en",
        unintelligible: false,
      });
    });
    routes.set("/process", () => json(verified({ claims: [], notice: "nothing to check" })));

    await handleVoice(voiceMessage(), {});
    assert.equal(target, "en");
  });

  test("gate 1 failure asks for a re-record and shows nothing it mis-heard", async () => {
    mockMediaDownload("MEDIA1");
    routes.set("/transcribe", () =>
      json({
        transcript: "uh ... eight hundred? ... permit",
        spoken_language: "en",
        mean_logprob: -0.91,
        language_probability: 0.4,
        max_no_speech_prob: 0.3,
        duration_seconds: 2.1,
        is_confident: false,
        text_en: null,
        text_target: null,
        target_language: null,
        unintelligible: false,
      }),
    );

    const result = await handleVoice(voiceMessage(), { language: { code: "en" } });

    assert.match(result.reply, /record it again/i);
    assert.equal(result.translation, null);
    // policy.md §9: showing a mis-hearing back as though it were understood is the
    // catastrophic failure. The uncertain transcript must not appear in the reply.
    assert.doesNotMatch(result.reply, /eight hundred/i);
  });

  test("handles a failed media lookup", async () => {
    routes.set("graph.facebook.com", () => json({ error: "not found" }, 404));

    const result = await handleVoice(voiceMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't download/i);
    assert.equal(result.translation, null);
  });

  test("handles the media CDN rejecting the token", async () => {
    // The classic: the second call needs the bearer token too, and 401s without it.
    routes.set("graph.facebook.com/v21.0/MEDIA1", () =>
      json({ url: "https://lookaside.fbsbx.com/whatsapp/media/xyz", mime_type: "audio/ogg" }),
    );
    routes.set("lookaside.fbsbx.com", () => new Response("", { status: 401 }));

    const result = await handleVoice(voiceMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't download/i);
  });

  test("sends the bearer token on both media calls", async () => {
    mockMediaDownload("MEDIA1");
    routes.set("/transcribe", () => json({ is_confident: false, transcript: "" }));

    await handleVoice(voiceMessage(), { language: { code: "en" } });

    const mediaCalls = calls.filter((c) => !c.url.includes("pipeline.test"));
    assert.equal(mediaCalls.length, 2);
    for (const call of mediaCalls) {
      assert.match(call.init.headers.Authorization, /^Bearer /);
    }
  });

  test("degrades gracefully when the pipeline is unreachable", async () => {
    mockMediaDownload("MEDIA1");
    routes.set("/transcribe", () => {
      throw new TypeError("fetch failed");
    });

    const result = await handleVoice(voiceMessage(), { language: { code: "en" } });
    assert.match(result.reply, /try again/i);
    assert.equal(result.translation, null);
  });

  test("asks for a resend when a confident transcript still says nothing", async () => {
    mockMediaDownload("MEDIA1");
    routes.set("/transcribe", () =>
      json({
        transcript: "...",
        spoken_language: "und",
        mean_logprob: -0.3,
        language_probability: 0.5,
        max_no_speech_prob: 0.1,
        duration_seconds: 1.0,
        is_confident: true,
        text_en: "",
        text_target: "",
        target_language: "en",
        unintelligible: true,
      }),
    );

    const result = await handleVoice(voiceMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't make out/i);
  });
});

// ---------------------------------------------------------------------------
// Image and everything else
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Images
// ---------------------------------------------------------------------------

const imageMessage = (caption = "") => ({
  kind: "image",
  type: "image",
  mediaId: "IMG1",
  mimeType: "image/jpeg",
  caption,
});

/** A successful /extract response, overridable per test. */
const extracted = (overrides = {}) => ({
  text_source: "Kerja di Singapura! Gaji $4,800 sebulan.",
  detected_language: "id",
  has_text: true,
  confidence: 0.93,
  untranscribable: false,
  text_en: "Work in Singapore! Salary $4,800 a month.",
  text_target: "சிங்கப்பூரில் வேலை! மாதம் $4,800 சம்பளம்.",
  target_language: "ta",
  unintelligible: false,
  ...overrides,
});

describe("images", () => {
  // Photos try /contract first (contract pages often arrive as WhatsApp images).
  beforeEach(() => {
    routes.set("/contract", () =>
      json({
        is_contract: false,
        is_usable: false,
        confidence: 0.95,
        language_code: "en",
        text: "",
      }),
    );
  });

  test("downloads, reads, verifies, and replies with a T/F verdict", async () => {
    mockMediaDownload("IMG1", { mimeType: "image/jpeg" });
    routes.set("/extract", () => json(extracted()));
    routes.set("/process", () =>
      json(
        verified({
          claims: [
            {
              text: "A job in Singapore pays $4,800 a month",
              type: "salary",
              verdict: "insufficient",
              reasoning: "",
              cited_sources: [],
              gates_triggered: [],
              top_score: 0,
            },
          ],
        }),
      ),
    );

    const result = await handleImage(imageMessage(), {
      language: { code: "ta", title: "தமிழ்" },
    });

    assert.match(result.reply, /Image text:|பட உரை:/);
    assert.match(result.reply, /4,800/);
    assert.match(result.reply, /couldn.t confirm|can.t confirm|True|False/i);
    assert.equal(result.translation.detected_language, "id");
    assert.equal(result.verification.claims[0].verdict, "insufficient");
  });

  test("a photographed employment contract enters contract mode, not verification", async () => {
    mockMediaDownload("IMG1", { mimeType: "image/jpeg" });
    routes.set("/contract", () =>
      json(
        contractRead({
          text:
            "STANDARD EMPLOYMENT CONTRACT BETWEEN FOREIGN DOMESTIC WORKER AND EMPLOYER\n" +
            "Basic salary: SGD 650",
        }),
      ),
    );
    let hitExtract = false;
    routes.set("/extract", () => {
      hitExtract = true;
      return json(extracted());
    });

    const result = await handleImage(imageMessage(), {
      language: { code: "en", title: "English" },
    });

    assert.equal(hitExtract, false, "usable contracts must not fall through to /extract");
    assert.match(result.reply, /I've read your contract/i);
    assert.match(result.contract.text, /FOREIGN DOMESTIC WORKER/i);
    assert.equal(result.verification, undefined);
  });

  test("uploads the image bytes and chosen language as multipart", async () => {
    mockMediaDownload("IMG1", { mimeType: "image/png" });
    let form;
    routes.set("/extract", (_url, init) => {
      form = init.body;
      return json(extracted());
    });
    routes.set("/process", () => json(verified({ claims: [], notice: "nothing" })));

    await handleImage(imageMessage(), { language: { code: "ta" } });

    assert.ok(form instanceof FormData, "the image must go up as multipart/form-data");
    assert.equal(form.get("target_language"), "ta");

    const file = form.get("file");
    assert.equal(file.name, "image.png", "extension follows the real media type");
    assert.equal(file.type, "image/png", "the pipeline validates on the declared type");
    assert.equal(
      Buffer.from(await file.arrayBuffer()).toString(),
      AUDIO.toString(),
      "the bytes downloaded from Meta must be the bytes sent to the pipeline",
    );
  });

  test("defaults to English when the worker has not picked a language yet", async () => {
    mockMediaDownload("IMG1");
    let target;
    routes.set("/extract", (_url, init) => {
      target = init.body.get("target_language");
      return json(extracted());
    });
    routes.set("/process", () => json(verified({ claims: [], notice: "nothing" })));

    await handleImage(imageMessage(), {});
    assert.equal(target, "en");
  });

  test("an unreadable image asks for a sharper photo, showing nothing it misread", async () => {
    mockMediaDownload("IMG1");
    routes.set("/extract", () =>
      json(
        extracted({
          untranscribable: true,
          confidence: 0.22,
          text_source: "L[unclear]vy $8[unclear]0",
          text_en: null,
          text_target: null,
          target_language: null,
        }),
      ),
    );

    const result = await handleImage(imageMessage(), { language: { code: "en" } });

    assert.match(result.reply, /sharper photo/i);
    assert.equal(result.translation, null);
    // Same rule as the voice gate: a possible misreading must never be shown back
    // as though it were understood.
    assert.doesNotMatch(result.reply, /unclear/i);
  });

  test("a picture with no writing gets a different reply than an unreadable one", async () => {
    mockMediaDownload("IMG1");
    routes.set("/extract", () =>
      json(
        extracted({
          has_text: false,
          untranscribable: true,
          confidence: 0,
          text_source: "",
          text_en: null,
          text_target: null,
        }),
      ),
    );

    const result = await handleImage(imageMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't find any text/i);
    assert.doesNotMatch(result.reply, /sharper photo/i);
  });

  test("handles a failed media download", async () => {
    routes.set("graph.facebook.com", () => json({ error: "not found" }, 404));

    const result = await handleImage(imageMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't download/i);
    assert.equal(result.translation, null);
  });

  test("degrades gracefully when the pipeline is unreachable", async () => {
    mockMediaDownload("IMG1");
    routes.set("/extract", () => {
      throw new TypeError("fetch failed");
    });

    const result = await handleImage(imageMessage(), { language: { code: "en" } });
    assert.match(result.reply, /try again/i);
    assert.equal(result.translation, null);
  });

  test("falls back to the caption when the image can't be read", async () => {
    // The caption is the worker's own words — no OCR risk, and often where the
    // actual question is. Using it beats making them re-send the photo.
    mockMediaDownload("IMG1");
    routes.set("/extract", () => json(extracted({ untranscribable: true, text_target: null })));
    routes.set("/process", () =>
      json(
        verified({
          claims: [],
          notice: "I can't verify this message — it doesn't contain a policy claim I can check.",
        }),
      ),
    );

    const result = await handleImage(imageMessage("Apakah lowongan ini asli?"), {
      language: { code: "en" },
    });

    assert.match(result.reply, /couldn't read the image/i);
    assert.match(result.reply, /can.t verify|policy claim/i);
    assert.ok(result.verification, "the caption should still be verified");
  });

  test("no caption means no fallback call", async () => {
    mockMediaDownload("IMG1");
    routes.set("/extract", () => json(extracted({ untranscribable: true, text_target: null })));

    const result = await handleImage(imageMessage(""), { language: { code: "en" } });

    assert.match(result.reply, /sharper photo/i);
    assert.ok(
      !calls.some((c) => c.url.includes("/process")),
      "an empty caption must not trigger a /process call",
    );
  });

  test("a caption that also fails leaves the image message intact", async () => {
    mockMediaDownload("IMG1");
    routes.set("/extract", () => json(extracted({ untranscribable: true, text_target: null })));
    routes.set("/process", () => json({ detail: "boom" }, 502));

    const result = await handleImage(imageMessage("is this real?"), {
      language: { code: "en" },
    });
    assert.match(result.reply, /sharper photo/i);
    assert.equal(result.verification, null);
  });
});

// ---------------------------------------------------------------------------
// Contracts
// ---------------------------------------------------------------------------

const CONTRACT_TEXT = "1. Basic salary: SGD 800 per month.\n2. Notice period: one month.";

const documentMessage = (overrides = {}) => ({
  kind: "document",
  type: "document",
  mediaId: "DOC1",
  mimeType: "application/pdf",
  filename: "contract.pdf",
  caption: "",
  ...overrides,
});

const contractRead = (overrides = {}) => ({
  is_contract: true,
  is_usable: true,
  confidence: 0.94,
  language_code: "en",
  text: CONTRACT_TEXT,
  ...overrides,
});

describe("contract upload", () => {
  test("reads the contract and hands the text back to be held", async () => {
    mockMediaDownload("DOC1", { mimeType: "application/pdf" });
    routes.set("/contract", () => json(contractRead()));

    const result = await handleDocument(documentMessage(), { language: { code: "ta" } });

    assert.match(result.reply, /I've read your contract/i);
    assert.equal(result.contract.text, CONTRACT_TEXT);
    assert.match(result.reply, /done/, "the worker must be told how to leave contract mode");
  });

  test("uploads the document bytes with the right media type", async () => {
    mockMediaDownload("DOC1", { mimeType: "application/pdf" });
    let form;
    routes.set("/contract", (_url, init) => {
      form = init.body;
      return json(contractRead());
    });

    await handleDocument(documentMessage(), { language: { code: "en" } });

    const file = form.get("file");
    assert.equal(file.name, "contract.pdf");
    assert.equal(file.type, "application/pdf");
    assert.equal(
      Buffer.from(await file.arrayBuffer()).toString(),
      AUDIO.toString(),
      "the bytes downloaded from Meta must be the bytes sent to the pipeline",
    );
  });

  test("a document that isn't a contract gets a specific explanation", async () => {
    mockMediaDownload("DOC1");
    routes.set("/contract", () => json(contractRead({ is_contract: false, is_usable: false, text: "" })));

    const result = await handleDocument(documentMessage(), { language: { code: "en" } });

    assert.match(result.reply, /doesn't look like an employment contract/i);
    assert.equal(result.contract, undefined, "nothing should be held");
  });

  test("an unreadable contract is refused rather than half-used", async () => {
    // The strictest gate in the pipeline: a misread salary would be quoted back to
    // someone with no other way to check it.
    mockMediaDownload("DOC1");
    routes.set("/contract", () =>
      json(contractRead({ is_usable: false, confidence: 0.35, text: "" })),
    );

    const result = await handleDocument(documentMessage(), { language: { code: "en" } });

    assert.match(result.reply, /couldn't read it clearly enough/i);
    assert.equal(result.contract, undefined, "an unusable contract must never be held");
  });

  test("handles a failed download", async () => {
    routes.set("graph.facebook.com", () => json({ error: "not found" }, 404));

    const result = await handleDocument(documentMessage(), { language: { code: "en" } });
    assert.match(result.reply, /couldn't download/i);
    assert.equal(result.contract, undefined);
  });

  test("degrades gracefully when the pipeline is unreachable", async () => {
    mockMediaDownload("DOC1");
    routes.set("/contract", () => {
      throw new TypeError("fetch failed");
    });

    const result = await handleDocument(documentMessage(), { language: { code: "en" } });
    assert.match(result.reply, /try again/i);
    assert.equal(result.contract, undefined);
  });
});

describe("contract questions", () => {
  const session = (overrides = {}) => ({
    language: { code: "ta" },
    contract: { text: CONTRACT_TEXT },
    ...overrides,
  });

  const answer = (overrides = {}) => ({
    answerable: true,
    answer_en: "Your basic salary is SGD 800 per month.",
    answer_target: "உங்கள் அடிப்படை சம்பளம் மாதம் SGD 800.",
    quote: "Basic salary: SGD 800 per month.",
    needs_legal_check: false,
    ...overrides,
  });

  test("answers in the worker's language and shows the clause it relied on", async () => {
    routes.set("/contract/ask", () => json(answer()));

    const result = await handleContractQuestion("how much do I earn?", session());

    assert.match(result.reply, /SGD 800/);
    assert.match(result.reply, /Your contract says/);
    assert.match(result.reply, /Basic salary: SGD 800 per month\./);
  });

  test("sends the held contract text with the question", async () => {
    let sent;
    routes.set("/contract/ask", (_url, init) => {
      sent = JSON.parse(init.body);
      return json(answer());
    });

    await handleContractQuestion("what notice must I give?", session());

    assert.equal(sent.contract_text, CONTRACT_TEXT);
    assert.equal(sent.question, "what notice must I give?");
    assert.equal(sent.target_language, "ta");
  });

  test("an unanswerable question says so and quotes nothing", async () => {
    routes.set("/contract/ask", () =>
      json(
        answer({
          answerable: false,
          answer_en: "Your contract does not mention overtime pay.",
          answer_target: "உங்கள் ஒப்பந்தத்தில் மேலதிக நேர ஊதியம் பற்றி இல்லை.",
          quote: "",
        }),
      ),
    );

    const result = await handleContractQuestion("what is my overtime rate?", session());

    assert.doesNotMatch(result.reply, /Your contract says/, "nothing to quote");
    assert.match(result.reply, /மேலதிக/);
  });

  test("a legality question points to MOM instead of answering it", async () => {
    // We were handed the contract, not the law. Implying otherwise would be inventing
    // legal advice for someone who cannot check it.
    routes.set("/contract/ask", () => json(answer({ needs_legal_check: true })));

    const result = await handleContractQuestion("is this deduction legal?", session());

    assert.match(result.reply, /can't tell you whether that's allowed/i);
    assert.match(result.reply, /1800 339 5505/);
  });

  test("falls back to English when the target rendering is missing", async () => {
    routes.set("/contract/ask", () => json(answer({ answer_target: "" })));
    const result = await handleContractQuestion("how much do I earn?", session());
    assert.match(result.reply, /Your basic salary is SGD 800/);
  });

  test("degrades gracefully when the pipeline is unreachable", async () => {
    routes.set("/contract/ask", () => {
      throw new TypeError("fetch failed");
    });

    const result = await handleContractQuestion("how much do I earn?", session());
    assert.match(result.reply, /try again/i);
    assert.equal(result.translation, null);
  });
});

describe("other media", () => {
  test("unsupported media is silently ignored", async () => {
    const result = await handleUnsupported({ kind: "unsupported", type: "sticker" });
    assert.equal(result.reply, null, "a reply to every sticker would be spam");
    assert.equal(calls.length, 0);
  });
});
