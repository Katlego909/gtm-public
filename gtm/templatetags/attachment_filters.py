from pathlib import PurePath

from django import template

register = template.Library()


@register.filter
def attachment_kind(name):
    """Short file-type badge for a chat attachment card, e.g. 'PDF', 'CSV'.

    Mirrors fileKindLabel() in dashboard/partials/agent_content.html so
    server-rendered history and live sends show the same badge."""
    suffix = PurePath(str(name or "")).suffix.lstrip(".")
    return (suffix or "file").upper()[:4]
