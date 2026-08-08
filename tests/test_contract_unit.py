"""pipeline/contract.py — the usability gate, validation, and the prompt contract.

The gate here is stricter than the image one (0.7 vs 0.6) and the reason is worth
stating: a misread scam poster produces a bad answer about someone else's message, but
a misread contract produces a wrong number for the worker's own salary, quoted back to
someone who has no other way to check it.

These tests spend no tokens. What they cannot check — whether the model actually
abstains when the contract is silent — is in test_contract_live.py.
"""

from __future__ import annotations

import base64

import pytest

from pipeline import contract as C
from pipeline.contract import ContractAnswer, ContractRead


def read(**overrides) -> ContractRead:
    """A read that comfortably passes the gate, unless overridden."""
    base = dict(
        is_contract=True,
        text="1. Basic salary: SGD 800 per month.\n2. Notice period: one month.",
        language_code="en",
        confidence=0.95,
    )
    return ContractRead(**{**base, **overrides})


# --------------------------------------------------------------------------------
# The usability gate
# --------------------------------------------------------------------------------


def test_a_clear_contract_is_usable():
    assert read().is_usable


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"is_contract": False}, "not an employment document"),
        ({"text": ""}, "nothing transcribed"),
        ({"text": "   "}, "whitespace only"),
        ({"confidence": C.MIN_CONFIDENCE - 0.01}, "below the confidence threshold"),
        ({"confidence": 0.0}, "no confidence at all"),
        ({"is_contract": False, "confidence": 0.99}, "legible, but not a contract"),
    ],
)
def test_gate_fails_closed(overrides, why):
    assert not read(**overrides).is_usable, f"should have been unusable: {why}"


def test_threshold_is_inclusive():
    assert read(confidence=C.MIN_CONFIDENCE).is_usable


def test_contract_gate_is_stricter_than_the_image_gate():
    """Deliberate, and worth pinning: a wrong salary figure is worse than a wrong poster."""
    from pipeline import vision

    assert C.MIN_CONFIDENCE > vision.MIN_CONFIDENCE


def test_a_read_that_clears_the_image_gate_can_still_fail_this_one():
    """The concrete consequence of that gap."""
    from pipeline import vision

    between = (vision.MIN_CONFIDENCE + C.MIN_CONFIDENCE) / 2
    assert not read(confidence=between).is_usable


# --------------------------------------------------------------------------------
# Input validation — before any API call
# --------------------------------------------------------------------------------


class _Recorder:
    """Stands in for the Anthropic client and captures the call kwargs."""

    def __init__(self, sink, parsed):
        self.sink = sink
        self.parsed = parsed

    @property
    def messages(self):
        return self

    def parse(self, **kwargs):
        self.sink["kwargs"] = kwargs
        return type("Response", (), {"parsed_output": self.parsed})()


@pytest.fixture
def recorder(monkeypatch):
    def install(parsed):
        sink = {}
        monkeypatch.setattr(C, "_client", lambda: _Recorder(sink, parsed))
        return sink

    return install


@pytest.fixture
def no_api(monkeypatch):
    def boom():
        raise AssertionError("validation should have rejected this before the API call")

    monkeypatch.setattr(C, "_client", boom)


def test_rejects_empty_document(no_api):
    with pytest.raises(ValueError, match="empty"):
        C.read_contract(b"", "application/pdf")


def test_rejects_oversized_document(no_api):
    with pytest.raises(ValueError, match="limit"):
        C.read_contract(b"\x00" * (C.MAX_DOCUMENT_BYTES + 1), "application/pdf")


@pytest.mark.parametrize(
    "media_type", ["text/plain", "audio/ogg", "video/mp4", "", "application/msword"]
)
def test_rejects_unsupported_document_types(no_api, media_type):
    with pytest.raises(ValueError, match="unsupported document type"):
        C.read_contract(b"bytes", media_type)


