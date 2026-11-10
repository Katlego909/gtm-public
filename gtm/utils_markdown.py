"""Markdown -> HTML rendering that is safe to drop into a template with |safe.

Python-Markdown passes raw inline HTML straight through, and much of what we
render is attacker-influenced: action-item notes any workspace member can
write, and AI output that can be steered by prompt injection inside uploaded
documents. Every markdown render that reaches a browser or email goes through
render_markdown so the result is sanitized (nh3) before it is marked safe.
"""

import threading

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


# markdown.markdown() builds a new Markdown instance -- and re-instantiates
# every extension -- on each call. Pages render dozens of snippets, so keep
# one instance per (thread, extension list) and reset() it between documents.
# Instances are not thread-safe, hence thread-local.
_local = threading.local()


def _converter(extensions):
    key = tuple(extensions)
    if not all(isinstance(e, str) for e in key):
        # Extension objects carry their own config/state; don't share them.
        return markdown.Markdown(extensions=list(extensions))
    cache = getattr(_local, "converters", None)
    if cache is None:
        cache = _local.converters = {}
    md = cache.get(key)
    if md is None:
        md = cache[key] = markdown.Markdown(extensions=list(extensions))
    return md


def render_markdown(text, extensions=None):
    """Render markdown to sanitized HTML, marked safe for templates."""
    if not text:
        return mark_safe("")
    return sanitize_html(_converter(extensions or []).reset().convert(text))
