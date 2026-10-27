# Inbound Metrics API

The inbound API lets a workspace's own tooling, such as a CRM report, a BI job or a Zapier/Make automation, push **measured** values for its gap metrics. A pushed value is handled exactly like a manual check-in on the Gap Analysis panel. It is recorded in the metric's measurement history, becomes the metric's current value, and **closes the gap when it reaches the target**.

The API is deliberately narrow. It cannot create gaps, change targets or resolve a gap manually. Those stay in the app, decided by the assessment and by people.

## API keys

Workspace admins and Funti3r consultants (`can_manage_integrations`) manage keys under **Workspace Hub > API Keys**:

- **Create.** Give the key a name, such as "HubSpot nightly sync". The full key (`gtmk_...`) is shown **once**. Only its SHA-256 hash is stored, so a lost key can't be recovered. Revoke it and create a new one.
- **Revoke.** The key stops working immediately.

Each key belongs to one workspace and can only read and write that workspace's gap metrics. Every request authenticates with a header:

```
Authorization: Bearer gtmk_your_key_here
```

The API has no session or cookie authentication. Being logged in to the app does not grant API access.

## Endpoints

### `GET /api/v1/gap-metrics/`

Lists the workspace's flagged gaps, which are the metrics you can push values for. It's also a cheap way to check that a key works.

```json
{
  "gap_metrics": [
    {"metric": "Win Rate", "category": "Sales Efficiency", "current": 30.0,
     "target": 40.0, "status": "open", "closed_reason": null}
  ]
}
```

### `POST /api/v1/gap-metrics/measurements/`

Records one measured value.

| Field | Required | Notes |
|---|---|---|
| `metric` | yes | One of `Monthly Qualified Leads`, `Average Deal Size`, `Net Revenue Retention`, `Product Qualified Leads`, `Win Rate`, `CAC Payback Period` |
| `value` | yes | A finite number ≥ 0, in the same units as the gap's target (percent for Win Rate / NRR, months for CAC Payback) |
| `note` | no | Up to 240 characters. Defaults to `Pushed via API key "<name>".` |

```bash
curl -X POST https://forge.funti3r.xyz/api/v1/gap-metrics/measurements/ \
  -H "Authorization: Bearer gtmk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"metric": "Win Rate", "value": 41, "note": "HubSpot closed-won / closed, last 90 days"}'
```

```json
{"metric": "Win Rate", "category": "Sales Efficiency", "current": 41.0, "target": 40.0,
 "status": "closed", "closed_reason": "target_reached", "value": 41.0, "closed": true,
 "measured_at": "2026-10-27T09:15:02.481Z"}
```

**Closure direction.** For every metric except CAC Payback Period, a value **at or above** the target closes the gap. For CAC Payback Period, lower is better, so a value **at or below** the target closes it. A value that falls short leaves the gap open, and its measurement is still recorded.

## Status codes

| Code | Meaning |
|---|---|
| `200` / `201` | Success |
| `400` | The body isn't a JSON object, is larger than 4 KB, names an unknown metric, has an invalid value, or has an invalid note. The `error` field explains which. |
| `401` | The key is missing, unknown or revoked, or the workspace is inactive |
| `404` | The metric isn't flagged as a gap in this workspace. Flag it in the app first. |
| `405` | Wrong HTTP method |
| `429` | More than 60 measurements from this key in the last minute. Honour `Retry-After`. |

Every error response has the shape `{"error": "..."}`.

## How it fits the closure rule

A gap closes only on measured data or when a person explicitly resolves it. AI estimates and task completion never close one. API pushes are measured data, so they go through the same `record_gap_measurement` helper (`dashboard/views/helpers.py`) as the manual check-in form and the chat agent's `log_gap_measurement_from_chat`. In the measurement history they're tagged `source='api'`, and each one is linked to the key that sent it.
