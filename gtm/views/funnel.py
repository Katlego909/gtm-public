# gtm/views/funnel.py
"""Staff-only signup funnel and feature usage (logic lives in gtm/funnel.py)."""
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone

from ..funnel import build_funnel

WINDOWS = {"30": 30, "90": 90, "365": 365}
DEFAULT_WINDOW = "90"


@staff_member_required
def beta_funnel(request):
    window = request.GET.get("days", DEFAULT_WINDOW)
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    now = timezone.now()
    start = now - timedelta(days=WINDOWS[window])
    steps, features, cohort_size = build_funnel(start, now, now)
    return render(request, "gtm/beta_funnel.html", {
        "steps": steps,
        "features": features,
        "cohort_size": cohort_size,
        "window": window,
        "windows": list(WINDOWS),
    })
