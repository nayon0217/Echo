"""FastAPI service fronting the ECHO verification pipeline (policy.md §13).

The Node WhatsApp layer (`src/`) owns the Meta webhook and acks it fast; it then
calls this service for ASR, vision, translation, and full claim verification.

Run it:
    uvicorn app.webhook:app --reload --port 8000

The port matches PIPELINE_URL in .env. Bind to localhost: this service holds the
Claude API key and must not be reachable from outside the host.

Privacy (policy.md §11): request and response bodies carry message content, so
nothing here logs them. Uvicorn's access log records method, path, and status only.
"""

from __future__ import annotations

import os
import tempfile

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from pipeline import asr, contract, vision
from pipeline.pipeline import process_message
from pipeline.translate import (
    REPLY_LANGUAGES,
    Translation,
    detect_and_translate,
    model_name,
    translate_transcript,
)

app = FastAPI(
    title="ECHO pipeline",
    description="Verification pipeline for the ECHO WhatsApp bot.",
    version="0.1.0",
)


class TranslateRequest(BaseModel):
    text: str = Field(description="The inbound message text, in any language.")


class TranslateResponse(BaseModel):
    language_code: str
    language_name: str
    text_en: str
    unintelligible: bool
    is_english: bool
    can_reply_in_language: bool

    @classmethod
    def from_translation(cls, t: Translation) -> "TranslateResponse":
        return cls(
            language_code=t.language_code,
            language_name=t.language_name,
            text_en=t.text_en,
            unintelligible=t.unintelligible,
            is_english=t.is_english,
            can_reply_in_language=t.can_reply_in_language,
        )


class TranscribeResponse(BaseModel):
    """Result of the voice path: what was said, and how confident we are it was heard."""

    # --- what was heard ---
    transcript: str = Field(description="Verbatim, in the language it was spoken.")
    spoken_language: str = Field(description="Whisper's language guess, ISO 639-1.")

    # --- confidence (policy.md §7 gate 1) ---
    mean_logprob: float
    language_probability: float
    max_no_speech_prob: float
    duration_seconds: float
    is_confident: bool = Field(
        description="False means the transcript failed gate 1 — ask for a re-record, don't verify."
    )

    # --- translations; both null when the transcript failed the gate ---
    text_en: str | None = Field(default=None, description="English pivot, for retrieval.")
    text_target: str | None = Field(default=None, description="Rendered in the worker's language.")
    target_language: str | None = None
    unintelligible: bool = False

    whisper_model: str = ""


class ExtractResponse(BaseModel):
    """Result of the image path: what the image said, and whether it could be read."""

    # --- what was read ---
    text_source: str = Field(description="Verbatim, in the language it is written in.")
    detected_language: str = Field(description="Language of the text, ISO 639-1.")
    has_text: bool = Field(description="False when the image contains no readable text at all.")

    # --- confidence (the image counterpart of policy.md §7 gate 1) ---
    confidence: float = Field(
        description=(
            "How well the image could be read, 0-1. SELF-REPORTED by the model, not a "
            "calibrated signal like the voice path's logprob — see pipeline/vision.py."
        )
    )
    untranscribable: bool = Field(
        description="True means the image failed the gate — ask for a clearer photo, don't verify."
    )

    # --- translations; both null when the image was untranscribable ---
    text_en: str | None = Field(default=None, description="English pivot, for retrieval.")
    text_target: str | None = Field(default=None, description="Rendered in the worker's language.")
    target_language: str | None = None
    unintelligible: bool = False


class ContractReadResponse(BaseModel):
    """Result of reading a contract. `text` is returned so the caller can hold it.

    This service stores nothing (policy.md §11): the extracted text goes back to the
    Node layer, which keeps it in an in-memory session for the life of the process and
    sends it back with each question.
    """

    is_contract: bool = Field(description="False means this isn't an employment document.")
    is_usable: bool = Field(description="False means don't answer questions against it.")
    confidence: float = Field(description="How well it could be read, 0-1. Self-reported.")
    language_code: str
    text: str = Field(description="The transcription. Empty when not usable.")


class ContractQuestionRequest(BaseModel):
    contract_text: str = Field(description="Text from a prior /contract call.")
    question: str = Field(description="The worker's question, in any language.")
    target_language: str = Field(description="ISO 639-1 reply language chosen by the worker.")


