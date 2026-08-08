"""Unit tests for stage-10 compose (short replies, ≤150 words)."""

from pipeline.compose import compose_reply, MAX_WORDS


def test_supported_is_short_and_names_source():
    reply = compose_reply(
        claims=[
            {
                "text": "Employers must buy medical insurance for migrant workers.",
                "verdict": "supported",
                "reasoning": "long unused reasoning " * 40,
                "cited_sources": [
                    {
                        "source_name": "MOM",
                        "source_url": "https://www.mom.gov.sg/medical-insurance",
                        "authority_tier": 1,
                        "snippet": "You must buy and maintain medical insurance for each migrant worker.",
                    }
                ],
            }
        ]
    )
    assert "✅ True." in reply
    assert "MOM" in reply
    assert len(reply.split()) <= MAX_WORDS


def test_refuted_voice_uses_voice_headline():
    reply = compose_reply(
        claims=[
            {
                "text": "The levy is $900 from August 2026.",
                "verdict": "refuted",
                "reasoning": "unused",
                "cited_sources": [
                    {
                        "source_name": "MOM",
                        "source_url": "https://www.mom.gov.sg/levy",
                        "authority_tier": 1,
                    }
                ],
            }
        ],
        media_kind="voice",
    )
    assert "❌ This voice message is false." in reply
    assert len(reply.split()) <= MAX_WORDS


def test_insufficient_gives_hotline():
    reply = compose_reply(
        claims=[
            {
                "text": "Something vague about papers.",
                "verdict": "insufficient",
                "reasoning": "unused",
                "cited_sources": [],
            }
        ]
    )
    assert "can't confirm" in reply.lower() or "🤔" in reply
    assert "6438 5122" in reply


def test_short_scam_merge():
    reply = compose_reply(
        claims=[
            {
                "text": "Workers must pay MOM $300 tonight.",
                "verdict": "refuted",
                "reasoning": "",
                "cited_sources": [],
            }
        ],
        scam={"is_scam_suspected": True, "signals": ["urgency", "payment_request"]},
    )
    assert "❌ False." in reply
    assert "Possible scam" in reply
    assert len(reply.split()) <= MAX_WORDS


def test_notice_when_nothing_else():
    reply = compose_reply(notice="I can't verify this message.")
    assert "can't verify" in reply.lower()
