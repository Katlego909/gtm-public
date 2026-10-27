"""
Plain-text helpers shared across apps. Previews here always end on a whole
sentence -- the app never shows text cut off mid-word or mid-sentence.
"""
import re

from django.utils.text import Truncator


def sentence_preview(text: str, max_chars: int = 200) -> str:
    """Build a preview out of whole sentences only, growing until the next
    sentence would exceed `max_chars` -- never cuts a sentence off mid-way,
    so what's shown always reads as a complete thought (even a single long
    sentence beats a fragment ending nowhere).

    Skips label:value metadata paragraphs (e.g. "Document ID: ... Author:
    AI Assistant") that some documents open with -- otherwise those glue
    onto the first real sentence since they have no terminal punctuation
    of their own, producing an oddly long, metadata-heavy preview."""
    text = text.strip()
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    for paragraph in paragraphs:
        word_count = len(paragraph.split())
        looks_like_labels = paragraph.count(':') >= 2 and word_count < 25
        if looks_like_labels:
            continue

        sentences = re.split(r'(?<=[.!?])\s+', paragraph)
        preview = ''
        for sentence in sentences:
            if not sentence:
                continue
            candidate = f"{preview} {sentence}".strip() if preview else sentence
            if preview and len(candidate) > max_chars:
                break
            preview = candidate
        if preview:
            return preview

    # Every paragraph looked like metadata -- fall back to a word-safe cut
    # of the raw text rather than showing nothing.
    return Truncator(text).words(28, truncate='')
