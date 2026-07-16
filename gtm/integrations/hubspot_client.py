"""Thin wrapper around HubSpot's CRM REST API (api.hubapi.com).

Deliberately plain `requests` rather than the hubspot-api-client SDK -- this
integration only needs 5 endpoints (companies read/search, notes, tasks), and
a generated SDK would pull in serialization for deals/tickets/contacts this
feature never touches. Mirrors the lazy-thin-wrapper style of
gtm/ai_services.py's Gemini client rather than that module's quota-cooldown
machinery, which doesn't apply to HubSpot's simpler rate limiting.
"""
from __future__ import annotations

from typing import Optional

import requests

BASE_URL = "https://api.hubapi.com"


class HubSpotAPIError(Exception):
    """Raised for any non-2xx HubSpot API response other than an auth failure."""


class HubSpotAuthError(HubSpotAPIError):
    """Raised when HubSpot rejects the access token (401)."""


class HubSpotClient:
    def __init__(self, access_token: str, timeout: int = 10):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self._session.request(method, f"{BASE_URL}{path}", timeout=self._timeout, **kwargs)
        if response.status_code == 401:
            raise HubSpotAuthError("HubSpot rejected this access token. It may be invalid or revoked.")
        if response.status_code >= 400:
            raise HubSpotAPIError(f"HubSpot API error ({response.status_code}): {response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    def test_connection(self) -> tuple[bool, str]:
        """Verify the token is valid and has at least companies-read scope."""
        try:
            self._request("GET", "/crm/v3/objects/companies", params={"limit": 1})
            return True, "Connected."
        except HubSpotAuthError:
            return False, "HubSpot rejected this token. Check it hasn't been revoked."
        except HubSpotAPIError as exc:
            return False, str(exc)

    def find_company(self, identifier: str) -> Optional[dict]:
        """Find a company by domain (exact) or name (contains). Returns None if
        there's no match OR more than one candidate matched by name -- callers
        should ask for a more specific identifier rather than guess."""
        identifier = (identifier or "").strip()
        if not identifier:
            return None

        properties = ["name", "domain", "industry", "numberofemployees", "lifecyclestage"]

        if "." in identifier and " " not in identifier:
            result = self._request("POST", "/crm/v3/objects/companies/search", json={
                "filterGroups": [{"filters": [
                    {"propertyName": "domain", "operator": "EQ", "value": identifier}
                ]}],
                "properties": properties,
                "limit": 2,
            })
            hits = result.get("results", [])
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return None

        result = self._request("POST", "/crm/v3/objects/companies/search", json={
            "filterGroups": [{"filters": [
                {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": identifier}
            ]}],
            "properties": properties,
            "limit": 2,
        })
        hits = result.get("results", [])
        if len(hits) == 1:
            return hits[0]
        return None

    def create_note(self, company_id: str, body: str) -> str:
        note = self._request("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_note_body": body, "hs_timestamp": _now_ms()},
        })
        note_id = note["id"]
        self._request("PUT", f"/crm/v4/objects/notes/{note_id}/associations/default/companies/{company_id}")
        return note_id

    def create_task(self, company_id: str, subject: str, body: str, due_date: Optional[str] = None) -> str:
        properties = {
            "hs_task_subject": subject,
            "hs_task_body": body,
            "hs_task_status": "NOT_STARTED",
            "hs_timestamp": _due_date_ms(due_date),
        }
        task = self._request("POST", "/crm/v3/objects/tasks", json={"properties": properties})
        task_id = task["id"]
        self._request("PUT", f"/crm/v4/objects/tasks/{task_id}/associations/default/companies/{company_id}")
        return task_id

    def update_task(self, task_id: str, subject: str, body: str, due_date: Optional[str] = None) -> None:
        properties = {"hs_task_subject": subject, "hs_task_body": body}
        if due_date:
            properties["hs_timestamp"] = _due_date_ms(due_date)
        self._request("PATCH", f"/crm/v3/objects/tasks/{task_id}", json={"properties": properties})


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _due_date_ms(due_date: Optional[str]) -> int:
    if not due_date:
        return _now_ms()
    from datetime import datetime
    return int(datetime.strptime(due_date, "%Y-%m-%d").timestamp() * 1000)


def get_client_for_workspace(workspace) -> Optional["HubSpotClient"]:
    """Loads the workspace's connected HubSpot integration, if any."""
    from ..models_integrations import WorkspaceIntegration
    from .crypto import CredentialDecryptionError

    try:
        integration = WorkspaceIntegration.objects.get(workspace=workspace, provider="hubspot")
    except WorkspaceIntegration.DoesNotExist:
        return None

    if integration.status != "connected":
        return None

    try:
        token = integration.get_credential()
    except CredentialDecryptionError:
        return None

    return HubSpotClient(token)
