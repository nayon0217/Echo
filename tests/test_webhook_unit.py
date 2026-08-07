"""app/webhook.py — the HTTP contract the Node layer depends on.

Both pipeline stages are faked here, so what is under test is the service's own
behaviour: status-code mapping, the gate-1 short circuit, and the temp-file cleanup
policy.md §11 commits to. These are the paths a WhatsApp end-to-end test would
exercise only by accident, if at all — a real voice note is confident, decodable,
and cheap to translate, so the interesting branches never fire.
"""

from __future__ import annotations

import io
import os

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app import webhook as W
from pipeline.asr import Transcript
from pipeline.translate import Translation, VoiceTranslation
from pipeline.vision import Extraction


@pytest.fixture
def client():
    return TestClient(W.app)


def api_status_error(status: int = 500) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "boom", response=httpx.Response(status, request=request), body=None
    )


def api_connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def transcript(**overrides) -> Transcript:
    base = dict(
        text="Levy naik jadi 800 dolar",
        language="id",
        language_probability=0.98,
        mean_logprob=-0.25,
        max_no_speech_prob=0.02,
        duration=3.2,
        segment_count=1,
        model="base",
    )
    return Transcript(**{**base, **overrides})


def upload(name="voice.ogg", data=b"\x00fake-opus-bytes"):
    return {"file": (name, io.BytesIO(data), "audio/ogg")}


# --------------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------------