def test_pdf_is_sent_as_a_document_block(recorder):
    sink = recorder(read())
    C.read_contract(b"%PDF-1.4 pretend", "application/pdf")

    block = sink["kwargs"]["messages"][0]["content"][0]
    assert block["type"] == "document", "a PDF must go up as a document block, not an image"
    assert block["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(block["source"]["data"]) == b"%PDF-1.4 pretend"


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/png", "image/webp"])
def test_a_photographed_page_is_sent_as_an_image_block(recorder, media_type):
    """A worker photographing their contract is the common case, not the exception."""
    sink = recorder(read())
    C.read_contract(b"page-bytes", media_type)

    block = sink["kwargs"]["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == media_type


def test_normalises_the_media_type(recorder):
    sink = recorder(read())
    C.read_contract(b"bytes", "APPLICATION/PDF; charset=binary")
    assert sink["kwargs"]["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"


# --------------------------------------------------------------------------------
# Question validation
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   "])
def test_rejects_an_empty_contract(no_api, bad):
    with pytest.raises(ValueError, match="contract_text is empty"):
        C.answer_question(bad, "what is my salary?", "en")


@pytest.mark.parametrize("bad", ["", "   "])
def test_rejects_an_empty_question(no_api, bad):
    with pytest.raises(ValueError, match="question is empty"):
        C.answer_question("some contract text", bad, "en")


@pytest.mark.parametrize("bad", ["fr", "xx", "EN", ""])
def test_rejects_an_unsupported_target_language(no_api, bad):
    with pytest.raises(ValueError, match="unsupported target language"):
        C.answer_question("some contract text", "what is my salary?", bad)


# --------------------------------------------------------------------------------
# Shape of the Claude calls
# --------------------------------------------------------------------------------


def answer(**overrides) -> ContractAnswer:
    base = dict(
        answerable=True,
        answer_en="Your basic salary is SGD 800 per month.",
        answer_target="আপনার মূল বেতন প্রতি মাসে SGD 800।",
        quote="Basic salary: SGD 800 per month.",
        needs_legal_check=False,
    )
    return ContractAnswer(**{**base, **overrides})


def test_contract_and_question_are_delimited_separately(recorder):
    """The model must be able to tell the document from the person asking.

    Both are untrusted, but they are untrusted in different ways — the contract may
    carry injected text, the question is the worker speaking.
    """
    sink = recorder(answer())
    C.answer_question("SALARY: 800", "how much do I earn?", "en")

    content = sink["kwargs"]["messages"][0]["content"]
    assert "<contract>" in content and "</contract>" in content
    assert "<question>" in content and "</question>" in content
    assert content.index("</contract>") < content.index("<question>")


def test_the_answer_prompt_forbids_outside_knowledge(recorder):
    """The single most important property: no invented Singapore employment law."""
    sink = recorder(answer())
    C.answer_question("text", "is this legal?", "en")

    system = sink["kwargs"]["system"]
    assert "Answer ONLY from the contract text" in system
    assert "Never use outside knowledge" in system
    assert "legal, lawful, permitted" in system


def test_the_answer_prompt_names_the_target_language(recorder):
    sink = recorder(answer())
    C.answer_question("text", "what is my salary?", "ta")
    assert "Tamil" in sink["kwargs"]["system"]


def test_the_read_prompt_carries_the_injection_guard(recorder):
    sink = recorder(read())
    C.read_contract(b"bytes", "application/pdf")
    assert "DATA TO BE TRANSCRIBED" in sink["kwargs"]["system"]


def test_both_calls_are_schema_enforced(recorder):
    """policy.md §2: 'Never regex an LLM response'."""
    sink = recorder(read())
    C.read_contract(b"bytes", "application/pdf")
    assert sink["kwargs"]["output_format"] is ContractRead

    sink = recorder(answer())
    C.answer_question("text", "q", "en")
    assert sink["kwargs"]["output_format"] is ContractAnswer


def test_reading_gets_a_large_token_budget(recorder):
    """A contract is far longer than a chat screenshot; truncation would lose clauses."""
    sink = recorder(read())
    C.read_contract(b"bytes", "application/pdf")
    assert sink["kwargs"]["max_tokens"] >= 16384


def test_both_calls_use_the_configured_model(recorder, monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5")
    sink = recorder(read())
    C.read_contract(b"bytes", "application/pdf")
    assert sink["kwargs"]["model"] == "claude-opus-5"


def test_quote_defaults_to_empty_when_unanswerable():
    """Nothing to quote when the contract doesn't cover it — the field must not be required."""
    a = ContractAnswer(
        answerable=False,
        answer_en="Your contract does not mention overtime pay.",
        answer_target="আপনার চুক্তিতে ওভারটাইম বেতনের কথা নেই।",
    )
    assert a.quote == ""
    assert a.needs_legal_check is False
