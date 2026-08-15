"""Unit tests for the stage-3 router heuristics (no LLM calls)."""

from pipeline.router import _heuristic_scam_signals


def test_mom_payment_threat_is_scam():
    text = (
        "This is MOM. Pay $300 to renew your Work Permit immediately "
        "or your permit will be cancelled."
    )
    signals = _heuristic_scam_signals(text)
    assert "claimed_authority" in signals
    assert "payment_request" in signals
    assert "urgency" in signals or "threat" in signals


def test_plain_policy_question_is_not_scam():
    text = "Did MOM increase the Work Permit levy this year?"
    assert _heuristic_scam_signals(text) == []


def test_otp_request_flags_personal_data():
    text = "Send your Singpass OTP to verify your account on WhatsApp."
    signals = _heuristic_scam_signals(text)
    assert "personal_data_request" in signals
    assert "bypass_normal_channel" in signals
