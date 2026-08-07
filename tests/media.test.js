/**
 * src/media.js — the routing decision.
 *
 * This is the first thing an inbound WhatsApp message hits, and it is pure, so it
 * can be checked against real Meta payload shapes without a webhook. Getting it
 * wrong means a voice note silently takes the text path, or a sticker gets a reply.
 *
 * Run: npm test
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { classify, KIND } from "../src/media.js";

describe("text", () => {
  test("extracts the body", () => {
    const c = classify({ type: "text", text: { body: "levy going up?" } });
    assert.equal(c.kind, KIND.TEXT);
    assert.equal(c.text, "levy going up?");
  });

  test("survives a missing body", () => {
    // handlers.processText() treats empty as "send it again" — it must not throw here.
    const c = classify({ type: "text" });
    assert.equal(c.kind, KIND.TEXT);
    assert.equal(c.text, "");
  });
});

describe("voice", () => {
  test("classifies an in-app voice note", () => {
    const c = classify({
      type: "audio",
      audio: { id: "MEDIA123", mime_type: "audio/ogg; codecs=opus", voice: true },
    });
    assert.equal(c.kind, KIND.VOICE);
    assert.equal(c.mediaId, "MEDIA123");
    assert.equal(c.mimeType, "audio/ogg; codecs=opus");
    assert.equal(c.isVoiceNote, true);
  });

  test("an uploaded audio file is still transcribable", () => {
    // Meta omits `voice` for forwarded files. Both go to Whisper; the flag only
    // records which it was.
    const c = classify({ type: "audio", audio: { id: "M2", mime_type: "audio/mpeg" } });
    assert.equal(c.kind, KIND.VOICE);
    assert.equal(c.isVoiceNote, false);
  });
});

describe("image", () => {
  test("keeps the caption, where the claim usually is", () => {
    const c = classify({
      type: "image",
      image: { id: "IMG1", mime_type: "image/jpeg", caption: "is this real?" },
    });
    assert.equal(c.kind, KIND.IMAGE);
    assert.equal(c.mediaId, "IMG1");
    assert.equal(c.caption, "is this real?");
  });

  test("caption defaults to empty, not undefined", () => {
    const c = classify({ type: "image", image: { id: "IMG2" } });
    assert.equal(c.caption, "");
  });
});

describe("control", () => {
  test("list reply — how the language menu is answered", () => {
    const c = classify({
      type: "interactive",
      interactive: { type: "list_reply", list_reply: { id: "lang_ta", title: "தமிழ்" } },
    });
    assert.equal(c.kind, KIND.CONTROL);
    assert.equal(c.replyId, "lang_ta");
  });

  test("button reply", () => {
    const c = classify({
      type: "interactive",
      interactive: { type: "button_reply", button_reply: { id: "yes" } },
    });
    assert.equal(c.kind, KIND.CONTROL);
    assert.equal(c.replyId, "yes");
  });

  test("legacy button payload", () => {
    const c = classify({ type: "button", button: { payload: "lang_en" } });
    assert.equal(c.kind, KIND.CONTROL);
    assert.equal(c.replyId, "lang_en");
  });
});

describe("ignored", () => {
  for (const type of [
    "video",
    "document",
    "sticker",
    "location",
    "contacts",
    "reaction",
    "order",
    "system",
    "unknown",
    "some_type_meta_adds_in_2027",
  ]) {
    test(`${type} is unsupported`, () => {
      assert.equal(classify({ type }).kind, KIND.UNSUPPORTED);
    });
  }

  test("a malformed message does not throw", () => {
    // The webhook must ack 200 whatever Meta sends; a crash here would retry-loop.
    for (const bad of [undefined, null, {}, { type: null }]) {
      assert.equal(classify(bad).kind, KIND.UNSUPPORTED);
    }
  });
});
