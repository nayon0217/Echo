/**
 * src/contractQuestion.js — which typed questions should wait for a contract upload.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { isContractQuestion } from "../src/contractQuestion.js";

test("personal salary and notice questions are contract questions", () => {
  assert.equal(isContractQuestion("how much is my salary?"), true);
  assert.equal(isContractQuestion("What is my salary, according to my contract?"), true);
  assert.equal(isContractQuestion("what is my notice period?"), true);
  assert.equal(isContractQuestion("can they deduct money from my pay?"), true);
});

test("policy rumours are not treated as contract questions", () => {
  assert.equal(isContractQuestion("is the levy going up?"), false);
  assert.equal(isContractQuestion("Levy naik?"), false);
  assert.equal(isContractQuestion("is this a scam?"), false);
  assert.equal(isContractQuestion("hi"), false);
});
