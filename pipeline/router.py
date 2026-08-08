"""Stage 3 — router (policy.md §4).

Single multi-label call. A message can contain a policy claim, scam signals,
both, or neither:
  - policy claim  -> policy verification path (claims -> retrieval -> verify)
  - scam signals  -> scam handler (currently a stub, see pipeline.scam)
  - neither       -> "I can't check this, here's the MOM hotline" template
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import structured_call

SCAM_SIGNALS = [
    "claimed_authority",
    "urgency",
    "payment_request",
    "bypass_normal_channel",
    "threat",
    "too_good_to_be_true",
    "personal_data_request",
]

_SCHEMA = {
    "type": "object",
    "properties": {
        "contains_policy_claim": {
            "type": "boolean",
            "description": "True if the message asserts a checkable fact about Singapore work-pass/employment policy "
            "(levy, permit rules, fees, eligibility, salary, entitlements, procedures).",
        },
        "contains_scam_signals": {
            "type": "boolean",
            "description": "True if the message shows manipulation patterns typical of scams.",
        },
        "scam_signals": {
            "type": "array",
            "items": {"type": "string", "enum": SCAM_SIGNALS},
            "description": "Which scam signals are present (empty if none).",
        },
        "unintelligible": {
            "type": "boolean",
            "description": "True if the message is too garbled or empty to interpret.",
        },
    },
    "required": ["contains_policy_claim", "contains_scam_signals", "scam_signals", "unintelligible"],
}

_SYSTEM = (
    "You route messages forwarded by migrant workers in Singapore for a fact-checking service. "
    "Classify the message on multiple independent labels. A message can be BOTH a policy claim and a scam — set both flags when both apply.\n"
    "A 'policy claim' is any statement that can be phrased as 'X is true / X is false' about Singapore migrant-worker "
    "rules (MOM/CPF/IRAS work passes, levy, fees, eligibility, salary, entitlements, procedures, medical/insurance "
    "requirements, deportation/cancellation consequences tied to those rules). "
    "If such an assertion is present, set contains_policy_claim=true EVEN WHEN the message also uses threats, "
    "urgency, or claimed authority — those are scam signals layered on top of the claim, not a reason to skip verification.\n"
    "Scam signals (claimed_authority, urgency, payment_request, bypass_normal_channel, threat, too_good_to_be_true, "
    "personal_data_request) are independent. Pure instructions to send money or share passwords with no checkable "
    "policy assertion are scam-only. Be conservative on contains_policy_claim only when there is genuinely no "
    "verifiable assertion."
)


@dataclass
class Routing:
    contains_policy_claim: bool
    contains_scam_signals: bool
    scam_signals: list[str] = field(default_factory=list)
    unintelligible: bool = False
    language_detected: str | None = None


def route(text_en: str, *, language_detected: str | None = None) -> Routing:
    result = structured_call(
        system=_SYSTEM,
        user=text_en,
        schema=_SCHEMA,
        tool_name="record_routing",
        tool_description="Record the multi-label routing decision for this message.",
        max_tokens=512,
    )
    signals = [s for s in result.get("scam_signals", []) if s in SCAM_SIGNALS]
    return Routing(
        contains_policy_claim=bool(result.get("contains_policy_claim", False)),
        contains_scam_signals=bool(result.get("contains_scam_signals", False)),
        scam_signals=signals,
        unintelligible=bool(result.get("unintelligible", False)),
        language_detected=language_detected,
    )
