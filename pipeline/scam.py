"""Scam handler — stub (policy.md §4).

Per the plan this is a *real function signature returning a real message*, not a
`pass`, so the compose stage is already written to merge two result objects when
the full scam-pattern path is built later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MOM_HOTLINE = "MOM hotline 6438 5122"
SCAM_HOTLINE = "ScamShield helpline 1799"

_SIGNAL_EXPLANATIONS = {
    "claimed_authority": "the sender claims to be an official (e.g. MOM, police) — real agencies don't demand action over WhatsApp",
    "urgency": "it pressures you to act immediately, which is a classic pressure tactic",
    "payment_request": "it asks you to pay or transfer money",
    "bypass_normal_channel": "it tells you to avoid official channels or your employer",
    "threat": "it threatens you (fines, cancellation, deportation) to make you comply",
    "too_good_to_be_true": "it promises something that sounds too good to be true",
    "personal_data_request": "it asks for personal details, passwords, or your work pass number",
}


@dataclass
class ScamResult:
    is_scam_suspected: bool
    signals: list[str] = field(default_factory=list)
    message: str = ""
    red_flags: list[str] = field(default_factory=list)


def handle_scam(signals: list[str]) -> ScamResult:
    """Return a generic scam warning for the detected signals.

    Stub: does not yet match named typologies from SPF ScamAlert. It produces a
    real, mergeable result so §10 compose can already consume it.
    """
    red_flags = [_SIGNAL_EXPLANATIONS[s] for s in signals if s in _SIGNAL_EXPLANATIONS]
    flag_text = "; ".join(red_flags) if red_flags else "it shows patterns commonly seen in scams"
    message = (
        f"This message shows warning signs of a scam: {flag_text}. "
        f"Do not send money or personal details, and do not use any phone number in the message. "
        f"If unsure, check with the {MOM_HOTLINE} or the {SCAM_HOTLINE}."
    )
    return ScamResult(
        is_scam_suspected=bool(signals),
        signals=list(signals),
        message=message,
        red_flags=red_flags,
    )
