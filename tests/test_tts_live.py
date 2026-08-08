"""Real speech synthesis for every language ECHO offers. Run with --live.

These hit the voice service and decode what comes back, because the failure that
matters is not "the call errored" — it is "the call succeeded and produced audio the
worker cannot use". A voice note in the wrong container arrives as a file attachment;
one with no audio in it arrives as a broken bubble. Both look like success from the
HTTP layer.

Cheap: no API key, no per-call cost. Slow enough to be opt-in.
"""

from __future__ import annotations

import io

import av
import pytest
from fastapi.testclient import TestClient

from app.webhook import app
from pipeline import tts

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def probe(audio: bytes) -> dict:
    """Decode the clip and report what WhatsApp would actually receive."""
    with av.open(io.BytesIO(audio)) as container:
        stream = container.streams.audio[0]
        frames = sum(1 for _ in container.decode(audio=0))
        return {
            "codec": stream.codec_context.name,
            "rate": stream.codec_context.sample_rate,
            "channels": stream.codec_context.layout.nb_channels,
            "seconds": float(container.duration or 0) / 1_000_000,
            "frames": frames,
        }


# --------------------------------------------------------------------------------
# Every language the menu offers
# --------------------------------------------------------------------------------

SAMPLES = {
    "en": "Your basic salary is 800 dollars per month. Do not send money to anyone.",
    "bn": "আপনার মূল বেতন প্রতি মাসে ৮০০ ডলার। কাউকে টাকা পাঠাবেন না।",
    "ta": "உங்கள் அடிப்படை சம்பளம் மாதம் 800 டாலர். யாருக்கும் பணம் அனுப்ப வேண்டாம்.",
    "zh": "您的基本工资是每月800新元。不要给任何人汇款。",
}


@pytest.mark.parametrize("code", ["en", "bn", "ta", "zh"])
def test_speaks_every_offered_language(code):
    audio = tts.synthesize(SAMPLES[code], code)

    assert audio, f"no audio produced for {code}"
    info = probe(audio)

    # WhatsApp renders a voice note only for OGG/Opus; anything else is a file.
    assert info["codec"] == "opus", f"{code} produced {info['codec']}, not opus"
    assert info["rate"] == 48000
    assert info["channels"] == 1
    assert info["frames"] > 0, f"{code} produced a container with no audio in it"
    assert info["seconds"] > 0.5, f"{code} produced only {info['seconds']:.2f}s"


@pytest.mark.parametrize("code", ["en", "bn", "ta", "zh"])
def test_duration_is_proportionate_to_the_text(code):
    """A clip far shorter than the text usually means the voice skipped the script."""
    audio = tts.synthesize(SAMPLES[code], code)
    seconds = probe(audio)["seconds"]
    assert 1.5 < seconds < 40, f"{code}: {seconds:.2f}s for {len(SAMPLES[code])} chars"


# --------------------------------------------------------------------------------
# Text preparation, end to end
# --------------------------------------------------------------------------------


def test_a_reply_full_of_formatting_still_speaks():
    """The real shape of a scam warning: emoji, blank lines, a quoted clause."""
    reply = (
        "⚠️ Possible scam.\n\n"
        'Your contract says:\n"Basic salary: SGD 650 per month."\n\n'
        "Do not send money or click links. If unsure, call 1800 339 5505."
    )
    info = probe(tts.synthesize(reply, "en"))
    assert info["frames"] > 0
    assert info["seconds"] > 3


def test_a_very_long_reply_is_capped_not_rejected():
    """A worker should get a usable clip, not silence, when an answer runs long."""
    long_reply = "This is a sentence about your employment contract. " * 60
    info = probe(tts.synthesize(long_reply, "en"))

    assert info["frames"] > 0
    # MAX_CHARS at a normal speaking rate; generous upper bound, but far below the
    # ~5 minutes the untruncated text would produce.
    assert info["seconds"] < 90, f"cap did not hold: {info['seconds']:.1f}s"


@pytest.mark.parametrize("junk", ["⚠️", "   ", '"""'])
def test_nothing_speakable_is_rejected_before_the_network_call(junk):
    with pytest.raises(ValueError, match="nothing to speak"):
        tts.synthesize(junk, "en")


# --------------------------------------------------------------------------------
# Through the HTTP route
# --------------------------------------------------------------------------------


def test_speak_endpoint_returns_usable_audio(client):
    res = client.post("/speak", json={"text": SAMPLES["bn"], "language": "bn"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("audio/ogg")

    info = probe(res.content)
    assert info["codec"] == "opus"
    assert info["frames"] > 0


def test_speak_endpoint_rejects_a_language_with_no_voice(client):
    assert client.post("/speak", json={"text": "hello", "language": "fr"}).status_code == 400


def test_each_language_uses_a_distinct_voice(client):
    """A mismatched voice reads the script phonetically and is unintelligible."""
    clips = {
        code: client.post("/speak", json={"text": SAMPLES[code], "language": code}).content
        for code in ["en", "bn", "ta", "zh"]
    }
    assert len(set(clips.values())) == 4, "two languages produced identical audio"
