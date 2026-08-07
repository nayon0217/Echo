"""Shared fixtures.

Two tiers of test live here, and the split matters:

- **Unit** (default): no network, no model weights, sub-second. Every external edge
  is faked, so these check *our* logic — routing, gates, error mapping, cleanup.
- **Live** (`--live`): the real Claude API and the real Whisper weights. These are
  the ones that answer "does the feature actually work", which is what you cannot
  check without a WhatsApp webhook. They cost money and take minutes.

Voice-note fixtures are synthesised with macOS `say`, then transcoded to OGG/Opus
with PyAV — byte-for-byte the container WhatsApp sends. No ffmpeg needed, and no
committed audio blobs. Clips are cached in tests/fixtures/audio/ so a second run
skips synthesis.

Image fixtures are rendered with Pillow into JPEG/PNG, and can be degraded on demand
(blur, downscale, rotate) to exercise the extraction gate — the point of a synthetic
image here is that we know exactly what text is in it, so an assertion about what was
read is meaningful.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "audio"

# macOS voices, one per language ECHO deals with. `say -v '?'` lists what is installed;
# there is no Bengali voice on stock macOS, so bn is exercised as text only.
VOICES = {
    "en": "Samantha",
    "id": "Damayanti",
    "zh": "Tingting",
    "ta": "Vani",
}


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that call the real Claude API and load real Whisper weights",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live (real Claude API / Whisper weights)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live(request) -> bool:
    return bool(request.config.getoption("--live"))


@pytest.fixture(scope="session", autouse=True)
def _whisper_model_guard(request):
    """Keep live runs on the cached `base` model.

    pipeline/asr.py defaults to large-v3 per policy.md §2. That is a ~3 GB download
    on first use, which is not something a test run should trigger silently. `base`
    is already cached locally; it mis-hears the odd homophone, which is exactly why
    the live assertions below key on numbers and meaning rather than exact wording.
    """
    if request.config.getoption("--live") and not os.getenv("WHISPER_MODEL"):
        os.environ["WHISPER_MODEL"] = "base"


# --------------------------------------------------------------------------------
# Audio synthesis
# --------------------------------------------------------------------------------


def _have_say() -> bool:
    return shutil.which("say") is not None


def _transcode_to_opus(src: Path, dst: Path) -> None:
    """AIFF -> OGG/Opus, 48 kHz mono, matching a WhatsApp voice note."""
    import av
    from av.audio.resampler import AudioResampler

    with av.open(str(src)) as inp, av.open(str(dst), "w", format="ogg") as out:
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


def _synthesise(text: str, lang: str) -> Path:
    voice = VOICES[lang]
    key = hashlib.sha256(f"{voice}:{text}".encode()).hexdigest()[:16]
    ogg = FIXTURE_DIR / f"{lang}-{key}.ogg"
    if ogg.exists():
        return ogg

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    aiff = ogg.with_suffix(".aiff")
    try:
        subprocess.run(
            ["say", "-v", voice, "-o", str(aiff), text],
            check=True,
            capture_output=True,
            timeout=60,
        )
        _transcode_to_opus(aiff, ogg)
    finally:
        aiff.unlink(missing_ok=True)
    return ogg


@pytest.fixture(scope="session")
def voice_clip():
    """Factory: (text, lang) -> path to an OGG/Opus clip of that text spoken aloud.

    Skips the test if the voice is not installed rather than failing — a missing
    system voice is an environment gap, not a defect in the pipeline.
    """
    if not _have_say():
        pytest.skip("macOS `say` not available; cannot synthesise voice fixtures")

    def make(text: str, lang: str = "en") -> str:
        if lang not in VOICES:
            pytest.skip(f"no system voice configured for {lang!r}")
        try:
            return str(_synthesise(text, lang))
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"`say -v {VOICES[lang]}` failed — voice not installed: {exc}")

    return make


# --------------------------------------------------------------------------------
# Image synthesis
# --------------------------------------------------------------------------------

# Stock macOS fonts. Arial covers Latin; the Unicode fallback carries Bengali, Tamil,
# and Chinese, which Arial does not.
LATIN_FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
UNICODE_FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


def _render(
    lines: list[str],
    *,
    width: int = 1000,
    font_size: int = 34,
    font_path: str | None = None,
) -> "object":
    """Render text as a plain white-on-dark document image. Returns a PIL Image."""
    from PIL import Image, ImageDraw, ImageFont

    path = font_path or LATIN_FONT
    if not os.path.exists(path):
        pytest.skip(f"font not available: {path}")
    font = ImageFont.truetype(path, font_size)

    padding, spacing = 40, int(font_size * 1.6)
    height = padding * 2 + spacing * len(lines)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((padding, padding + i * spacing), line, fill=(15, 15, 15), font=font)
    return img


@pytest.fixture(scope="session")
def text_image(tmp_path_factory):
    """Factory: (lines, **opts) -> (bytes, media_type) for an image containing that text.

    Options:
      fmt="JPEG"|"PNG"   container; WhatsApp sends photos as JPEG, screenshots as PNG
      blur=<radius>      Gaussian blur, to push the extraction below the gate
      scale=<0-1>        downscale then upscale back, simulating a low-resolution photo
      rotate=<degrees>   skew, as if photographed at an angle
      quality=<1-95>     JPEG compression
      unicode=True       use the font that carries Bengali/Tamil/Chinese
    """
    import io

    from PIL import ImageFilter

    out_dir = tmp_path_factory.mktemp("images")

    def make(lines, fmt="JPEG", blur=0, scale=1.0, rotate=0, quality=92, unicode=False):
        if isinstance(lines, str):
            lines = [lines]

        img = _render(lines, font_path=UNICODE_FONT if unicode else None)

        if rotate:
            img = img.rotate(rotate, expand=True, fillcolor=(255, 255, 255))
        if scale != 1.0:
            small = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            img = img.resize(small).resize((img.width, img.height))
        if blur:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur))

        buf = io.BytesIO()
        if fmt == "JPEG":
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue(), "image/jpeg"
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"

    make.dir = out_dir
    return make


@pytest.fixture(scope="session")
def photo_without_text(text_image):
    """A picture with no writing in it — the has_text=False case, not a bad read."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (800, 600), (110, 150, 200))
    draw = ImageDraw.Draw(img)
    draw.ellipse((300, 120, 500, 320), fill=(250, 220, 90))  # sun
    draw.polygon([(0, 600), (250, 300), (500, 600)], fill=(70, 110, 70))  # hill
    draw.polygon([(300, 600), (600, 280), (800, 600)], fill=(90, 130, 90))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


@pytest.fixture(scope="session")
def silent_clip(tmp_path_factory) -> str:
    """Four seconds of digital silence, as OGG/Opus.

    The negative case for abstention gate 1: Whisper's VAD finds no speech, so the
    transcript comes back empty and must never reach translation.
    """
    import av

    path = tmp_path_factory.mktemp("audio") / "silence.ogg"
    with av.open(str(path), "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "mono"

        # 20 ms frames of zeroed samples — Opus' native frame size.
        for _ in range(4 * 50):
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            frame.sample_rate = 48000
            frame.pts = None
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

    return str(path)
