"""pipeline/tts.py — voice selection, text preparation, and the size guard.

No network and no synthesis here. What these pin is everything that happens to the
reply *before* it reaches the voice service: emoji and layout removed, length capped at
a sentence boundary, unsupported languages rejected. test_tts_live.py does the real
synthesis.
"""

from __future__ import annotations

import pytest

from pipeline import tts


# --------------------------------------------------------------------------------
# Voice selection
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["en", "id", "my", "bn", "ta", "zh"])
def test_every_offered_language_has_a_voice(code):
    assert tts.voice_for(code)


def test_voices_cover_exactly_the_language_menu():
    """src/languages.js is the source of truth — a menu option with no voice is silent."""
    import re
    from pathlib import Path

    js = (Path(__file__).parent.parent / "src" / "languages.js").read_text()
    menu_codes = set(re.findall(r'code:\s*"([a-z]{2})"', js))

    assert menu_codes, "could not parse language codes out of src/languages.js"
    missing = menu_codes - set(tts.VOICES)
    assert not missing, f"menu offers {sorted(missing)} with no configured voice"


@pytest.mark.parametrize("code", ["fr", "xx", "EN", "", "ms"])
def test_unsupported_languages_are_rejected(code):
    """Malay (ms) is not offered — workers who need it use Bahasa Indonesia (id)."""
    with pytest.raises(ValueError, match="unsupported language"):
        tts.voice_for(code)


def test_indonesian_uses_an_id_id_voice():
    assert tts.VOICES["id"].startswith("id-ID")


def test_burmese_uses_a_my_mm_voice():
    assert tts.VOICES["my"].startswith("my-MM")


def test_bengali_uses_the_bangladeshi_voice():
    """Most Bengali-speaking workers in Singapore are Bangladeshi; bn-IN sounds wrong."""
    assert tts.VOICES["bn"].startswith("bn-BD")


# --------------------------------------------------------------------------------
# Preparing text for speech
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, absent",
    [
        ("⚠️ Possible scam.", "⚠"),
        ("🔎 Checking your message…", "🔎"),
        ('Your contract says:\n"Basic salary: SGD 800"', '"'),
    ],
)
def test_symbols_and_quote_marks_are_stripped(raw, absent):
    """Spoken, these become either silence or a literal 'warning sign'."""
    assert absent not in tts._strip_for_speech(raw)


def test_the_words_survive_stripping():
    out = tts._strip_for_speech("⚠️ Possible scam.\n\nDo not send money.")
    assert "Possible scam" in out
    assert "Do not send money" in out


@pytest.mark.parametrize(
    "script, sample",
    [
        ("bengali", "আপনার মূল বেতন প্রতি মাসে ৮০০ ডলার"),
        ("tamil", "உங்கள் அடிப்படை சம்பளம் மாதம் 800 டாலர்"),
        ("chinese", "您的基本工资是每月800新元"),
    ],
)
def test_non_latin_scripts_are_not_mangled(script, sample):
    """Bengali and Tamil lean on combining marks; a naive filter would strip them."""
    assert tts._strip_for_speech(sample) == sample


def test_layout_becomes_pauses_not_run_on_text():
    out = tts._strip_for_speech("Line one.\n\nLine two.\nLine three.")
    assert "\n" not in out
    assert "Line one" in out and "Line three" in out
    assert ".." not in out, "collapsing newlines must not produce stutters"


def test_numbers_and_amounts_are_preserved():
    """The figures are the whole point — they must reach the voice intact."""
    out = tts._strip_for_speech("Salary SGD 4,800 and a $1,200 fee before 15 March.")
    for token in ["4,800", "1,200", "15 March"]:
        assert token in out


# --------------------------------------------------------------------------------
# Length cap
# --------------------------------------------------------------------------------


def test_short_text_is_untouched():
    assert tts._truncate("Short reply.") == "Short reply."


def test_long_text_is_capped():
    assert len(tts._truncate("word " * 500)) <= tts.MAX_CHARS


def test_truncation_prefers_a_sentence_boundary():
    text = ("This is a complete sentence. " * 60).strip()
    out = tts._truncate(text)
    assert out.endswith("."), f"cut mid-sentence: {out[-40:]!r}"


def test_truncation_never_cuts_mid_word():
    out = tts._truncate("supercalifragilistic " * 200)
    assert not out.endswith("supercalifragilisti")
    assert out == out.strip()


def test_a_long_unpunctuated_reply_still_gets_capped():
    """No sentence boundary to find — must still fall back to a word boundary."""
    out = tts._truncate("word " * 500)
    assert len(out) <= tts.MAX_CHARS
    assert " " not in out[-1:]


# --------------------------------------------------------------------------------
# Input validation — before any network call
# --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("validation should have rejected this before synthesising")

    monkeypatch.setattr(tts, "_synthesize_mp3", boom)


@pytest.mark.parametrize("bad", ["fr", "xx", ""])
def test_synthesize_rejects_unsupported_language(bad):
    with pytest.raises(ValueError, match="unsupported language"):
        tts.synthesize("hello", bad)


@pytest.mark.parametrize("bad", ["", "   ", "⚠️", '"""', "\n\n"])
def test_synthesize_rejects_text_with_nothing_to_say(bad):
    """A reply that is only emoji or punctuation has no audio worth sending."""
    with pytest.raises(ValueError, match="nothing to speak"):
        tts.synthesize(bad, "en")
