"""Stage 10 — compose the worker-facing reply (policy.md §10).

Keep the whole reply short (≤150 words), plain language, and easy to understand.
Then localise into the worker's chosen reply language when it is not English.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pipeline.translate import REPLY_LANGUAGES, localize_reply

MediaKind = Optional[Literal["voice", "image", "text"]]

MOM_HOTLINE = "MOM 6438 5122"
SCAM_CONTACT = "MOM 6438 5122 or ScamShield 1799"
MAX_WORDS = 150


def _reframe_claim(text: str) -> str:
    return (text or "").strip().rstrip(".?!")


def _word_count(text: str) -> int:
    return len((text or "").split())


def _trim_words(text: str, limit: int) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return (text or "").strip()
    return " ".join(words[:limit]).rstrip(".,;:") + "…"


def _short_url(url: str | None) -> str:
    return (url or "").strip()


def _compose_one_claim(claim: dict[str, Any], *, media_kind: MediaKind) -> str:
    text = _trim_words((claim.get("text") or "").strip(), 28)
    verdict = claim.get("verdict") or "insufficient"
    cited_list = claim.get("cited_sources") or []
    cited = cited_list[0] if cited_list else None
    source_name = (cited or {}).get("source_name") or "MOM"
    url = _short_url((cited or {}).get("source_url"))
    tier = (cited or {}).get("authority_tier")

    # Name the source; mention tier lightly so workers see NGO/news vs MOM.
    if tier == 2:
        source_label = f"{source_name} (worker advice)"
    elif tier == 3:
        source_label = f"{source_name} (news)"
    else:
        source_label = source_name

    if verdict == "supported":
        line = f"✅ True.\nThis matches {source_label}."
        if url:
            line += f"\nRead more: {url}"
        return line

    if verdict == "refuted":
        headline = "❌ This voice message is false." if media_kind == "voice" else "❌ False."
        line = f"{headline}\nNo clear evidence that {_reframe_claim(text)}."
        if url:
            line += f"\nRead more: {url}"
        return line

    return (
        "🤔 I can't confirm this.\n"
        f"There is not enough official information. To be safe, call {MOM_HOTLINE}."
    )


def format_ai_detection(status: str | None) -> str | None:
    if status == "ai_generated":
        return "⚠️ This may be AI-made. Do not trust it without checking."
    if status == "likely_ai":
        return "⚠️ This might be AI-made. Be careful before you share or act."
    if status == "not_ai":
        return "✅ This does not look AI-made. The information can still be wrong."
    return None


def compose_reply(
    *,
    claims: list[dict[str, Any]] | None = None,
    scam: dict[str, Any] | None = None,
    notice: str | None = None,
    ai_detection: str | None = None,
    media_kind: MediaKind = None,
    reply_language: str | None = None,
) -> str:
    """Build a short WhatsApp reply, then localise if needed."""
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
        parts.append(
            f"⚠️ Possible scam.\nDo not send money or click links. If unsure, call {SCAM_CONTACT}."
        )

    if not parts:
        text = notice or (
            f"I could not find anything to check. If unsure, call {MOM_HOTLINE}."
        )
    else:
        text = "\n\n".join(parts)

    text = _trim_words(text, MAX_WORDS)

    lang = (reply_language or "en").lower()
    if lang in REPLY_LANGUAGES and lang != "en":
        try:
            text = localize_reply(text, lang)
            text = _trim_words(text, MAX_WORDS + 20)  # translated text can be denser
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
