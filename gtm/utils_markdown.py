"""Markdown -> HTML rendering that is safe to drop into a template with |safe.

Python-Markdown passes raw inline HTML straight through, and much of what we
render is attacker-influenced: action-item notes any workspace member can
write, and AI output that can be steered by prompt injection inside uploaded
documents. Every markdown render that reaches a browser or email goes through
render_markdown so the result is sanitized (nh3) before it is marked safe.
"""

import markdown
import nh3
from django.utils.safestring import mark_safe

# nh3's defaults already cover what markdown emits (headings, lists, tables,
# code/pre, blockquote, links, images) and strip on* handlers and
# javascript: URLs. id/class are added so 'toc' heading anchors and
# fenced-code language classes survive.
_ALLOWED_ATTRIBUTES = {**nh3.ALLOWED_ATTRIBUTES, "*": {"id", "class"}}


def sanitize_html(html):
    """Sanitize an HTML fragment and mark it safe for templates."""
    return mark_safe(nh3.clean(html or "", attributes=_ALLOWED_ATTRIBUTES))


def render_markdown(text, extensions=None):
    """Render markdown to sanitized HTML, marked safe for templates."""
    return sanitize_html(markdown.markdown(text or "", extensions=extensions or []))
