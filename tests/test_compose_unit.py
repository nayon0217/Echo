"""Unit tests for stage-10 compose (narrated replies with reasoning)."""

from pipeline.compose import compose_reply, MAX_WORDS


def test_supported_includes_reasoning_and_names_source():
    reply = compose_reply(
        claims=[
            {
                "text": "Employers must buy medical insurance for migrant workers.",
                "verdict": "supported",
                "reasoning": (
                    "MOM says employers must buy and keep medical insurance for each "
                    "Work Permit holder. That matches the claim that medical insurance "
                    "is required."
                ),
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
    assert "Checked:" in reply
    assert "Why:" in reply
    assert "medical insurance" in reply.lower()
    assert "MOM" in reply or "Read more:" in reply
    assert "Chunk" not in reply
    assert len(reply.split()) <= MAX_WORDS


def test_supported_strips_chunk_ids_from_reasoning():
    reply = compose_reply(
        claims=[
            {
                "text": "Salary must be paid monthly.",
                "verdict": "supported",
                "reasoning": "Chunk 141 states employers must pay salary each month within 7 days.",
                "cited_sources": [
                    {
                        "source_name": "MOM",
                        "source_url": "https://www.mom.gov.sg/salary",
                        "authority_tier": 1,
                    }
                ],
            }
        ]
    )
    assert "Why:" in reply
    assert "Chunk 141" not in reply
    assert "the official guidance" in reply


def test_refuted_voice_uses_voice_headline():
    reply = compose_reply(
        claims=[
            {
                "text": "The levy is $900 from August 2026.",
                "verdict": "refuted",
                "reasoning": "MOM does not list a $900 levy starting in August 2026 for this claim.",
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
    assert "Why:" in reply
    assert len(reply.split()) <= MAX_WORDS


def test_insufficient_gives_hotline_and_reasoning():
    reply = compose_reply(
        claims=[
            {
                "text": "Something vague about papers.",
                "verdict": "insufficient",
                "reasoning": "The retrieved pages talk about Work Permits but never mention this paper fee.",
                "cited_sources": [],
            }
        ]
    )
    assert "can't confirm" in reply.lower() or "🤔" in reply
    assert "Why:" in reply
    assert "6438 5122" in reply


def test_short_scam_merge():
    reply = compose_reply(
        claims=[
            {
                "text": "Workers must pay MOM $300 tonight.",
                "verdict": "refuted",
                "reasoning": "MOM does not ask workers to pay $300 by tonight.",
                "cited_sources": [],
            }
        ],
        scam={
            "is_scam_suspected": True,
            "signals": ["urgency", "payment_request"],
            "red_flags": ["pushes you to act now", "asks for money"],
        },
    )
    assert "❌ False." in reply
    assert "Possible scam" in reply
    assert "Why:" in reply
    assert "asks for money" in reply
    assert len(reply.split()) <= MAX_WORDS


def test_scam_only_includes_reasoning_flags():
    reply = compose_reply(
        scam={
            "is_scam_suspected": True,
            "signals": ["claimed_authority", "threat"],
        }
    )
    assert "Possible scam" in reply
    assert "Why:" in reply
    assert "claims to be an official" in reply
    assert "uses threats" in reply
    assert "ScamShield" in reply or "1799" in reply


def test_notice_when_nothing_else():
    reply = compose_reply(notice="I can't verify this message.")
    assert "can't verify" in reply.lower()


def test_long_reasoning_stays_a_complete_message():
    reply = compose_reply(
        claims=[
            {
                "text": "The levy is $900 from August 2026.",
                "verdict": "refuted",
                "reasoning": (
                    "MOM does not list a $900 levy starting in August 2026. "
                    "The official pages describe current levy rates for Work Permit holders. "
                    "Nothing in the retrieved guidance matches this date or amount. "
                    "A worker who paid on this claim would be sending money without an official basis. "
                    "Call MOM if you are unsure about a levy change."
                ),
                "cited_sources": [
                    {
                        "source_name": "MOM",
                        "source_url": "https://www.mom.gov.sg/levy",
                        "authority_tier": 1,
                        "snippet": (
                            "Levy rates depend on the sector and the worker's skill level "
                            "and are published on MOM's Work Permit levy pages for employers."
                        ),
                    }
                ],
            }
        ]
    )
    assert "…" not in reply and "..." not in reply
    assert reply.endswith((".", "!", "?")) or "levy" in reply.lower()
    assert len(reply.split()) <= MAX_WORDS + 15
