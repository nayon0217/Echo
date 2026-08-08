"""Scam handler — stub (policy.md §4).

Real signature returning a short, mergeable warning for stage-10 compose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MOM_HOTLINE = "MOM 6438 5122"
SCAM_HOTLINE = "ScamShield 1799"

# Keep flags short — compose already has a tight word budget.
_SIGNAL_EXPLANATIONS = {
    "claimed_authority": "claims to be an official",
    "urgency": "pushes you to act now",
    "payment_request": "asks for money",
    "bypass_normal_channel": "avoids official channels",
    "threat": "uses threats",
    "too_good_to_be_true": "promises too much",
    "personal_data_request": "asks for personal data",
}


@dataclass
class ScamResult:
    is_scam_suspected: bool
    signals: list[str] = field(default_factory=list)
    message: str = ""
    red_flags: list[str] = field(default_factory=list)


def handle_scam(signals: list[str]) -> ScamResult:
    """Return a short scam warning for the detected signals."""
    red_flags = [_SIGNAL_EXPLANATIONS[s] for s in signals if s in _SIGNAL_EXPLANATIONS]
    flag_text = ", ".join(red_flags[:3]) if red_flags else "common scam signs"
    message = (
        f"Possible scam ({flag_text}). Do not send money or click links. "
        f"Call {MOM_HOTLINE} or {SCAM_HOTLINE}."
    )
    return ScamResult(
        is_scam_suspected=bool(signals),
        signals=list(signals),
        message=message,
        red_flags=red_flags,
    )
