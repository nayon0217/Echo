"""Stage 4 — atomic claim extraction (policy.md §5).

Turn a message into a list of atomic, independently checkable assertions. Each
claim must be a complete sentence with its subject restored (pronouns resolved),
because it is retrieved and verified in isolation, without the surrounding
transcript.

Rejects instructions, threats, and opinions — those belong to the scam path.
A claim that cannot be phrased as "X is true / X is false" is not a claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm import structured_call

CLAIM_TYPES = [
    "policy_change",
    "fee",
    "levy",
    "eligibility",
    "procedure",
    "entitlement",
    "penalty",
    "salary",
    "other",
]

_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "A single atomic assertion as a complete, self-contained sentence with all "
                        "pronouns and subjects resolved. Must be phraseable as 'X is true / X is false'.",
                    },
                    "type": {"type": "string", "enum": CLAIM_TYPES},
                },
                "required": ["text", "type"],
            },
        }
    },
    "required": ["claims"],
}

_SYSTEM = (
    "You extract atomic, independently checkable factual claims about Singapore migrant-worker policy "
    "(MOM/CPF/IRAS work-pass and employment rules) from a message. "
    "Rules:\n"
    "- Each claim must be ONE assertion, a complete sentence, with pronouns and subjects restored so it stands alone.\n"
    "- Each claim must be phraseable as 'X is true / X is false'.\n"
    "- Split compound statements into separate claims.\n"
    "- Restore official terminology where obvious (e.g. 'work permit', 'levy').\n"
    "- Reject instructions, threats, opinions, and questions — do NOT turn them into claims.\n"
    "- Reject one-off payment / OTP / link demands aimed at this worker "
    "('pay us $300 tonight', 'send your Singpass OTP') — those are scam content, not policy claims.\n"
    "- Only keep a claim if it states a general rule or fact (e.g. 'The levy is $650', "
    "'Employers must buy medical insurance').\n"
    "- If there is no checkable policy claim, return an empty list."
)


@dataclass
class Claim:
    text: str
    type: str


def extract_claims(text_en: str) -> list[Claim]:
    result = structured_call(
        system=_SYSTEM,
        user=text_en,
        schema=_SCHEMA,
        tool_name="record_claims",
        tool_description="Record the list of atomic, checkable policy claims found in the message.",
        max_tokens=1500,
    )
    claims: list[Claim] = []
    for item in result.get("claims", []):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        ctype = item.get("type") if item.get("type") in CLAIM_TYPES else "other"
        claims.append(Claim(text=text, type=ctype))
    return claims
