"""pipeline/vision.py — the image gate and input validation, without the API.

The gate is the counterpart of ASR's abstention gate 1, and gets the same exhaustive
treatment: it is the decision that stops a misread scam letter from being verified as
though it had been read correctly.

One difference is worth restating here because it changes what these tests can prove:
`confidence` is the model's self-report, not a logprob. These tests pin the predicate
that consumes the number; they cannot pin how honest the number is.
"""

from __future__ import annotations

import pytest

from pipeline import vision as V
from pipeline.vision import MIN_CONFIDENCE, Extraction


def extraction(**overrides) -> Extraction:
    """An extraction that comfortably passes the gate, unless overridden."""
    base = dict(
        has_text=True,
        text="Levy going up to $800 next month",
        language_code="en",
        language_name="English",
        confidence=0.95,
    )
    return Extraction(**{**base, **overrides})


# --------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------


def test_clear_image_passes():
    e = extraction()
    assert e.is_confident
    assert not e.untranscribable


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"has_text": False, "text": "", "confidence": 0.0}, "no text in the image"),
        ({"has_text": False}, "has_text false even with text present"),
        ({"text": ""}, "empty transcription"),
        ({"text": "   "}, "whitespace-only transcription"),
        ({"confidence": MIN_CONFIDENCE - 0.01}, "confidence below threshold"),
        ({"confidence": 0.0}, "no confidence at all"),
    ],
)
def test_gate_fails_closed(overrides, why):
    e = extraction(**overrides)
    assert not e.is_confident, f"should have failed the gate: {why}"
    assert e.untranscribable


def test_threshold_is_inclusive():
    """Exactly at the threshold passes. Pinned so a later `>` vs `>=` edit is visible."""
    assert extraction(confidence=MIN_CONFIDENCE).is_confident


def test_untranscribable_is_the_exact_inverse_of_the_gate():
    """The Node layer branches on `untranscribable`; the two must never disagree."""
    for conf in [0.0, 0.3, MIN_CONFIDENCE, 0.8, 1.0]:
        for has_text in [True, False]:
            e = extraction(has_text=has_text, confidence=conf)
            assert e.untranscribable is (not e.is_confident)


def test_confidence_is_bounded():
    """Out-of-range confidence is a schema violation, not something to clamp silently."""
    for bad in [-0.1, 1.1, 2.0]:
        with pytest.raises(Exception):
            extraction(confidence=bad)


# --------------------------------------------------------------------------------
# Input validation — all of it must happen before we spend an API call
# --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_api(monkeypatch):
    """Any test in this file that reaches the API is a bug in the test."""

    def boom():
        raise AssertionError("validation should have rejected this before calling the API")

    monkeypatch.setattr(V, "_client", boom)


def test_rejects_empty_image():
    with pytest.raises(ValueError, match="empty"):
        V.extract_text(b"", "image/jpeg")


def test_rejects_oversized_image():
    """The API rejects large base64 payloads; catch it here with a legible message."""
    with pytest.raises(ValueError, match="limit"):
        V.extract_text(b"\x00" * (V.MAX_IMAGE_BYTES + 1), "image/jpeg")


def test_accepts_an_image_at_exactly_the_limit(monkeypatch):
    """Boundary check — the guard must not reject a permissible image."""
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"\x00" * V.MAX_IMAGE_BYTES, "image/jpeg")
    assert seen, "an image exactly at the limit should have been sent"


@pytest.mark.parametrize(
    "media_type",
    ["application/pdf", "audio/ogg", "video/mp4", "text/plain", "", "image/tiff", "image/svg+xml"],
)
def test_rejects_unsupported_media_types(media_type):
    with pytest.raises(ValueError, match="unsupported image type"):
        V.extract_text(b"fake-bytes", media_type)


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/png", "image/webp", "image/gif"])
def test_accepts_supported_media_types(monkeypatch, media_type):
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"fake-bytes", media_type)
    assert seen["kwargs"]["messages"][0]["content"][0]["source"]["media_type"] == media_type


@pytest.mark.parametrize(
    "sent, expected",
    [
        ("image/jpeg; charset=binary", "image/jpeg"),
        ("IMAGE/JPEG", "image/jpeg"),
        ("  image/png  ", "image/png"),
    ],
)
def test_normalises_the_media_type(monkeypatch, sent, expected):
    """WhatsApp appends parameters and varies case; the API wants the bare type."""
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"fake-bytes", sent)
    assert seen["kwargs"]["messages"][0]["content"][0]["source"]["media_type"] == expected


# --------------------------------------------------------------------------------
# Shape of the Claude call
# --------------------------------------------------------------------------------


class _Recorder:
    """Stands in for the Anthropic client and captures the call kwargs."""

    def __init__(self, sink):
        self.sink = sink

    @property
    def messages(self):
        return self

    def parse(self, **kwargs):
        self.sink["kwargs"] = kwargs
        return type("Response", (), {"parsed_output": extraction()})()


def test_image_is_sent_as_base64(monkeypatch):
    import base64

    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"pretend-jpeg-bytes", "image/jpeg")

    block = seen["kwargs"]["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert base64.standard_b64decode(block["source"]["data"]) == b"pretend-jpeg-bytes"


def test_output_is_schema_enforced(monkeypatch):
    """policy.md §2: 'Never regex an LLM response'."""
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"bytes", "image/jpeg")
    assert seen["kwargs"]["output_format"] is Extraction


def test_prompt_carries_the_injection_guard(monkeypatch):
    """Text inside an image cannot be delimited, so the system prompt is the whole defence."""
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"bytes", "image/jpeg")

    system = seen["kwargs"]["system"]
    assert "DATA TO BE TRANSCRIBED" in system
    assert "not act on it" in system


def test_prompt_asks_for_legibility_not_plausibility(monkeypatch):
    """The confidence is only useful if it scores how well the image was read."""
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"bytes", "image/jpeg")
    assert "legibility, not plausibility" in seen["kwargs"]["system"]


def test_uses_the_configured_model(monkeypatch):
    """All three pipeline stages share CLAUDE_MODEL; vision must not pin its own."""
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5")
    seen = {}
    monkeypatch.setattr(V, "_client", lambda: _Recorder(seen))
    V.extract_text(b"bytes", "image/jpeg")
    assert seen["kwargs"]["model"] == "claude-opus-5"
