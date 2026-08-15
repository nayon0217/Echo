"""Text to speech — the reply half of the voice-first design (specs.md §2).

specs.md is explicit that this is the point of the product: the bot replies "in the
worker's own language with a short **voice message, not text**", because "reading is
the barrier, so the entire interaction is built to never require it". Until now every
reply was text only, so the bot asked the worker to do the one thing they came here to
avoid.

**Why edge-tts rather than Puter.js.** Puter.js is a browser SDK — it ships as
`<script src="https://js.puter.com/v2/">` and authenticates against a logged-in browser
user. There is no server-side package (the `puter` npm entry is an unrelated 570-byte
stub), so using it here would mean driving a headless browser per reply. edge-tts needs
no API key, no account, and no browser, and has neural voices for all six languages
ECHO offers — which gTTS and macOS `say` do not cover between them.

**Why OGG/Opus.** WhatsApp renders an audio message as a proper voice note — waveform,
inline playback — only when it is OGG with the Opus codec. Anything else arrives as a
file attachment the worker has to open. edge-tts returns MP3, so this module transcodes
with PyAV, which faster-whisper already pulls in; no ffmpeg binary is required.

Privacy (policy.md §11): the synthesised audio is returned to the caller and never
written to disk. This module does not log reply content.
"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata

import av
import edge_tts
from av.audio.resampler import AudioResampler

# One voice per language ECHO offers (mirrors src/languages.js), chosen for the
# Singapore migrant-worker context rather than the largest speaker population:
#
#   id-ID  Bahasa Indonesia — common among Indonesian domestic and construction workers.
#   my-MM  Burmese (Myanmar) — common among Myanmar domestic and construction workers.
#   bn-BD  Bangladeshi Bengali — most Bengali-speaking workers here are from Bangladesh,
#          not West Bengal, and the two differ audibly.
#   ta-IN  Indian Tamil. ta-MY (Malaysian) is the nearer accent but a smaller voice set.
#   en-SG  Singapore English — the accent of the notices and employers being discussed.
#
# specs.md's open checklist item stands: these need validating with native speakers,
# not picked from a list. Treat them as defaults, not decisions.
VOICES = {
    "en": "en-SG-LunaNeural",
    "id": "id-ID-GadisNeural",
    "my": "my-MM-NilarNeural",
    "bn": "bn-BD-NabanitaNeural",
    "ta": "ta-IN-PallaviNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
}

# A voice note the worker has to scrub through is worse than no voice note. Replies
# above this are spoken up to the cap and truncated at a sentence boundary — the text
# message alongside always carries the whole thing.
MAX_CHARS = 900

# Roughly the audio a MAX_CHARS reply produces; a guard against a runaway synthesis.
MAX_AUDIO_BYTES = 5 * 1024 * 1024


def _strip_for_speech(text: str) -> str:
    """Remove what reads badly aloud, keeping the words themselves intact.

    Replies carry emoji (⚠️, 🔎), quote marks around contract clauses, and blank lines
    for layout. Spoken, those become either silence, a literal "warning sign", or an
    odd pause, depending on the voice. None of it is content.
    """
    # Drop emoji and other symbol/pictograph characters, but keep every letter, digit,
    # and combining mark — Bengali and Tamil rely heavily on marks, so a blanket
    # mark-stripping filter would destroy them.
    #
    # Variation selectors (U+FE00-U+FE0F) are the exception: they are categorised as
    # marks but carry no sound, and "⚠️" would otherwise leave one behind after the
    # emoji itself is removed.
    cleaned = "".join(
        ch
        for ch in text
        if unicodedata.category(ch) not in {"So", "Sk", "Cf"}
        and not ("︀" <= ch <= "️")
    )
    cleaned = cleaned.replace('"', " ").replace("«", " ").replace("»", " ")
    # A blank line is a paragraph break; a single newline is a line wrap. Both become
    # a sentence pause rather than being read as nothing.
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = cleaned.replace("\n", ". ")
    cleaned = re.sub(r"\.\s*\.(\s*\.)*", ". ", cleaned)  # collapse the runs that creates
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    """Cut to `limit`, preferring the last sentence end so it doesn't stop mid-word."""
    if len(text) <= limit:
        return text

    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("। "), window.rfind("。"))
    if cut > limit * 0.5:
        return window[: cut + 1].strip()
    return window.rsplit(" ", 1)[0].strip()


def _to_opus(mp3: bytes) -> bytes:
    """MP3 -> OGG/Opus, 48 kHz mono: the container WhatsApp treats as a voice note."""
    out_buf = io.BytesIO()
    with av.open(io.BytesIO(mp3)) as inp, av.open(out_buf, "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        resampler = AudioResampler(format="s16", layout="mono", rate=48000)

        for frame in inp.decode(audio=0):
            for resampled in resampler.resample(frame):
                resampled.pts = None
                for packet in stream.encode(resampled):
                    out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

    return out_buf.getvalue()


async def _synthesize_mp3(text: str, voice: str) -> bytes:
    buf = io.BytesIO()
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
            if buf.tell() > MAX_AUDIO_BYTES:
                raise ValueError("synthesised audio exceeded the size limit")
    return buf.getvalue()


def voice_for(language: str) -> str:
    """The configured voice for a language code. Raises ValueError if unsupported."""
    voice = VOICES.get(language)
    if voice is None:
        raise ValueError(f"unsupported language {language!r}; expected one of {sorted(VOICES)}")
    return voice


def synthesize(text: str, language: str) -> bytes:
    """Speak `text` in `language`, returning OGG/Opus bytes ready for WhatsApp.

    Raises ValueError on empty text or an unsupported language, and RuntimeError if
    synthesis fails — callers degrade to text-only rather than dropping the reply
    (see src/whatsapp.js).
    """
    voice = voice_for(language)

    spoken = _truncate(_strip_for_speech(text))
    # Non-empty isn't enough: stripping "⚠️" or a blank line can leave punctuation
    # behind. Require something actually pronounceable before paying for synthesis.
    if not any(ch.isalnum() for ch in spoken):
        raise ValueError("nothing to speak once formatting was stripped")

    try:
        mp3 = asyncio.run(_synthesize_mp3(spoken, voice))
    except ValueError:
        raise
    except Exception as exc:  # network, service, or codec failure
        raise RuntimeError(f"speech synthesis failed: {exc}") from exc

    if not mp3:
        raise RuntimeError("speech synthesis returned no audio")

    return _to_opus(mp3)


def main() -> int:
    """Speak a line of text to an .ogg file. Manual testing only."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Synthesise a WhatsApp voice note.")
    p.add_argument("text")
    p.add_argument("--language", default="en", help=f"one of {sorted(VOICES)}")
    p.add_argument("--out", default="reply.ogg")
    args = p.parse_args()

    try:
        audio = synthesize(args.text, args.language)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "wb") as fh:
        fh.write(audio)
    print(f"wrote {args.out} ({len(audio)} bytes, voice {voice_for(args.language)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