class ContractAnswerResponse(BaseModel):
    answerable: bool
    answer_en: str
    answer_target: str
    quote: str = ""
    needs_legal_check: bool = False


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check — also reports which models the pipeline is configured against."""
    return {"status": "ok", "model": model_name(), "whisper_model": asr.model_name()}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile = File(description="Audio file — WhatsApp voice notes are OGG/Opus."),
    target_language: str = Form(description="ISO 639-1 reply language chosen by the worker."),
) -> TranscribeResponse:
    """Transcribe a voice note, then translate it to English and the worker's language.

    Runs abstention gate 1 (policy.md §7) between the two: a transcript Whisper is not
    confident about is returned untranslated, so the caller asks for a re-record rather
    than pushing a mis-heard claim into verification. That also avoids paying for a
    translation of noise.
    """
    if target_language not in REPLY_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported target_language; expected one of {sorted(REPLY_LANGUAGES)}",
        )

    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio file")

    # Whisper wants a path. Write to a temp file and always delete it — policy.md §11
    # commits to processing voice notes and discarding them, never storing them.
    suffix = os.path.splitext(file.filename or "")[1] or ".ogg"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name

        try:
            t = asr.transcribe(tmp_path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"could not decode audio: {exc}") from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    base = TranscribeResponse(
        transcript=t.text,
        spoken_language=t.language,
        mean_logprob=round(t.mean_logprob, 4),
        language_probability=round(t.language_probability, 4),
        max_no_speech_prob=round(t.max_no_speech_prob, 4),
        duration_seconds=round(t.duration, 2),
        is_confident=t.is_confident,
        whisper_model=t.model,
    )

    # Gate 1: stop here rather than translating something we misheard.
    if not t.is_confident:
        print(
            f"[transcribe] gate 1 failed: logprob={t.mean_logprob:.3f} "
            f"no_speech={t.max_no_speech_prob:.3f} — not translating"
        )
        return base

    try:
        v = translate_transcript(t.text, target_language)
    except anthropic.APIStatusError as exc:
        # Log status + body so a Claude 400 (e.g. unsupported params) is visible
        # in the uvicorn terminal, not only as an opaque 502 to Node.
        print(f"[transcribe] claude api error ({exc.status_code}): {exc.message}")
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        print(f"[transcribe] cannot reach claude api: {exc}")
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    return base.model_copy(
        update={
            "text_en": v.text_en,
            "text_target": v.text_target,
            "target_language": target_language,
            "unintelligible": v.unintelligible,
            # Claude reads the transcript and may disagree with Whisper about the
            # language; its judgement is the better one for a full sentence.
            "spoken_language": v.language_code or t.language,
        }
    )


@app.post("/extract", response_model=ExtractResponse)
async def extract(
    file: UploadFile = File(description="Image file — WhatsApp photos are JPEG, screenshots PNG."),
    target_language: str = Form(description="ISO 639-1 reply language chosen by the worker."),
) -> ExtractResponse:
    """Read the text out of an image, then translate it to English and the worker's language.

    Mirrors /transcribe exactly, including the abstention between the two stages: an
    image the model could not read confidently comes back untranslated, so the caller
    asks for a clearer photo rather than pushing a possible misreading into
    verification, and we don't pay to translate a guess.

    The gate is weaker here than on the voice path — the confidence is the model's own
    self-report rather than a logprob (see pipeline/vision.py). It reliably catches an
    unreadable image; it does not catch a confidently misread digit.
    """
    if target_language not in REPLY_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported target_language; expected one of {sorted(REPLY_LANGUAGES)}",
        )

    image = await file.read()
    if not image:
        raise HTTPException(status_code=400, detail="empty image file")

    # Unlike voice, nothing is written to disk — the bytes go straight to the API and
    # are dropped when this function returns (policy.md §11).
    try:
        result = vision.extract_text(image, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    base = ExtractResponse(
        text_source=result.text,
        detected_language=result.language_code,
        has_text=result.has_text,
        confidence=round(result.confidence, 4),
        untranscribable=result.untranscribable,
    )

    # The gate: stop here rather than translating something we may have misread.
    if result.untranscribable:
        print(
            f"[extract] gate failed: has_text={result.has_text} "
            f"confidence={result.confidence:.3f} — not translating"
        )
        return base

    try:
        v = translate_transcript(result.text, target_language, source="image")
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    return base.model_copy(
        update={
            "text_en": v.text_en,
            "text_target": v.text_target,
            "target_language": target_language,
            "unintelligible": v.unintelligible,
            # Claude reads the whole passage when translating and may disagree with
            # the extraction pass about the language; the later judgement is better.
            "detected_language": v.language_code or result.language_code,
        }
    )


@app.post("/contract", response_model=ContractReadResponse)
async def read_contract_endpoint(
    file: UploadFile = File(description="The contract — a PDF, or a photo of its pages."),
) -> ContractReadResponse:
    """Transcribe an employment contract so the caller can hold it and ask about it.

    Takes no target_language: reading is language-agnostic and the transcription stays
    in the document's own language. The reply language matters only at question time.

    Returns the text rather than storing it — see ContractReadResponse. Nothing is
    written to disk here; unlike /transcribe there is no temp file at all.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty document")

    try:
        result = contract.read_contract(data, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    if not result.is_usable:
        print(
            f"[contract] unusable: is_contract={result.is_contract} "
            f"confidence={result.confidence:.3f}"
        )
        # Withhold the text when it isn't good enough to answer against, so a caller
        # cannot accidentally hold and query a bad read.
        return ContractReadResponse(
            is_contract=result.is_contract,
            is_usable=False,
            confidence=round(result.confidence, 4),
            language_code=result.language_code,
            text="",
        )

    return ContractReadResponse(
        is_contract=True,
        is_usable=True,
        confidence=round(result.confidence, 4),
        language_code=result.language_code,
        text=result.text,
    )


@app.post("/contract/ask", response_model=ContractAnswerResponse)
def ask_contract(req: ContractQuestionRequest) -> ContractAnswerResponse:
    """Answer one question about a contract, grounded only in its text.

    The contract arrives on every request because this service holds no state. That is
    the deliberate trade for policy.md §11 — the document lives only in the caller's
    memory, and only for as long as that process runs.
    """
    if req.target_language not in REPLY_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported target_language; expected one of {sorted(REPLY_LANGUAGES)}",
        )

    try:
        answer = contract.answer_question(req.contract_text, req.question, req.target_language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    return ContractAnswerResponse(
        answerable=answer.answerable,
        answer_en=answer.answer_en,
        answer_target=answer.answer_target,
        quote=answer.quote,
        needs_legal_check=answer.needs_legal_check,
    )


@app.post("/translate", response_model=TranslateResponse)
def translate(req: TranslateRequest) -> TranslateResponse:
    """Detect the message's language and return it translated into English.

    502 on an upstream Claude failure, so the caller can tell "the pipeline is
    down" from "the message was unusable" (400) and degrade accordingly.
    """
    try:
        result = detect_and_translate(req.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"claude api error ({exc.status_code})"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="cannot reach claude api") from exc

    return TranslateResponse.from_translation(result)


class ProcessRequest(BaseModel):
    text: str = Field(..., description="Original or pivot text to verify.")
    language: str | None = Field(
        None, description="Worker's chosen reply language (BCP-47), for later localisation."
    )
    text_en: str | None = Field(
        None,
        description="Pre-translated English pivot (from ASR/vision). Skips stage-2 translate.",
    )
    source_language: str | None = Field(
        None, description="Detected source language when text_en is supplied."
    )
    media_kind: str | None = Field(
        None, description="voice | image | text — shapes stage-10 compose wording."
    )
    with_verify: bool = Field(
        True, description="Run verdict + citation audit + gates for each claim (stages 7-9)."
    )


@app.post("/process")
def process(req: ProcessRequest) -> dict:
    """Run stages 2–10: translate → route → claims → retrieve → verify → compose."""
    kind = req.media_kind if req.media_kind in ("voice", "image", "text") else None
    try:
        result = process_message(
            req.text,
            text_en=req.text_en,
            source_language=req.source_language,
            media_kind=kind,
            reply_language=req.language,
            with_verify=req.with_verify,
        )
    except Exception as exc:
        # Surface upstream failures as 502 so the bot can degrade gracefully.
        raise HTTPException(status_code=502, detail=f"pipeline error: {exc}") from exc

    payload = result.to_dict()
    payload["reply_language"] = req.language
    return payload
