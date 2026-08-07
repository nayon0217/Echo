"""The voice feature end to end, minus WhatsApp. Run with --live.

Real synthesised audio -> real Whisper -> real Claude, driven through the actual
FastAPI route. Everything the Node layer would do is here except the two Meta calls
(download the media id, send the reply), so a pass means the pipeline half of a voice
note works and only the WhatsApp plumbing is unverified.

Assertions are deliberately loose on wording. These clips are text-to-speech through
the `base` model, which mishears homophones — it renders "levy" as "levee". What must
survive is the number and the meaning, since those are what verification checks.
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.webhook import app

pytestmark = pytest.mark.live

LEVY_EN = "Is it true that the levy is going up to 800 dollars next month?"
LEVY_ID = "Apakah benar levy naik jadi 800 dolar bulan depan?"

# Number fidelity on non-English audio is model-dependent, and the gap is not subtle:
# on `base`, the Indonesian clip above transcribes as "jadi $889 depan" — 800 becomes
# 889 and "bulan" is dropped — while still clearing gate 1 at mean_logprob -0.593
# against a -0.6 threshold. policy.md §2 specifies large-v3 for exactly this reason.
# Asserting numbers on non-English `base` output would be a flaky test, so those
# assertions are gated on running the model the policy actually calls for.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
STRICT_NUMBERS = WHISPER_MODEL.startswith("large")
needs_large = pytest.mark.skipif(
    not STRICT_NUMBERS,
    reason=f"number fidelity on non-English audio needs large-v3; running {WHISPER_MODEL!r}",
)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def post(client, path: str, target: str):
    with open(path, "rb") as fh:
        return client.post(
            "/transcribe",
            files={"file": ("voice.ogg", fh, "audio/ogg; codecs=opus")},
            data={"target_language": target},
        )


def test_english_voice_note_end_to_end(client, voice_clip):
    res = post(client, voice_clip(LEVY_EN, "en"), "en")
    assert res.status_code == 200

    body = res.json()
    assert body["is_confident"], f"clear speech failed gate 1: {body}"
    assert body["spoken_language"] == "en"
    assert body["duration_seconds"] > 0
    assert "800" in body["transcript"]
    assert "800" in body["text_en"]
    assert "800" in body["text_target"]
    assert body["target_language"] == "en"
    assert not body["unintelligible"]


def test_confidence_signals_are_reported(client, voice_clip):
    """The numbers behind gate 1 — the Node layer logs them when it abstains."""
    body = post(client, voice_clip(LEVY_EN, "en"), "en").json()

    assert -1.0 < body["mean_logprob"] <= 0.0
    assert 0.0 <= body["language_probability"] <= 1.0
    assert 0.0 <= body["max_no_speech_prob"] <= 1.0
    assert body["whisper_model"]


def test_indonesian_voice_note_replies_in_the_chosen_language(client, voice_clip):
    """The realistic case: spoken in one language, answered in the one the worker picked.

    This is about routing, not transcription accuracy — see the number-fidelity test
    below for that.
    """
    res = post(client, voice_clip(LEVY_ID, "id"), "ta")
    assert res.status_code == 200

    body = res.json()
    assert body["is_confident"], f"failed gate 1: {body}"
    # 'id' or 'ms' — see the note in test_translate_live.py; the pair is genuinely
    # ambiguous for a sentence this short, and the reply language does not depend on it.
    assert body["spoken_language"] in {"id", "ms"}
    assert body["text_en"].strip()
    assert body["target_language"] == "ta"
    assert any("஀" <= ch <= "௿" for ch in body["text_target"]), (
        f"worker chose Tamil but got:\n{body['text_target']}"
    )


@needs_large
def test_numbers_survive_non_english_audio(client, voice_clip):
    """The amount is the claim. If ASR corrupts it, everything downstream verifies a fiction."""
    body = post(client, voice_clip(LEVY_ID, "id"), "en").json()
    assert body["is_confident"], f"failed gate 1: {body}"
    assert "800" in body["transcript"], f"ASR corrupted the amount:\n{body['transcript']}"
    assert "800" in body["text_en"]


def test_chinese_voice_note(client, voice_clip):
    res = post(client, voice_clip("下个月人头税要涨到800块，是真的吗？", "zh"), "en")
    assert res.status_code == 200

    body = res.json()
    assert body["is_confident"], f"failed gate 1: {body}"
    assert body["spoken_language"] == "zh"
    assert body["text_en"].strip()
    if STRICT_NUMBERS:
        assert "800" in body["text_en"]


def test_transcript_stays_in_the_spoken_language(client, voice_clip):
    """asr.py deliberately does not use Whisper's translate task — the worker can be
    shown what was actually said. A transcript that came back in English would mean
    that regressed."""
    body = post(client, voice_clip(LEVY_ID, "id"), "en").json()
    assert body["is_confident"]
    assert re.search(r"benar|levy|naik|dolar|bulan", body["transcript"], re.I), (
        f"transcript does not look Indonesian:\n{body['transcript']}"
    )


def test_silence_fails_gate_1_and_is_not_translated(client, silent_clip):
    """The abstention that protects everything downstream (policy.md §7, §9)."""
    res = post(client, silent_clip, "en")
    assert res.status_code == 200, "abstention is a 200 with is_confident False, not an error"

    body = res.json()
    assert body["is_confident"] is False, f"silence passed gate 1: {body}"
    assert body["text_en"] is None
    assert body["text_target"] is None


def test_numbers_survive_the_whole_chain(client, voice_clip):
    """Two amounts and a deadline spoken aloud, through ASR and translation intact."""
    body = post(
        client,
        voice_clip(
            "The agent says pay 1200 dollars before March 15th and the salary is 4800 a month.",
            "en",
        ),
        "en",
    ).json()

    assert body["is_confident"], f"failed gate 1: {body}"
    assert re.search(r"1,?200", body["text_en"]), body["text_en"]
    assert re.search(r"4,?800", body["text_en"]), body["text_en"]
    assert "15" in body["text_en"]


def test_unsupported_target_language_is_rejected_before_whisper(client, voice_clip):
    res = post(client, voice_clip(LEVY_EN, "en"), "fr")
    assert res.status_code == 400
