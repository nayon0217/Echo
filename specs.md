# ECHO — Voice-First AI Verification for Migrant Workers in Singapore

> *A voice sent out gets an answer back — and the bot breaks echo chambers.*

**Focus areas:** AI and MIL (primary), Community Impact
**Category:** Applications/Websites — WhatsApp-based bot, with a community-based intervention layer

---

## 1. The Problem

Singapore hosts roughly 800,000 migrant workers across construction, marine, and domestic work. They are heavy WhatsApp users, but many have low literacy in English *and* in their own first language. This makes them structurally invisible to the existing MIL toolkit: text-based fact-checkers, explainer articles, and quiz apps all assume a reader.

Their actual information environment is **voice**:

- Forwarded rumours about work permit changes
- Fake "new MOM policy" audio clips
- Remittance and recruitment scam calls
- AI-cloned voice scams impersonating family members or agents asking for money

The last category is growing fastest, and it is precisely the one that existing tools are worst at reaching.

**The gap:** 2025's winning entries (MIL Point, Mentes Libres) targeted Gen Z app users and orphaned youth. Nobody has built for voice-note-native, low-literacy adult migrant workers. That gap is genuine and defensible.

---

## 2. The Solution

A WhatsApp bot that workers forward any suspicious voice note, image, or job posting to. It replies **in the worker's own language** — English, Bengali, Tamil, Mandarin, Indonesian — with a short **voice message, not text**, walking through the same reasoning a literate fact-checker would use out loud.

This is the core design decision: reading is the barrier, so the entire interaction is built to never require it.

### Core capabilities

| Capability | What it does |
|---|---|
| **AI-generation detection** | Flags whether a voice note or image is synthetic (Google SynthID) |
| **Claim extraction & verification** | Transcribes the voice note or parses the image, then checks the claim against news and official policy sources (RAG) |
| **Translation** | Delivers the answer in the worker's mother tongue |
| **Contract parsing** | Workers upload their employment contract and ask questions about it — what limits apply, what they should raise with their employer |

### Additional features

- Newsletter updates on employment policy changes and key news from the worker's home country

---

## 3. Framework — The PAUSE Protocol

Taught in dormitory sessions, reinforced by every bot reply:

| | Step | |
|---|---|---|
| **P** | **Pause** | before forwarding |
| **A** | **Ask** | who actually sent it first |
| **U** | **Understand** | what it's asking you to do — send money? click a link? panic? |
| **S** | **Source-check** | forward it to the bot |
| **E** | **Explain** | tell a friend afterwards — teaching it to someone else is the retention step |

---

## 4. User Journey

1. A worker receives a suspicious voice note claiming to be from a recruitment agent or an "MOM officer."
2. He forwards it to the ECHO WhatsApp number.
3. Within seconds he gets back a ~30-second voice reply in his own language covering:
   - **What red flags were detected** — voice-clone artifacts, urgency and money-transfer language, mismatched caller ID patterns
   - **What to actually do next** — e.g. call MOM's verified hotline, *not* the number in the clip
4. The bot never says only "real" or "fake." It narrates the reasoning, so that over repeated use workers start recognising the red flags themselves.

That last point is the whole MIL thesis: the bot is a teacher that happens to answer questions, not an oracle.

---

## 5. Technical Architecture

Buildable by a technical team within hackathon scope.

| Layer | Choice |
|---|---|
| **Messaging** | WhatsApp Cloud API (Meta-hosted). Telegram bot as fallback if WhatsApp provisioning stalls |
| **Speech-to-text** | Open-source multilingual ASR (Whisper) |
| **Reasoning** | Lightweight LLM layer, retrieval-grounded — never freelances an answer |
| **Fact base** | Official sources only: MOM advisories, Singapore Police Force Scam Alert bulletins, TWC2 and Migrant Workers' Centre scam reports |
| **Synthetic media detection** | AI-detection API (SynthID and equivalents) |
| **Reply generation** | TTS matched to the worker's language and dialect |

### Cost note

ECHO is inherently **user-initiated**, which puts it in WhatsApp's free lane — replies sent inside the 24-hour customer service window opened by an inbound message are not billed. Only the newsletter feature (a business-initiated template) carries a per-message cost. Meta has announced that per-message billing extends to service replies from 1 October 2026, which is a known input to the sustainability plan rather than a surprise.

### Privacy

**No personal data retention.** Voice notes are processed and discarded, not stored. This is both an ethical baseline and a practical one — this population is, with good reason, wary of systems that keep records of them.

---

## 6. Community Distribution Layer

Partner with existing on-the-ground infrastructure rather than building new reach from scratch.

- **HealthServe, TWC2, and the Migrant Workers' Centre** already run Sunday gatherings in Little India, Geylang Serai, and Soon Lee — ideal venues for short PAUSE Protocol demos and QR-code bot sign-up.
- **Dormitory recreation committees** provide a second, repeatable channel.
- **Worker peer-ambassadors:** train a small cohort so the message comes from *within* the community rather than from an outside app. This is what made Mentes Libres credible, and it is the single highest-leverage adoption decision in the plan.

---

## 7. Why This Maxes Each Criterion

**Consistency with theme.** A direct response to the challenges posed by AI — voice-clone scams — answered with MIL principles: verify the source, question the intent, understand the context.

**Clarity of presentation.** A working bot demo, where judges forward a test voice note live and hear the reply, beats any slide deck.

**Innovation.** Voice-first design *is* the innovation. Nearly every MIL tool defaults to text or quiz formats and thereby excludes this population by design. ECHO inverts the default.

**Feasibility and sustainability.** WhatsApp requires no app install — critical, since workers often use shared, low-storage, or older devices and are reluctant to install unfamiliar software. Distribution rides on partner organisations that already have trust and footfall, and the free service-window model keeps marginal cost near zero at pilot scale.

---

## Open Items

- [ ] Confirm SynthID API access and coverage for audio, not just images
- [ ] Build the initial fact base — scope which MOM/SPF sources to ingest and how often to refresh
- [ ] Pick TTS voices per language; validate intelligibility with native speakers, not just a demo
- [ ] Decide contract-parsing scope for the hackathon build vs. the roadmap
- [ ] Line up at least one partner org contact before submission
