/**
 * src/whatsapp.js — outbound media and the voice-note reply path.
 *
 * `sendReply` is the one place where a reply becomes both text and audio (specs.md §2).
 * The property that matters most is its failure behaviour: the text is sent first and
 * every subsequent step is best-effort, because losing the voice note degrades a reply
 * while losing the reply fails the worker outright.
 *
 * Only `globalThis.fetch` is stubbed. Nothing in src/ is modified.
 *
 * Run: npm test
 */

import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

process.env.number_ID = "TEST_PHONE_ID";
process.env.access_token = "TEST_TOKEN";

const { sendReply, uploadMedia, sendVoiceNote, sendText } = await import("../src/whatsapp.js");

const AUDIO = Buffer.from("OggS-pretend-opus-bytes");

let calls;
let routes;
let realFetch, realLog, realError;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  calls = [];
  routes = new Map();
  realFetch = globalThis.fetch;
  realLog = console.log;
  realError = console.error;
  console.log = () => {};
  console.error = () => {};

  globalThis.fetch = async (url, init = {}) => {
    const href = String(url);
    calls.push({ url: href, init });
    const matches = [...routes.keys()]
      .filter((p) => href.includes(p))
      .sort((a, b) => b.length - a.length);
    if (matches.length) return routes.get(matches[0])(href, init);
    throw new Error(`unrouted fetch: ${href}`);
  };

  // Sensible defaults; individual tests override.
  routes.set("/messages", () => json({ messages: [{ id: "wamid.sent" }] }));
  routes.set("/media", () => json({ id: "MEDIA_UPLOADED_1" }));
});

afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  console.error = realError;
});

/** Bodies of the outbound message sends, in order. */
const messages = () =>
  calls.filter((c) => c.url.endsWith("/messages")).map((c) => JSON.parse(c.init.body));

const okSpeak = async () => AUDIO;

// ---------------------------------------------------------------------------
// Media upload
// ---------------------------------------------------------------------------

describe("uploadMedia", () => {
  test("posts the bytes as multipart and returns the media id", async () => {
    let init;
    routes.set("/media", (_url, i) => {
      init = i;
      return json({ id: "MEDIA_9" });
    });

    const id = await uploadMedia(AUDIO, "audio/ogg", "reply.ogg");

    assert.equal(id, "MEDIA_9");
    assert.equal(init.body.get("messaging_product"), "whatsapp");
    assert.equal(init.body.get("type"), "audio/ogg");

    const file = init.body.get("file");
    assert.equal(file.name, "reply.ogg");
    assert.equal(Buffer.from(await file.arrayBuffer()).toString(), AUDIO.toString());
    assert.match(init.headers.Authorization, /^Bearer /);
  });

  test("returns null on a rejected upload rather than throwing", async () => {
    routes.set("/media", () => json({ error: "too large" }, 400));
    assert.equal(await uploadMedia(AUDIO, "audio/ogg", "reply.ogg"), null);
  });

  test("returns null when the upload is unreachable", async () => {
    routes.set("/media", () => {
      throw new TypeError("fetch failed");
    });
    assert.equal(await uploadMedia(AUDIO, "audio/ogg", "reply.ogg"), null);
  });
});

describe("sendVoiceNote", () => {
  test("sends an audio message referencing the uploaded id", async () => {
    await sendVoiceNote("6590000001", "MEDIA_9");

    const [msg] = messages();
    assert.equal(msg.type, "audio");
    assert.equal(msg.audio.id, "MEDIA_9");
    assert.equal(msg.messaging_product, "whatsapp");
  });
});

// ---------------------------------------------------------------------------
// sendReply — text plus voice
// ---------------------------------------------------------------------------

describe("sendReply", () => {
  test("sends the text first, then the voice note", async () => {
    await sendReply("6590000001", "Your salary is SGD 800.", "ta", okSpeak);

    const sent = messages();
    assert.equal(sent.length, 2);
    assert.equal(sent[0].type, "text", "the text must go first — it's the reliable half");
    assert.equal(sent[0].text.body, "Your salary is SGD 800.");
    assert.equal(sent[1].type, "audio");
  });

  test("passes the reply text and language to the speech function", async () => {
    let seen;
    await sendReply("6590000001", "Possible scam.", "bn", async (text, language) => {
      seen = { text, language };
      return AUDIO;
    });

    assert.deepEqual(seen, { text: "Possible scam.", language: "bn" });
  });

  test("uploads the audio as OGG so WhatsApp renders a voice note", async () => {
    // Any other container arrives as a file attachment the worker has to open, which
    // defeats the point of a voice-first reply.
    let uploadedType;
    routes.set("/media", (_url, init) => {
      uploadedType = init.body.get("type");
      return json({ id: "M1" });
    });

    await sendReply("6590000001", "hello", "en", okSpeak);
    assert.equal(uploadedType, "audio/ogg");
  });
});

describe("sendReply degrades to text", () => {
  const assertTextStillSent = () => {
    const sent = messages();
    assert.equal(sent.length, 1, "exactly the text should have been sent");
    assert.equal(sent[0].type, "text");
  };

  test("when synthesis returns nothing", async () => {
    await sendReply("6590000001", "hello", "en", async () => null);
    assertTextStillSent();
  });

  test("when synthesis returns empty audio", async () => {
    await sendReply("6590000001", "hello", "en", async () => Buffer.alloc(0));
    assertTextStillSent();
  });

  test("when synthesis throws", async () => {
    await sendReply("6590000001", "hello", "en", async () => {
      throw new Error("pipeline down");
    });
    assertTextStillSent();
  });

  test("when the media upload fails", async () => {
    routes.set("/media", () => json({ error: "nope" }, 500));
    await sendReply("6590000001", "hello", "en", okSpeak);
    assertTextStillSent();
  });

  test("a voice failure never throws out of sendReply", async () => {
    // index.js awaits this on the reply path; an exception here would abort the turn
    // after the worker has already been answered.
    routes.set("/media", () => {
      throw new TypeError("fetch failed");
    });
    await assert.doesNotReject(() => sendReply("6590000001", "hello", "en", okSpeak));
  });
});
