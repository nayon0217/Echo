"""Stage 1 of the ECHO pipeline: speech to text (policy.md §1, §2).

faster-whisper (CTranslate2) rather than reference Whisper — same weights, ~4x
faster, and it exposes per-segment average log-probability. That last part is not
incidental: policy.md §7 abstention gate 1 is "ASR mean logprob below threshold →
don't verify at all, ask for a re-record". It is the only genuine model-confidence
signal anywhere in this pipeline, so `Transcript.mean_logprob` is load-bearing
rather than diagnostic.

WhatsApp voice notes arrive as OGG/Opus. faster-whisper decodes via PyAV, so no
ffmpeg binary is required.

The model is loaded lazily and cached — the first call pays the download (large-v3
is ~3 GB) and load; later calls reuse it. Set WHISPER_MODEL=small while iterating.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from faster_whisper import WhisperModel

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # dotenv is optional at runtime
    pass


# policy.md §2 specifies large-v3. Override for faster local iteration.
DEFAULT_MODEL = "large-v3"

# Threshold for abstention gate 1. Whisper's avg_logprob is roughly -0.1 (very
# confident) to -1.0 (garbage); -0.6 is a defensible starting line, but policy.md §7
# is explicit that gates get tuned on the golden set rather than guessed — treat this
# as a placeholder until phase 4.
MIN_MEAN_LOGPROB = -0.6

# Above this, Whisper thinks the audio is probably not speech at all.
MAX_NO_SPEECH_PROB = 0.6


@dataclass
class Transcript:
    """What Whisper heard, plus the confidence signals the gates need."""

    text: str
    #: Whisper's own language guess (ISO 639-1). Independent of Claude's detection,
    #: so the two can be cross-checked.
    language: str
    #: Whisper's confidence in that language guess, 0-1.
    language_probability: float
    #: Token-weighted mean of per-segment avg_logprob. Higher (closer to 0) is better.
    mean_logprob: float
    #: Highest no-speech probability across segments.
    max_no_speech_prob: float
    #: Audio length in seconds.
    duration: float
    segment_count: int = 0
    model: str = field(default="")

    @property
    def is_confident(self) -> bool:
        """Whether this transcript clears abstention gate 1 (policy.md §7)."""
        return (
            bool(self.text.strip())
            and self.mean_logprob >= MIN_MEAN_LOGPROB
            and self.max_no_speech_prob <= MAX_NO_SPEECH_PROB
        )


def model_name() -> str:
    return os.getenv("WHISPER_MODEL") or DEFAULT_MODEL


@lru_cache(maxsize=1)
def _model() -> WhisperModel:
    """Load the Whisper model once per process.

    int8 on CPU: large-v3 in float32 is far too slow on a laptop, and quantisation
    costs little accuracy for this workload.
    """
    name = model_name()
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    print(f"[asr] loading whisper '{name}' ({device}/{compute_type})…")
    return WhisperModel(name, device=device, compute_type=compute_type)


def transcribe(audio_path: str, language: str | None = None) -> Transcript:
    """Transcribe an audio file in its original language.

    Deliberately does NOT use Whisper's translate task: policy.md wants translation
    done by the LLM (§2), and keeping the transcript in the source language means
    the worker can be shown what was actually said.

    `language` forces a language when it is already known; leaving it None lets
    Whisper detect it, which is what the WhatsApp path does.
    """
    segments, info = _model().transcribe(
        audio_path,
        language=language,
        task="transcribe",
        # Voice notes are often noisy, in traffic, or half-whispered. VAD trims the
        # silence Whisper would otherwise hallucinate words into.
        vad_filter=True,
        beam_size=5,
    )

    # `segments` is a generator — nothing runs until it is consumed.
    collected = list(segments)

    parts: list[str] = []
    weighted_logprob = 0.0
    total_tokens = 0
    max_no_speech = 0.0

    for seg in collected:
        parts.append(seg.text)
        # Weight by token count so one stray two-word segment can't drag the mean.
        n_tokens = len(seg.tokens) if seg.tokens else 1
        weighted_logprob += seg.avg_logprob * n_tokens
        total_tokens += n_tokens
        max_no_speech = max(max_no_speech, seg.no_speech_prob or 0.0)

    mean_logprob = (weighted_logprob / total_tokens) if total_tokens else -10.0

    return Transcript(
        text="".join(parts).strip(),
        language=info.language or "und",
        language_probability=info.language_probability or 0.0,
        mean_logprob=mean_logprob,
        max_no_speech_prob=max_no_speech,
        duration=info.duration or 0.0,
        segment_count=len(collected),
        model=model_name(),
    )


def main() -> int:
    """Transcribe an audio file given on the command line. Manual testing only."""
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="Transcribe an audio file with Whisper.")
    p.add_argument("audio", help="path to an audio file")
    p.add_argument("--language", help="force a language instead of detecting")
    args = p.parse_args()

    try:
        t = transcribe(args.audio, language=args.language)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "text": t.text,
                "language": t.language,
                "language_probability": round(t.language_probability, 3),
                "mean_logprob": round(t.mean_logprob, 3),
                "max_no_speech_prob": round(t.max_no_speech_prob, 3),
                "duration": round(t.duration, 2),
                "segments": t.segment_count,
                "is_confident": t.is_confident,
                "model": t.model,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