def test_health_reports_both_models(client):
    """Node calls this at boot. It must name both models, since either can be misconfigured."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model"] and body["whisper_model"]


# --------------------------------------------------------------------------------
# /translate — the text feature
# --------------------------------------------------------------------------------


def test_translate_returns_the_full_contract(client, monkeypatch):
    monkeypatch.setattr(
        W,
        "detect_and_translate",
        lambda text: Translation(
            language_code="bn",
            language_name="Bengali",
            text_en="The levy is going up",
            unintelligible=False,
        ),
    )
    res = client.post("/translate", json={"text": "লেভি বাড়ছে"})

    assert res.status_code == 200
    assert res.json() == {
        "language_code": "bn",
        "language_name": "Bengali",
        "text_en": "The levy is going up",
        "unintelligible": False,
        "is_english": False,
        "can_reply_in_language": True,
    }


def test_translate_computes_derived_flags(client, monkeypatch):
    """is_english / can_reply_in_language are properties, not model fields — check they serialise."""
    monkeypatch.setattr(
        W,
        "detect_and_translate",
        lambda text: Translation(
            language_code="fr", language_name="French", text_en="hi", unintelligible=False
        ),
    )
    body = client.post("/translate", json={"text": "salut"}).json()
    assert body["is_english"] is False
    assert body["can_reply_in_language"] is False


def test_translate_empty_text_is_400(client):
    """400 vs 502 is the distinction Node degrades on: unusable message, not broken pipeline."""
    res = client.post("/translate", json={"text": "   "})
    assert res.status_code == 400


def test_translate_missing_field_is_422(client):
    assert client.post("/translate", json={}).status_code == 422


@pytest.mark.parametrize("exc", [api_status_error, api_connection_error])
def test_translate_upstream_failure_is_502(client, monkeypatch, exc):
    def boom(text):
        raise exc()

    monkeypatch.setattr(W, "detect_and_translate", boom)
    assert client.post("/translate", json={"text": "hi"}).status_code == 502


def test_translate_does_not_echo_content_on_failure(client, monkeypatch):
    """policy.md §11 — the error body must not leak the message back out."""
    secret = "my passport number is A1234567"

    def boom(text):
        raise api_status_error(429)

    monkeypatch.setattr(W, "detect_and_translate", boom)
    res = client.post("/translate", json={"text": secret})
    assert "passport" not in res.text.lower()


# --------------------------------------------------------------------------------
# /transcribe — the voice feature
# --------------------------------------------------------------------------------


def test_transcribe_happy_path(client, monkeypatch):
    monkeypatch.setattr(W.asr, "transcribe", lambda path: transcript())
    monkeypatch.setattr(
        W,
        "translate_transcript",
        lambda text, target: VoiceTranslation(
            language_code="id",
            language_name="Indonesian",
            text_en="The levy is going up to 800 dollars",
            text_target="லெவி 800 டாலராக உயர்கிறது",
            unintelligible=False,
        ),
    )
    res = client.post("/transcribe", files=upload(), data={"target_language": "ta"})

    assert res.status_code == 200
    body = res.json()
    assert body["is_confident"] is True
    assert body["transcript"] == "Levy naik jadi 800 dolar"
    assert body["text_en"] == "The levy is going up to 800 dollars"
    assert body["text_target"] == "லெவி 800 டாலராக உயர்கிறது"
    assert body["target_language"] == "ta"
    assert body["duration_seconds"] == 3.2
    assert body["whisper_model"] == "base"


def test_gate_1_failure_skips_translation_entirely(client, monkeypatch):
    """The behaviour this whole design exists for.

    A transcript Whisper is not confident about must come back untranslated — both
    so the worker is asked to re-record instead of being shown a mis-hearing, and so
    we do not pay Claude to translate noise.
    """
    monkeypatch.setattr(
        W.asr, "transcribe", lambda path: transcript(mean_logprob=-0.95, text="uh ... eight?")
    )

    called = False

    def must_not_run(text, target):
        nonlocal called
        called = True
        raise AssertionError("translation ran on a transcript that failed gate 1")

    monkeypatch.setattr(W, "translate_transcript", must_not_run)
    res = client.post("/transcribe", files=upload(), data={"target_language": "en"})

    assert res.status_code == 200, "gate 1 is an abstention, not an error"
    body = res.json()
    assert body["is_confident"] is False
    assert body["text_en"] is None
    assert body["text_target"] is None
    assert body["target_language"] is None
    assert body["transcript"] == "uh ... eight?", "the caller still gets what was heard"
    assert not called


def test_claude_language_overrides_whisper(client, monkeypatch):
    """Whisper guesses from audio; Claude reads a whole sentence. Claude wins."""
    monkeypatch.setattr(W.asr, "transcribe", lambda path: transcript(language="ms"))
    monkeypatch.setattr(
        W,
        "translate_transcript",
        lambda text, target: VoiceTranslation(
            language_code="id",
            language_name="Indonesian",
            text_en="x",
            text_target="x",
            unintelligible=False,
        ),
    )
    body = client.post("/transcribe", files=upload(), data={"target_language": "en"}).json()
    assert body["spoken_language"] == "id"


@pytest.mark.parametrize(
    "target, expected",
    [
        ("fr", 400),  # a real language ECHO has no voice for
        ("xx", 400),
        ("EN", 400),  # case matters — codes are lowercase
        ("", 422),  # an empty form field is dropped in transit, so it reads as missing
    ],
)
def test_transcribe_rejects_unsupported_target_language(client, monkeypatch, target, expected):
    """Validate before loading Whisper — the model load is the expensive part."""

    def must_not_run(path):
        raise AssertionError("loaded Whisper before validating the target language")

    monkeypatch.setattr(W.asr, "transcribe", must_not_run)
    res = client.post("/transcribe", files=upload(), data={"target_language": target})
    assert res.status_code == expected


def test_transcribe_rejects_empty_audio(client, monkeypatch):
    def must_not_run(path):
        raise AssertionError("ran Whisper on an empty file")

    monkeypatch.setattr(W.asr, "transcribe", must_not_run)
    res = client.post("/transcribe", files=upload(data=b""), data={"target_language": "en"})
    assert res.status_code == 400


def test_undecodable_audio_is_422(client, monkeypatch):
    def boom(path):
        raise RuntimeError("Invalid data found when processing input")

    monkeypatch.setattr(W.asr, "transcribe", boom)
    res = client.post("/transcribe", files=upload(), data={"target_language": "en"})
    assert res.status_code == 422


@pytest.mark.parametrize("exc", [api_status_error, api_connection_error])
def test_transcribe_upstream_failure_is_502(client, monkeypatch, exc):
    monkeypatch.setattr(W.asr, "transcribe", lambda path: transcript())

    def boom(text, target):
        raise exc()

    monkeypatch.setattr(W, "translate_transcript", boom)
    res = client.post("/transcribe", files=upload(), data={"target_language": "en"})
    assert res.status_code == 502


# --------------------------------------------------------------------------------
# Voice notes are processed and discarded (policy.md §11)
# --------------------------------------------------------------------------------


def test_audio_is_written_with_the_right_extension(client, monkeypatch):
    """Whisper's decoder is hinted by the suffix, so the upload's extension must survive."""
    seen = {}

    def capture(path):
        seen["path"] = path
        return transcript()

    monkeypatch.setattr(W.asr, "transcribe", capture)
    monkeypatch.setattr(
        W,
        "translate_transcript",
        lambda text, target: VoiceTranslation(
            language_code="en", language_name="English", text_en="x", text_target="x",
            unintelligible=False,
        ),
    )
    client.post("/transcribe", files=upload(name="voice.m4a"), data={"target_language": "en"})
    assert seen["path"].endswith(".m4a")


def test_temp_file_is_deleted_after_success(client, monkeypatch):
    seen = {}

    def capture(path):
        seen["path"] = path
        assert os.path.exists(path), "Whisper was handed a path that does not exist"
        return transcript()

    monkeypatch.setattr(W.asr, "transcribe", capture)
    monkeypatch.setattr(
        W,
        "translate_transcript",
        lambda text, target: VoiceTranslation(
            language_code="en", language_name="English", text_en="x", text_target="x",
            unintelligible=False,
        ),
    )
    client.post("/transcribe", files=upload(), data={"target_language": "en"})
    assert not os.path.exists(seen["path"]), "voice note left on disk after transcription"


def test_temp_file_is_deleted_when_decoding_fails(client, monkeypatch):
    """The failure path is the one that leaks, so it gets its own test."""
    seen = {}

    def boom(path):
        seen["path"] = path
        raise RuntimeError("corrupt")

    monkeypatch.setattr(W.asr, "transcribe", boom)
    client.post("/transcribe", files=upload(), data={"target_language": "en"})
    assert not os.path.exists(seen["path"]), "voice note left on disk after a decode failure"


# --------------------------------------------------------------------------------
# /extract — the image feature
# --------------------------------------------------------------------------------


def extraction(**overrides) -> Extraction:
    base = dict(
        has_text=True,
        text="Kerja di Singapura! Gaji $4,800 sebulan.",
        language_code="id",
        language_name="Indonesian",
        confidence=0.93,
    )
    return Extraction(**{**base, **overrides})


def image_upload(name="photo.jpg", data=b"\xff\xd8\xff-fake-jpeg", content_type="image/jpeg"):
    return {"file": (name, io.BytesIO(data), content_type)}


def image_translation(**overrides) -> VoiceTranslation:
    base = dict(
        language_code="id",
        language_name="Indonesian",
        text_en="Work in Singapore! Salary $4,800 a month.",
        text_target="সিঙ্গাপুরে কাজ! বেতন $4,800 প্রতি মাসে।",
        unintelligible=False,
    )
    return VoiceTranslation(**{**base, **overrides})


def test_extract_happy_path(client, monkeypatch):
    monkeypatch.setattr(W.vision, "extract_text", lambda data, mt: extraction())
    monkeypatch.setattr(W, "translate_transcript", lambda t, target, source: image_translation())

    res = client.post("/extract", files=image_upload(), data={"target_language": "bn"})

    assert res.status_code == 200
    body = res.json()
    assert body["untranscribable"] is False
    assert body["has_text"] is True
    assert body["text_source"] == "Kerja di Singapura! Gaji $4,800 sebulan."
    assert body["text_en"] == "Work in Singapore! Salary $4,800 a month."
    assert body["text_target"].startswith("সিঙ্গাপুরে")
    assert body["target_language"] == "bn"
    assert body["confidence"] == 0.93


def test_extract_passes_the_image_source_to_the_translator(client, monkeypatch):
    """Image text fails differently from a voice transcript; the prompt must know which."""
    seen = {}

    def capture(text, target, source):
        seen["source"] = source
        return image_translation()

    monkeypatch.setattr(W.vision, "extract_text", lambda data, mt: extraction())
    monkeypatch.setattr(W, "translate_transcript", capture)

    client.post("/extract", files=image_upload(), data={"target_language": "en"})
    assert seen["source"] == "image"


def test_extract_forwards_the_bytes_and_media_type(client, monkeypatch):
    seen = {}

    def capture(data, media_type):
        seen["data"], seen["media_type"] = data, media_type
        return extraction()

    monkeypatch.setattr(W.vision, "extract_text", capture)
    monkeypatch.setattr(W, "translate_transcript", lambda t, target, source: image_translation())

    client.post(
        "/extract",
        files=image_upload(data=b"exact-bytes", content_type="image/png"),
        data={"target_language": "en"},
    )
    assert seen["data"] == b"exact-bytes"
    assert seen["media_type"] == "image/png"


def test_unreadable_image_skips_translation_entirely(client, monkeypatch):
    """The image counterpart of the gate-1 short circuit.

    A photo the model couldn't read must come back untranslated — so the worker is
    asked for a clearer picture instead of being shown a guess, and so we don't pay
    to translate a misreading.
    """
    monkeypatch.setattr(
        W.vision, "extract_text", lambda data, mt: extraction(confidence=0.25, text="L[unclear]vy")
    )

    def must_not_run(text, target, source):
        raise AssertionError("translation ran on an image that failed the gate")

    monkeypatch.setattr(W, "translate_transcript", must_not_run)

    res = client.post("/extract", files=image_upload(), data={"target_language": "en"})

    assert res.status_code == 200, "abstention is a 200 with untranscribable True, not an error"
    body = res.json()
    assert body["untranscribable"] is True
    assert body["text_en"] is None
    assert body["text_target"] is None
    assert body["target_language"] is None
    assert body["has_text"] is True, "there was text; we just couldn't read it"


def test_image_with_no_text_is_distinguishable_from_an_unreadable_one(client, monkeypatch):
    """Different replies: 'no writing here' vs 'send a sharper photo'."""
    monkeypatch.setattr(
        W.vision,
        "extract_text",
        lambda data, mt: extraction(has_text=False, text="", confidence=0.0, language_code="und"),
    )

    def must_not_run(text, target, source):
        raise AssertionError("translated an image with no text in it")

    monkeypatch.setattr(W, "translate_transcript", must_not_run)

    body = client.post("/extract", files=image_upload(), data={"target_language": "en"}).json()
    assert body["has_text"] is False
    assert body["untranscribable"] is True
    assert body["text_en"] is None


def test_translator_language_overrides_the_extraction(client, monkeypatch):
    """The extraction pass sees fragments; the translator sees the whole passage."""
    monkeypatch.setattr(W.vision, "extract_text", lambda data, mt: extraction(language_code="ms"))
    monkeypatch.setattr(
        W, "translate_transcript", lambda t, target, source: image_translation(language_code="id")
    )
    body = client.post("/extract", files=image_upload(), data={"target_language": "en"}).json()
    assert body["detected_language"] == "id"


@pytest.mark.parametrize(
    "target, expected",
    [("fr", 400), ("xx", 400), ("EN", 400), ("", 422)],
)
def test_extract_rejects_unsupported_target_language(client, monkeypatch, target, expected):
    def must_not_run(data, media_type):
        raise AssertionError("called the vision API before validating the target language")

    monkeypatch.setattr(W.vision, "extract_text", must_not_run)
    res = client.post("/extract", files=image_upload(), data={"target_language": target})
    assert res.status_code == expected


def test_extract_rejects_empty_image(client, monkeypatch):
    def must_not_run(data, media_type):
        raise AssertionError("called the vision API with an empty image")

    monkeypatch.setattr(W.vision, "extract_text", must_not_run)
    res = client.post(
        "/extract", files=image_upload(data=b""), data={"target_language": "en"}
    )
    assert res.status_code == 400


@pytest.mark.parametrize("detail", ["unsupported image type 'application/pdf'", "limit is 5120 KB"])
def test_bad_image_is_400_not_500(client, monkeypatch, detail):
    """vision.extract_text raises ValueError for both; it must surface as a client error."""

    def boom(data, media_type):
        raise ValueError(detail)

    monkeypatch.setattr(W.vision, "extract_text", boom)
    res = client.post("/extract", files=image_upload(), data={"target_language": "en"})
    assert res.status_code == 400


@pytest.mark.parametrize("exc", [api_status_error, api_connection_error])
def test_extract_upstream_failure_is_502(client, monkeypatch, exc):
    def boom(data, media_type):
        raise exc()

    monkeypatch.setattr(W.vision, "extract_text", boom)
    res = client.post("/extract", files=image_upload(), data={"target_language": "en"})
    assert res.status_code == 502


@pytest.mark.parametrize("exc", [api_status_error, api_connection_error])
def test_extract_translation_failure_is_502(client, monkeypatch, exc):
    """The second stage can fail independently of the first."""
    monkeypatch.setattr(W.vision, "extract_text", lambda data, mt: extraction())

    def boom(text, target, source):
        raise exc()

    monkeypatch.setattr(W, "translate_transcript", boom)
    res = client.post("/extract", files=image_upload(), data={"target_language": "en"})
    assert res.status_code == 502


def test_extract_does_not_echo_content_on_failure(client, monkeypatch):
    """policy.md §11 — an image can hold a passport number; errors must not leak it."""

    def boom(data, media_type):
        raise api_status_error(429)

    monkeypatch.setattr(W.vision, "extract_text", boom)
    res = client.post(
        "/extract", files=image_upload(name="passport-A1234567.jpg"), data={"target_language": "en"}
    )
    assert "A1234567" not in res.text
