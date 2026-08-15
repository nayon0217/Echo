/**
 * src/index.js — webhook routing and session state.
 *
 * This is the layer that decides what a message *means*: onboarding, the language
 * menu, and contract mode. Until now it had no tests at all — handlers.test.js calls
 * handlers directly and never exercises the dispatch that chooses between them.
 *
 * The real Express app is booted on a test port and driven with real Meta webhook
 * payloads. Only `globalThis.fetch` is stubbed, so outbound WhatsApp sends and pipeline
 * calls are captured instead of leaving the machine. Nothing in src/ is modified.
 *
 * Run: npm test
 */

import { test, describe, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";

const PORT = 39871;
process.env.PORT = String(PORT);
process.env.PIPELINE_URL = "http://pipeline.test";
process.env.number_ID = "TEST_PHONE_ID";
process.env.access_token = "TEST_TOKEN";
process.env.VERIFY_TOKEN = "test_verify";

/** Outbound WhatsApp sends, newest last. */
let sent;
/** url-substring -> (url, init) => Response */
let routes;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const realFetch = globalThis.fetch;
let realLog, realError;

globalThis.fetch = async (url, init = {}) => {
  const href = String(url);

  // The tests drive the app through its own HTTP surface, so requests to the app
  // under test go to the real network stack. Only what the app calls *out* is faked.
  if (href.includes(`127.0.0.1:${PORT}`)) return realFetch(url, init);

  // Outbound WhatsApp message — capture instead of sending.
  if (href.includes("graph.facebook.com") && href.endsWith("/messages")) {
    sent.push(JSON.parse(init.body));
    return json({ messages: [{ id: "wamid.sent" }] });
  }
  // Longest pattern first: "/contract" would otherwise shadow "/contract/ask", and
  // the ask call would silently receive a contract-read payload.
  const matches = [...routes.keys()]
    .filter((p) => href.includes(p))
    .sort((a, b) => b.length - a.length);
  if (matches.length) return routes.get(matches[0])(href, init);

  throw new Error(`unrouted fetch: ${href}`);
};

before(async () => {
  sent = [];
  routes = new Map();
  realLog = console.log;
  realError = console.error;
  console.log = () => {};
  console.error = () => {};

  // index.js calls isHealthy() at boot, so this must be routed before import.
  routes.set("/health", () => json({ status: "ok" }));
  await import("../src/index.js"); // starts listening on PORT
  await waitFor(() => true, 200);
});

after(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  console.error = realError;
  // The Express server keeps the event loop open and index.js doesn't export it, so
  // the runner is started with --test-force-exit (see package.json). Calling
  // process.exit() here instead would kill the reporter before it flushes results.
});

