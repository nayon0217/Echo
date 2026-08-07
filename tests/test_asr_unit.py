"""Abstention gate 1 (policy.md §7), tested as pure logic.

`Transcript.is_confident` is the single decision that stops a mis-heard voice note
from being verified as though it were understood. policy.md §9 calls that failure
mode catastrophic, so it gets exhaustive coverage here — no audio, no model, just
the predicate.
"""

from __future__ import annotations

import pytest

from pipeline.asr import MAX_NO_SPEECH_PROB, MIN_MEAN_LOGPROB, Transcript, model_name


def make(**overrides) -> Transcript:
    """A transcript that comfortably passes the gate, unless overridden."""
    base = dict(
        text="the levy is going up to 800 dollars",
        language="en",
        language_probability=0.99,
        mean_logprob=-0.2,
        max_no_speech_prob=0.01,
        duration=3.0,
        segment_count=1,
        model="base",
    )
    return Transcript(**{**base, **overrides})


def test_clear_speech_passes():
    assert make().is_confident


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"text": ""}, "empty transcript"),
        ({"text": "   "}, "whitespace-only transcript"),
        ({"mean_logprob": MIN_MEAN_LOGPROB - 0.01}, "logprob below threshold"),
        ({"mean_logprob": -10.0}, "the no-segments sentinel"),
        ({"max_no_speech_prob": MAX_NO_SPEECH_PROB + 0.01}, "probably not speech"),
        ({"max_no_speech_prob": 1.0}, "definitely not speech"),
    ],
)
def test_gate_fails_closed(overrides, why):
    assert not make(**overrides).is_confident, f"should have failed the gate: {why}"


@pytest.mark.parametrize(
    "field, value",
    [("mean_logprob", MIN_MEAN_LOGPROB), ("max_no_speech_prob", MAX_NO_SPEECH_PROB)],
)
def test_thresholds_are_inclusive(field, value):
    """Exactly at the threshold passes. Pinned so a later `>` vs `>=` edit is visible."""
    assert make(**{field: value}).is_confident


def test_one_bad_signal_is_enough_to_abstain():
    """The gate is an AND — a confident-sounding logprob can't rescue non-speech."""
    assert not make(mean_logprob=-0.05, max_no_speech_prob=0.95).is_confident


def test_model_name_honours_env(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "small")
    assert model_name() == "small"


def test_model_name_falls_back_to_policy_default(monkeypatch):
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    assert model_name() == "large-v3"
