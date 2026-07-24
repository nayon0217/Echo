"""Heading-aware chunking for policy documents (policy.md §3).

Rules from the plan:
  - Split on headings first, then on paragraph boundaries.
  - Target 200-400 tokens per chunk with ~50 tokens of overlap.
  - Never use fixed character windows: policy pages have list structures where a
    mid-list split reads as a complete-but-false statement. So we split only on
    paragraph boundaries and keep list blocks intact.

Input is the markdown that trafilatura produces. We don't need a real tokenizer
for chunk sizing — a word-count estimate (tokens ~= words / 0.75) is close enough
to steer 200-400 token windows, and it removes a network/model dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_MIN_TOKENS = 200
TARGET_MAX_TOKENS = 400
OVERLAP_TOKENS = 50

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Chunk:
    ordinal: int
    heading: str | None
    content: str


def estimate_tokens(text: str) -> int:
    """Rough token count from words (English averages ~0.75 words/token)."""
    words = len(text.split())
    return round(words / 0.75) if words else 0


@dataclass
class _Block:
    """A paragraph or list block, tagged with the heading in force above it."""

    heading: str | None
    text: str


def _split_blocks(markdown: str) -> list[_Block]:
    """Break markdown into heading-tagged paragraph/list blocks.

    Consecutive list lines (-, *, digits.) stay in one block so a list is never
    split down the middle. Blank lines separate blocks; headings update the
    current heading and are not emitted as their own content blocks.
    """
    blocks: list[_Block] = []
    current_heading: str | None = None
    buf: list[str] = []
    buf_is_list = False

    def flush() -> None:
        nonlocal buf, buf_is_list
        if buf:
            text = "\n".join(buf).strip()
            if text:
                blocks.append(_Block(current_heading, text))
        buf = []
        buf_is_list = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        heading_match = _HEADING_RE.match(line.strip())

        if heading_match:
            flush()
            current_heading = heading_match.group(2).strip() or current_heading
            continue

        if not line.strip():
            flush()
            continue

        is_list_line = bool(re.match(r"^\s*([-*]|\d+[.)])\s+", line))
        # Keep list lines together; flush when switching between prose and list.
        if buf and (is_list_line != buf_is_list):
            flush()
        buf_is_list = is_list_line
        buf.append(line)

    flush()
    return blocks


def _tail_for_overlap(text: str, overlap_tokens: int) -> str:
    """Return the trailing ~overlap_tokens worth of words from text."""
    if overlap_tokens <= 0:
        return ""
    words = text.split()
    keep = max(1, round(overlap_tokens / 0.75))
    if len(words) <= keep:
        return text
    return " ".join(words[-keep:])


def chunk_markdown(markdown: str) -> list[Chunk]:
    """Chunk markdown into 200-400 token, heading-aware, overlapping chunks."""
    blocks = _split_blocks(markdown)
    chunks: list[Chunk] = []

    # Accumulate blocks that share a heading into chunks under the size target.
    current_heading: str | None = None
    body: list[str] = []
    carry = ""  # overlap text carried from the previous chunk

    def current_tokens() -> int:
        return estimate_tokens(" ".join(body))

    def flush() -> None:
        nonlocal body, carry
        joined = "\n\n".join(b for b in body if b).strip()
        if not joined:
            body = []
            return
        content = f"{current_heading}\n\n{joined}" if current_heading else joined
        chunks.append(Chunk(ordinal=len(chunks), heading=current_heading, content=content))
        carry = _tail_for_overlap(joined, OVERLAP_TOKENS)
        body = []

    for block in blocks:
        # Heading change starts a fresh chunk (headings are hard boundaries).
        if block.heading != current_heading:
            flush()
            current_heading = block.heading
            carry = ""

        block_tokens = estimate_tokens(block.text)

        # A single block larger than the max still ships whole — never split a
        # list/paragraph mid-way (policy.md §3).
        if block_tokens >= TARGET_MAX_TOKENS:
            flush()
            if carry:
                body.append(carry)
            body.append(block.text)
            flush()
            continue

        if body and current_tokens() + block_tokens > TARGET_MAX_TOKENS:
            flush()
            if carry:
                body.append(carry)

        body.append(block.text)

        if current_tokens() >= TARGET_MIN_TOKENS:
            flush()
            if carry:
                body = [carry]

    # Drop a trailing overlap-only remnant.
    if body and estimate_tokens(" ".join(body)) > OVERLAP_TOKENS:
        flush()
    elif body:
        flush()

    # Re-number ordinals densely after any drops.
    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks
