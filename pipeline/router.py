"""Stage 3 — router (policy.md §4).

Single multi-label call. A message can contain a policy claim, scam signals,
both, or neither:
  - policy claim  -> policy verification path (claims -> retrieval -> verify)
  - scam signals  -> scam handler (currently a stub, see pipeline.scam)
  - neither       -> "I can't check this, here's the MOM hotline" template
"""

from __future__ import annotations

import re
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
            "description": (
                "True only if the message asserts a general checkable fact about Singapore "
                "work-pass/employment policy (levy rates, permit rules, fees, eligibility, "
                "salary rules, entitlements, procedures). False for instructions, demands, "
                "threats, or one-off payment requests — even if they mention MOM."
            ),
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
    "Classify on independent labels. A message can be BOTH a policy claim and a scam.\n\n"
    "SCAM SIGNALS — set contains_scam_signals=true and list every matching signal when the "
    "message does any of these:\n"
    "- claimed_authority: pretends to be MOM, ICA, police, CPF, bank, employer, or 'official'\n"
    "- urgency: act now / today / within hours / immediately / before midnight\n"
    "- payment_request: asks the worker to pay, transfer, top-up, buy a voucher, or send money\n"
    "- bypass_normal_channel: private number, WhatsApp/Telegram link, personal account, "
    "QR code, unofficial website — instead of mom.gov.sg / official hotlines\n"
    "- threat: cancel permit, deport, arrest, blacklist, fine if they do not comply\n"
    "- too_good_to_be_true: huge salary, free pass, guaranteed PR, easy money\n"
    "- personal_data_request: asks for OTP, Singpass, passport, bank PIN, photos of documents\n\n"
    "Classic MOM-impersonation scams ('pay $X now or your Work Permit is cancelled', "
    "'verify your account via this link') MUST set contains_scam_signals=true. Do not "
    "route those as policy-only.\n\n"
    "POLICY CLAIM — set contains_policy_claim=true only for a general assertion that can be "
    "phrased as 'X is true / X is false' about Singapore migrant-worker rules "
    "(e.g. 'The levy is $650', 'Employers must buy medical insurance').\n"
    "Do NOT set contains_policy_claim for:\n"
    "- Instructions or demands aimed at this worker ('you must pay us tonight')\n"
    "- Threats, opinions, or questions with no factual assertion\n"
    "- Payment / OTP / link requests that merely name MOM or a fee\n"
    "If a scam also states a checkable general rule, set BOTH flags. "
    "If it is only a demand or threat dressed up with MOM language, scam-only."
)

# Additive overlays for classic scam wording the model sometimes misses.
_HEURISTIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "claimed_authority",
        re.compile(
            r"\b(ministry of manpower|\bmom\b|immigration|ica\b|police|scamshield|"
            r"cpf board|iras\b|work permit department)\b",
            re.I,
        ),
    ),
    (
        "urgency",
        re.compile(
            r"\b(immediately|urgent|right now|within \d+\s*(hour|hr|day)s?|"
            r"before midnight|today only|act now|asap)\b",
            re.I,
        ),
    ),
    (
        "payment_request",
        re.compile(
            r"\b(pay|payment|transfer|top-?up|send money|remit|wire|deposit|"
            r"paynow|paylah|voucher|gift card)\b",
            re.I,
        ),
    ),
    (
        "bypass_normal_channel",
        re.compile(
            r"(whatsapp|telegram|bit\.ly|tinyurl|click (this|the) link|"
            r"scan (this )?qr|personal account|private number)",
            re.I,
        ),
    ),
    (
        "threat",
        re.compile(
            r"\b(cancel(led|lation)?|deport(ed|ation)?|arrest(ed)?|blacklist|"
            r"fine you|legal action|revoke(d)? (your )?(pass|permit))\b",
            re.I,
        ),
    ),
    (
        "personal_data_request",
        re.compile(
            r"\b(otp|one[- ]time (password|pin)|singpass|bank (pin|password)|"
            r"passport (number|photo)|nric)\b",
            re.I,
        ),
    ),
    (
        "too_good_to_be_true",
        re.compile(
            r"\b(guaranteed pr|free work permit|earn \$?\d{4,}|salary \$?\d{4,}\s*"
            r"(sgd|dollars)?\s*(a|/)\s*month)\b",
            re.I,
        ),
    ),
]


@dataclass
class Routing:
    contains_policy_claim: bool
    contains_scam_signals: bool
    scam_signals: list[str] = field(default_factory=list)
    unintelligible: bool = False
    language_detected: str | None = None


def _heuristic_scam_signals(text: str) -> list[str]:
    """Extra scam signals from clear lexical patterns (additive only)."""
    found: list[str] = []
    for name, pattern in _HEURISTIC_PATTERNS:
        if pattern.search(text or ""):
            found.append(name)
    # A lone "MOM" in a real policy question should not force the scam path.
    if "claimed_authority" in found:
        companions = {
            "urgency",
            "payment_request",
            "bypass_normal_channel",
            "threat",
            "personal_data_request",
        }
        if not (companions & set(found)):
            found = [s for s in found if s != "claimed_authority"]
    return found


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
    for s in _heuristic_scam_signals(text_en):
        if s not in signals:
            signals.append(s)

    contains_scam = bool(result.get("contains_scam_signals", False)) or bool(signals)
    contains_policy = bool(result.get("contains_policy_claim", False))

    # Pure demand/threat with strong scam cues and no other signal of a general rule:
    # if heuristics found payment + (threat|authority|bypass) and the model also
    # marked policy, keep policy (both can fire). Scam must never be dropped.
    return Routing(
        contains_policy_claim=contains_policy,
        contains_scam_signals=contains_scam,
        scam_signals=signals,
        unintelligible=bool(result.get("unintelligible", False)),
        language_detected=language_detected,
    )