beforeEach(() => {
  sent = [];
  routes = new Map();
  routes.set("/health", () => json({ status: "ok" }));
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let messageSeq = 0;

/** Wrap a `messages[]` entry in the Meta webhook envelope and POST it. */
async function deliver(from, message) {
  const body = {
    entry: [
      {
        changes: [
          {
            value: {
              messages: [
                {
                  from,
                  id: `wamid.test.${++messageSeq}`,
                  timestamp: String(1700000000 + messageSeq),
                  ...message,
                },
              ],
            },
          },
        ],
      },
    ],
  };

  const res = await fetch(`http://127.0.0.1:${PORT}/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  assert.equal(res.status, 200, "the webhook must always ack 200 so Meta doesn't retry");
}

/** Poll until `predicate` holds, or throw. The webhook acks before doing the work. */
async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error(`timed out waiting; sent so far: ${JSON.stringify(sent, null, 2)}`);
}

/** Wait for the next outbound message and return it. */
async function nextReply(afterCount = 0) {
  await waitFor(() => sent.length > afterCount);
  return sent[sent.length - 1];
}

const textMsg = (body) => ({ type: "text", text: { body } });
const docMsg = (filename = "contract.pdf") => ({
  type: "document",
  document: { id: "DOC1", mime_type: "application/pdf", filename },
});
const pickLanguage = (id) => ({
  type: "interactive",
  interactive: { type: "list_reply", list_reply: { id, title: id } },
});

/** Take a fresh phone number so each test gets its own session. */
let phoneSeq = 0;
const newPhone = () => `6590000${String(++phoneSeq).padStart(3, "0")}`;

/** Drive a sender through onboarding so tests can start from "language chosen". */
async function onboard(phone, langId = "lang_en") {
  await deliver(phone, textMsg("hi"));
  await nextReply(0); // the language menu
  const before = sent.length;
  await deliver(phone, pickLanguage(langId));
  await waitFor(() => sent.length > before);
}

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

describe("onboarding", () => {
  test("a first message gets the language menu, not an answer", async () => {
    const phone = newPhone();
    await deliver(phone, textMsg("is the levy going up?"));

    const reply = await nextReply();
    assert.equal(reply.type, "interactive");
    assert.match(JSON.stringify(reply), /Which language should I reply in/);
  });

  test("the message that triggered onboarding is not auto-verified", async () => {
    // Deliberate upstream behaviour (see handleMessage): "Do not hold/auto-verify the
    // current message — verification starts on the *next* message after they choose."
    // Pinned so the earlier hold-and-replay design isn't reintroduced by accident.
    const phone = newPhone();
    let processed = false;
    routes.set("/process", () => {
      processed = true;
      return json({ reply: "verified" });
    });

    await deliver(phone, textMsg("Levy naik?"));
    await nextReply(); // the language menu
    await deliver(phone, pickLanguage("lang_en"));

    await waitFor(() => sent.length > 1); // the welcome message
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(processed, false, "the pre-onboarding message must not be replayed");
  });

  test("the next message after choosing a language is verified", async () => {
    const phone = newPhone();
    await onboard(phone);

    routes.set("/process", () => json({ reply: "Checked: this looks like a scam." }));

    await deliver(phone, textMsg("Levy naik?"));
    await waitFor(() => sent.some((m) => /looks like a scam/.test(JSON.stringify(m))));
  });

  test("a reply arrives as both text and a voice note", async () => {
    // specs.md §2 — the worker may not be able to read the text at all, so the audio
    // is the reply as far as the product is concerned.
    const phone = newPhone();
    await onboard(phone, "lang_bn");

    let spokenWith = null;
    routes.set("/speak", (_url, init) => {
      spokenWith = JSON.parse(init.body);
      return new Response(Buffer.from("OggS-fake"), {
        status: 200,
        headers: { "Content-Type": "audio/ogg" },
      });
    });
    routes.set("/media", () => json({ id: "MEDIA_TTS_1" }));
    routes.set("/process", () => json({ reply: "Checked: this looks like a scam." }));

    await deliver(phone, textMsg("Levy naik?"));
    await waitFor(() => sent.some((m) => m.type === "audio"));

    const audio = sent.find((m) => m.type === "audio");
    assert.equal(audio.audio.id, "MEDIA_TTS_1");
    assert.ok(
      sent.some((m) => m.type === "text" && /looks like a scam/.test(m.text?.body)),
      "the text reply must still be sent alongside the audio",
    );
    assert.equal(spokenWith.language, "bn", "spoken in the language they chose");
    assert.match(spokenWith.text, /looks like a scam/);
  });

  test("a reply still arrives when speech synthesis is down", async () => {
    const phone = newPhone();
    await onboard(phone);

    routes.set("/speak", () => json({ detail: "speech synthesis unavailable" }, 502));
    routes.set("/process", () => json({ reply: "Checked: this looks fine." }));

    await deliver(phone, textMsg("is this real?"));
    await waitFor(() => sent.some((m) => /looks fine/.test(JSON.stringify(m))));

    await new Promise((r) => setTimeout(r, 200));
    assert.ok(!sent.some((m) => m.type === "audio"), "no audio, but the answer arrived");
  });

  test("ignored media never triggers the language menu", async () => {
    const phone = newPhone();
    await deliver(phone, { type: "sticker", sticker: { id: "S1" } });

    // Give it room to do the wrong thing before concluding it didn't.
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(sent.length, 0, "a sticker must not start onboarding");
  });
});

// ---------------------------------------------------------------------------
// Contract mode
// ---------------------------------------------------------------------------

describe("contract mode", () => {
  const contractOk = () =>
    json({
      is_contract: true,
      is_usable: true,
      confidence: 0.94,
      language_code: "en",
      text: "1. Basic salary: SGD 800 per month.",
    });

  function mockMedia() {
    routes.set("graph.facebook.com/v21.0/DOC1", () =>
      json({ url: "https://lookaside.fbsbx.com/f", mime_type: "application/pdf" }),
    );
    routes.set("lookaside.fbsbx.com", () => new Response(Buffer.from("pdf"), { status: 200 }));
  }

  test("a plain message after a contract upload is answered against the contract", async () => {
    const phone = newPhone();
    await onboard(phone);
    mockMedia();
    routes.set("/contract", contractOk);

    await deliver(phone, docMsg());
    await waitFor(() => sent.some((m) => /I've read your contract/.test(JSON.stringify(m))));

    let askedWith = null;
    routes.set("/contract/ask", (_url, init) => {
      askedWith = JSON.parse(init.body);
      return json({
        answerable: true,
        answer_en: "Your basic salary is SGD 800 per month.",
        answer_target: "Your basic salary is SGD 800 per month.",
        quote: "Basic salary: SGD 800 per month.",
        needs_legal_check: false,
      });
    });

    const before = sent.length;
    await deliver(phone, textMsg("how much do I earn?"));
    await waitFor(() => sent.length > before);

    assert.ok(askedWith, "the question should have gone to /contract/ask, not /translate");
    assert.match(askedWith.contract_text, /SGD 800/);
    assert.match(JSON.stringify(sent[sent.length - 1]), /SGD 800/);
  });

  test("an unreadable contract does not put the sender into contract mode", async () => {
    const phone = newPhone();
    await onboard(phone);
    mockMedia();
    routes.set("/contract", () =>
      json({ is_contract: true, is_usable: false, confidence: 0.3, language_code: "en", text: "" }),
    );

    await deliver(phone, docMsg());
    await waitFor(() => sent.some((m) => /couldn't read it clearly/.test(JSON.stringify(m))));

    // The next message must go to verification, not to a contract we never held.
    let wentToVerification = false;
    routes.set("/process", () => {
      wentToVerification = true;
      return json({ reply: "verified: hello" });
    });
    routes.set("/contract/ask", () => {
      throw new Error("must not ask about a contract that was never held");
    });

    await deliver(phone, textMsg("hello"));
    await waitFor(() => wentToVerification);
  });

  test("'done' leaves contract mode and forgets the contract", async () => {
    const phone = newPhone();
    await onboard(phone);
    mockMedia();
    routes.set("/contract", contractOk);

    await deliver(phone, docMsg());
    await waitFor(() => sent.some((m) => /I've read your contract/.test(JSON.stringify(m))));

    let before = sent.length;
    await deliver(phone, textMsg("done"));
    await waitFor(() => sent.length > before);
    assert.match(JSON.stringify(sent[sent.length - 1]), /forgotten your contract/);

    // And a following message goes back to verification.
    routes.set("/contract/ask", () => {
      throw new Error("still in contract mode after 'done'");
    });
    routes.set("/process", () => json({ reply: "back to normal" }));

    await deliver(phone, textMsg("back to normal"));
    await waitFor(() => sent.some((m) => /back to normal/.test(JSON.stringify(m))));
  });

  test("a forwarded image in contract mode is still checked, and the contract is kept", async () => {
    // Contract mode captures typed questions only. A worker who uploads a contract and
    // then forwards a suspicious photo still wants the photo checked.
    const phone = newPhone();
    await onboard(phone);
    mockMedia();
    routes.set("/contract", contractOk);

    await deliver(phone, docMsg());
    await waitFor(() => sent.some((m) => /I've read your contract/.test(JSON.stringify(m))));

    routes.set("graph.facebook.com/v21.0/IMG9", () =>
      json({ url: "https://lookaside.fbsbx.com/i", mime_type: "image/jpeg" }),
    );
    let extracted = false;
    routes.set("/extract", () => {
      extracted = true;
      return json({
        text_source: "Pay $500 now",
        detected_language: "en",
        has_text: true,
        confidence: 0.9,
        untranscribable: false,
        text_en: "Pay $500 now",
        text_target: "Pay $500 now",
        target_language: "en",
        unintelligible: false,
      });
    });

    let before = sent.length;
    await deliver(phone, { type: "image", image: { id: "IMG9", mime_type: "image/jpeg" } });
    await waitFor(() => sent.length > before);
    assert.ok(extracted, "the image should go through the verification path");

    // Still in contract mode afterwards.
    let askedAgain = false;
    routes.set("/contract/ask", () => {
      askedAgain = true;
      return json({
        answerable: true,
        answer_en: "One month.",
        answer_target: "One month.",
        quote: "Notice period: one month.",
        needs_legal_check: false,
      });
    });

    before = sent.length;
    await deliver(phone, textMsg("what notice must I give?"));
    await waitFor(() => sent.length > before);
    assert.ok(askedAgain, "the contract should still be held after checking an image");
  });

  test("contracts are held per sender, not shared", async () => {
    const alice = newPhone();
    const bob = newPhone();
    await onboard(alice);
    await onboard(bob);

    mockMedia();
    routes.set("/contract", contractOk);
    await deliver(alice, docMsg());
    await waitFor(() => sent.some((m) => /I've read your contract/.test(JSON.stringify(m))));

    routes.set("/contract/ask", () => {
      throw new Error("bob was answered from alice's contract");
    });
    routes.set("/process", () => json({ reply: "bob's message went to verification" }));

    await deliver(bob, textMsg("is the levy going up?"));
    await waitFor(() => sent.some((m) => /went to verification/.test(JSON.stringify(m))));
  });

  test("a contract question with no document asks for the contract, not policy check", async () => {
    const phone = newPhone();
    await onboard(phone);

    let processed = false;
    routes.set("/process", () => {
      processed = true;
      return json({ reply: "should not verify" });
    });

    await deliver(phone, textMsg("how much is my salary?"));
    await waitFor(() =>
      sent.some((m) => /question about your employment contract/i.test(JSON.stringify(m))),
    );
    assert.equal(processed, false, "must not run the policy path");
  });

  test("after the prompt, the uploaded contract answers the held question and later ones", async () => {
    const phone = newPhone();
    await onboard(phone);

    await deliver(phone, textMsg("how much is my salary?"));
    await waitFor(() =>
      sent.some((m) => /question about your employment contract/i.test(JSON.stringify(m))),
    );

    mockMedia();
    routes.set("/contract", contractOk);

    let askedWith = null;
    let askCount = 0;
    routes.set("/contract/ask", (_url, init) => {
      askedWith = JSON.parse(init.body);
      askCount += 1;
      return json({
        answerable: true,
        answer_en: "Your basic salary is SGD 800 per month.",
        answer_target: "Your basic salary is SGD 800 per month.",
        quote: "Basic salary: SGD 800 per month.",
        needs_legal_check: false,
      });
    });

    await deliver(phone, docMsg());
    await waitFor(() => sent.some((m) => /SGD 800/.test(JSON.stringify(m))));
    assert.ok(askedWith, "the held question must be asked against the uploaded contract");
    assert.match(askedWith.question, /salary/i);
    assert.match(JSON.stringify(sent.at(-1)), /keep this contract/i);

    const before = sent.length;
    await deliver(phone, textMsg("what notice must I give?"));
    await waitFor(() => sent.length > before);
    assert.equal(askCount, 2, "follow-up questions must use the held contract");
  });

  test("salary question then passport follow-up after the contract is sent", async () => {
    const phone = newPhone();
    await onboard(phone);

    const salaryQ = "What is my salary, according to my contract?";
    const passportQ = "What does the passport section say?";
    const contractText =
      "1. Basic salary: SGD 800 per month.\n" +
      "2. Passport: The Employer shall keep the Worker's passport for safekeeping.";

    let processed = false;
    routes.set("/process", () => {
      processed = true;
      return json({ reply: "policy path must not run" });
    });

    await deliver(phone, textMsg(salaryQ));
    await waitFor(() =>
      sent.some((m) => /Please send the contract/i.test(JSON.stringify(m))),
    );
    assert.equal(processed, false, "step 2: prompt for the contract, do not fact-check MOM");

    mockMedia();
    routes.set("/contract", () =>
      json({
        is_contract: true,
        is_usable: true,
        confidence: 0.94,
        language_code: "en",
        text: contractText,
      }),
    );

    const questions = [];
    routes.set("/contract/ask", (_url, init) => {
      const body = JSON.parse(init.body);
      questions.push(body.question);
      assert.match(body.contract_text, /Basic salary: SGD 800/);
      assert.match(body.contract_text, /Passport/);

      if (/salary/i.test(body.question)) {
        return json({
          answerable: true,
          answer_en: "Your basic salary is SGD 800 per month.",
          answer_target: "Your basic salary is SGD 800 per month.",
          quote: "Basic salary: SGD 800 per month.",
          needs_legal_check: false,
        });
      }
      return json({
        answerable: true,
        answer_en: "The employer keeps the worker's passport for safekeeping.",
        answer_target: "The employer keeps the worker's passport for safekeeping.",
        quote: "The Employer shall keep the Worker's passport for safekeeping.",
        needs_legal_check: false,
      });
    });

    await deliver(phone, docMsg());
    await waitFor(() =>
      sent.some((m) => /Your basic salary is SGD 800/.test(JSON.stringify(m))),
    );
    assert.deepEqual(questions, [salaryQ], "step 4: answer the original salary question");
    assert.match(
      JSON.stringify(sent.at(-1)),
      /keep this contract/i,
      "the contract stays held for follow-ups",
    );

    const before = sent.length;
    await deliver(phone, textMsg(passportQ));
    await waitFor(() =>
      sent.some((m) => /passport for safekeeping/i.test(JSON.stringify(m))),
    );
    assert.equal(sent.length > before, true);
    assert.deepEqual(questions, [salaryQ, passportQ], "step 6: passport question uses the same contract");
    assert.equal(processed, false);
  });
});

// ---------------------------------------------------------------------------
// Webhook mechanics
// ---------------------------------------------------------------------------

describe("webhook mechanics", () => {
  test("a duplicate message id is processed once", async () => {
    // Meta redelivers; without dedupe the worker gets answered twice.
    const phone = newPhone();
    await onboard(phone);

    let calls = 0;
    routes.set("/process", () => {
      calls += 1;
      return json({ reply: "verified" });
    });

    const payload = {
      entry: [
        {
          changes: [
            {
              value: {
                messages: [
                  { from: phone, id: "wamid.duplicate", timestamp: "1700009999", ...textMsg("hi") },
                ],
              },
            },
          ],
        },
      ],
    };
    const post = () =>
      fetch(`http://127.0.0.1:${PORT}/webhook`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

    await post();
    await waitFor(() => calls === 1);
    await post();
    await new Promise((r) => setTimeout(r, 300));

    assert.equal(calls, 1, "the second delivery should have been skipped");
  });

  test("a status callback with no messages is ignored", async () => {
    const res = await fetch(`http://127.0.0.1:${PORT}/webhook`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry: [{ changes: [{ value: { statuses: [{ status: "read" }] } }] }] }),
    });
    assert.equal(res.status, 200);
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(sent.length, 0);
  });

  test("the verification handshake echoes the challenge", async () => {
    const res = await fetch(
      `http://127.0.0.1:${PORT}/webhook?hub.mode=subscribe&hub.verify_token=test_verify&hub.challenge=abc123`,
    );
    assert.equal(res.status, 200);
    assert.equal(await res.text(), "abc123");
  });

  test("a wrong verify token is rejected", async () => {
    const res = await fetch(
      `http://127.0.0.1:${PORT}/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc`,
    );
    assert.equal(res.status, 403);
  });
});
