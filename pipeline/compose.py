"""Stage 10 — compose the worker-facing reply (policy.md §10).

Narrate the check (claim → what sources say → verdict), in plain language.
Keep the whole reply within a voice-note-friendly word budget, then localise
into the worker's chosen reply language when it is not English.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pipeline.translate import REPLY_LANGUAGES, localize_reply

MediaKind = Optional[Literal["voice", "image", "text"]]

MOM_HOTLINE = "MOM 6438 5122"
SCAM_CONTACT = "MOM 6438 5122 or ScamShield 1799"
MAX_WORDS = 75
REASONING_WORDS = 40


def _reframe_claim(text: str) -> str:
    return (text or "").strip().rstrip(".?!")


def _word_count(text: str) -> int:
    return len((text or "").split())


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？။])\s+")


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split((text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _fit_words(text: str, limit: int) -> str:
    """Keep complete sentences that fit `limit` words. Never append an ellipsis.

    The reply is written to the budget as a finished message, not a longer one
    that gets cut mid-sentence with "…".
    """
    text = (text or "").strip()
    if not text or _word_count(text) <= limit:
        return text
    kept: list[str] = []
    count = 0
    for sentence in _sentences(text):
        n = _word_count(sentence)
        if count + n > limit:
            break
        kept.append(sentence)
        count += n
    if kept:
        return " ".join(kept)
    # A single long sentence still goes out whole — better slightly over budget
    # than a chopped "…" that sounds unfinished when spoken.
    return _sentences(text)[0]


def _short_url(url: str | None) -> str:
    return (url or "").strip()


def _worker_reasoning(text: str, *, limit: int = REASONING_WORDS) -> str:
    """Clean verifier prose so it reads aloud cleanly to a worker."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"\b[Cc]hunks?\s+\d+\b", "the official guidance", cleaned)
    cleaned = re.sub(r"\[\d+\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _fit_words(cleaned, limit)


def _compose_one_claim(claim: dict[str, Any], *, media_kind: MediaKind) -> str:
    # policy.md §10: claim restated → what sources say → verdict as reasoning → next step.
    text = _fit_words((claim.get("text") or "").strip(), 28)
    verdict = claim.get("verdict") or "insufficient"
    why = _worker_reasoning(claim.get("reasoning") or "")
    cited_list = claim.get("cited_sources") or []
    cited = cited_list[0] if cited_list else None
    source_name = (cited or {}).get("source_name") or "MOM"
    url = _short_url((cited or {}).get("source_url"))
    snippet = _fit_words((cited or {}).get("snippet") or "", 40)
    tier = (cited or {}).get("authority_tier")

    # Name the source; mention tier lightly so workers see NGO/news vs MOM.
    if tier == 2:
        source_label = f"{source_name} (worker advice)"
    elif tier == 3:
        source_label = f"{source_name} (news)"
    else:
        source_label = source_name

    checked = f"Checked: {_reframe_claim(text)}."

    if verdict == "supported":
        parts = ["✅ True.", checked]
        if why:
            parts.append(f"Why: {why}")
        elif snippet:
            parts.append(f"{source_label} says: {snippet}")
        else:
            parts.append(f"This matches what {source_label} says.")
        if url:
            parts.append(f"Read more: {url}")
        return "\n".join(parts)

    if verdict == "refuted":
        headline = "❌ This voice message is false." if media_kind == "voice" else "❌ False."
        parts = [headline, checked]
        if why:
            parts.append(f"Why: {why}")
        else:
            parts.append("Official rules do not match this claim.")
            if snippet:
                parts.append(f"{source_label} says: {snippet}")
        if url:
            parts.append(f"Read more: {url}")
        return "\n".join(parts)

    parts = [
        "🤔 I can't confirm this.",
        checked,
    ]
    if why:
        parts.append(f"Why: {why}")
    parts.append(
        f"Not enough clear official information. To be safe, call {MOM_HOTLINE}."
    )
    return "\n".join(parts)


def format_ai_detection(status: str | None) -> str | None:
    if status == "ai_generated":
        return "⚠️ This may be AI-made. Do not trust it without checking."
    if status == "likely_ai":
        return "⚠️ This might be AI-made. Be careful before you share or act."
    if status == "not_ai":
        return "✅ This does not look AI-made. The information can still be wrong."
    return None


def _compose_scam(scam: dict[str, Any]) -> str:
    """Verdict + short why (policy.md §10) from the scam stub's red flags."""
    flags = scam.get("red_flags") or []
    if not flags:
        # Fall back to signal keys if red_flags weren't populated.
        from pipeline.scam import _SIGNAL_EXPLANATIONS

        flags = [
            _SIGNAL_EXPLANATIONS[s]
            for s in (scam.get("signals") or [])
            if s in _SIGNAL_EXPLANATIONS
        ]

    why = ""
    if flags:
        shown = flags[:3]
        why = "Why: " + "; ".join(shown) + ".\n"

    return (
        f"⚠️ Possible scam.\n"
        f"{why}"
        f"Do not send money or click links. If unsure, call {SCAM_CONTACT}."
    )


def compose_reply(
    *,
    claims: list[dict[str, Any]] | None = None,
    scam: dict[str, Any] | None = None,
    notice: str | None = None,
    ai_detection: str | None = None,
    media_kind: MediaKind = None,
    reply_language: str | None = None,
) -> str:
    """Build a narrated WhatsApp reply, then localise if needed."""
    claims = claims or []
    parts: list[str] = []

    ai_msg = format_ai_detection(ai_detection)
    if ai_msg:
        parts.append(ai_msg)

    if len(claims) == 1:
        parts.append(_compose_one_claim(claims[0], media_kind=media_kind))
    elif len(claims) > 1:
        # Cap at 2 claims to stay under the word budget.
        blocks = []
        for i, c in enumerate(claims[:2], 1):
            blocks.append(f"{i}. {_compose_one_claim(c, media_kind=media_kind)}")
        parts.append("\n".join(blocks))

    if scam and scam.get("is_scam_suspected"):
        parts.append(_compose_scam(scam))

    if not parts:
        text = notice or (
            f"I could not find anything to check. If unsure, call {MOM_HOTLINE}."
        )
    else:
        text = "\n\n".join(parts)

    text = _fit_words(text, MAX_WORDS)

    lang = (reply_language or "en").lower()
    if lang in REPLY_LANGUAGES and lang != "en":
        try:
            text = localize_reply(text, lang)
            text = _fit_words(text, MAX_WORDS + 20)  # translated text can be denser
        except Exception as exc:  # noqa: BLE001 — fall back to English
            print(f"[compose] localize failed ({exc}); sending English")

    return text.strip()


def compose_from_result(
    result: Any, *, media_kind: MediaKind = None, reply_language: str | None = None
) -> str:
    """Accept a PipelineResult dataclass or a to_dict() payload."""
    if hasattr(result, "to_dict"):
        d = result.to_dict()
    elif isinstance(result, dict):
        d = result
    else:
        raise TypeError(f"unsupported result type: {type(result)!r}")

    return compose_reply(
        claims=d.get("claims") or [],
        scam=d.get("scam"),
        notice=d.get("notice"),
        ai_detection=d.get("ai_detection"),
        media_kind=media_kind,
        reply_language=reply_language or d.get("reply_language"),
    )
