"""
Inbound metrics API: lets a workspace's own tooling (CRM, BI job, Zapier,
etc.) push measured gap-metric values using a WorkspaceApiKey.

Deliberately narrow. It records measurements and reads gaps -- it can't
create gaps, change targets, or resolve anything. A pushed value goes
through the same `record_gap_measurement` path as a manual check-in, so a
gap only closes when real data reaches target.

Auth is `Authorization: Bearer gtmk_...` only; no session/cookie auth, which
is why these views are csrf_exempt.
"""

import json
import math
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from dashboard.models import GapAnalysisMetric, GapMetricMeasurement
from gtm.models_integrations import WorkspaceApiKey

from .helpers import _gap_metric_scope_queryset, record_gap_measurement

MAX_BODY_BYTES = 4096
RATE_LIMIT_MEASUREMENTS = 60
RATE_LIMIT_WINDOW = timedelta(seconds=60)


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


def _authenticate(request):
    header = request.headers.get('Authorization', '')
    scheme, _, token = header.partition(' ')
    if scheme.lower() != 'bearer':
        return None
    return WorkspaceApiKey.authenticate(token.strip())


def _serialize_gap(metric):
    return {
        'metric': metric.metric,
        'category': metric.category,
        'current': metric.current,
        'target': metric.target,
        'status': metric.status,
        'closed_reason': metric.closed_reason or None,
    }


@csrf_exempt
@require_http_methods(["GET"])
def list_gap_metrics(request):
    """List the workspace's flagged gaps -- what a client can push values for."""
    api_key = _authenticate(request)
    if not api_key:
        return _error('Invalid or missing API key.', 401)
    metrics = _gap_metric_scope_queryset(None, api_key.workspace).order_by('metric', '-id')
    return JsonResponse({'gap_metrics': [_serialize_gap(m) for m in metrics]})


@csrf_exempt
@require_http_methods(["POST"])
def create_gap_measurement(request):
    """Record one measured value for a flagged gap metric."""
    api_key = _authenticate(request)
    if not api_key:
        return _error('Invalid or missing API key.', 401)

    if len(request.body) > MAX_BODY_BYTES:
        return _error(f'Request body exceeds {MAX_BODY_BYTES} bytes.', 400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return _error('Request body must be valid JSON.', 400)
    if not isinstance(payload, dict):
        return _error('Request body must be a JSON object.', 400)

    metric_name = payload.get('metric')
    if metric_name not in GapAnalysisMetric.METRIC_FIELD_MAPPING:
        allowed = ', '.join(GapAnalysisMetric.METRIC_FIELD_MAPPING)
        return _error(f'"metric" must be one of: {allowed}.', 400)

    value = payload.get('value')
    # bool is an int subclass; `true` is not a measurement.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return _error('"value" must be a finite number greater than or equal to 0.', 400)

    note = payload.get('note', '')
    if not isinstance(note, str):
        return _error('"note" must be a string.', 400)
    if len(note) > 240:
        return _error('"note" must be 240 characters or fewer.', 400)

    recent = GapMetricMeasurement.objects.filter(
        api_key=api_key, measured_at__gte=timezone.now() - RATE_LIMIT_WINDOW,
    ).count()
    if recent >= RATE_LIMIT_MEASUREMENTS:
        response = _error('Rate limit exceeded. Try again in a minute.', 429)
        response['Retry-After'] = str(int(RATE_LIMIT_WINDOW.total_seconds()))
        return response

    metric = _gap_metric_scope_queryset(None, api_key.workspace, metric_name).order_by('-id').first()
    if not metric:
        return _error(f'"{metric_name}" is not flagged as a gap in this workspace.', 404)

    closed = record_gap_measurement(
        metric, float(value), source='api',
        note=note.strip() or f'Pushed via API key "{api_key.name}".', api_key=api_key,
    )
    measurement = metric.measurements.order_by('-id').first()
    return JsonResponse({
        **_serialize_gap(metric),
        'value': measurement.value,
        'closed': closed,
        'measured_at': measurement.measured_at.isoformat(),
    }, status=201)
