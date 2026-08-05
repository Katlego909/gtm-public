"""Web-access tools for chat agents: fetching a specific URL's text, and
searching the web for current information via Gemini's built-in Google
Search grounding.

Deliberately separate from gtm/agent_actions.py (workspace-collaboration
tools) and gtm/agent_documents.py (this app's own document ecosystem) --
these tools reach outside the app entirely, so they get their own security
posture (SSRF guarding in fetch_url) and their own nested-Gemini-call cost
accounting (web_search), neither of which the other tool modules need.

web_search can't simply add google_search as one more tool alongside this
app's existing custom function-calling tools in the same request -- there's
no first-party confirmation that Vertex AI accepts mixing a built-in
grounding tool with custom function declarations for gemini-2.5-flash, and
even if it did, the SDK's automatic-function-calling loop used everywhere
else in this app can't intercept/execute a server-side built-in tool call.
So web_search makes its own separate, one-shot client.models.generate_content
call with ONLY the google_search tool -- mirroring the existing nested-call
precedent, draft_client_summary_text in gtm/workspace_agent_chat.py -- and
returns the resulting text as this tool's return value, same as any other
Python tool function.
"""

import ipaddress
import logging
import socket
from typing import Any, List
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False


# ================================================================
# fetch_url -- SSRF-guarded single-URL fetch
# ================================================================

WEB_FETCH_TIMEOUT_SECONDS = 10
WEB_FETCH_MAX_BYTES = 2_000_000
WEB_FETCH_TEXT_LIMIT = 4000
WEB_FETCH_MAX_REDIRECTS = 3
WEB_FETCH_USER_AGENT = "GTM-Validator-Agent/1.0"

# Hostnames that resolve fine but are never a legitimate fetch target for
# this tool -- the GCP metadata server can hand out this app's own Vertex
# AI service-account token to anything that can reach it unauthenticated.
_BLOCKED_HOSTNAMES = {"metadata.google.internal"}
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598


def _is_safe_url(url: str) -> bool:
    """Reject anything but a public http(s) URL. Resolves the hostname and
    checks every returned address against the standard private/loopback/
    link-local/reserved/multicast ranges (this alone already catches the
    GCP metadata IP, 169.254.169.254, via is_link_local) plus two explicit
    additions: the metadata hostname itself, and RFC 6598 shared address
    space (100.64.0.0/10), which is_private doesn't cover.

    Known, accepted limitation: this is a resolve-then-connect check, not a
    DNS-rebinding-proof one (the name could re-resolve to a different
    address between this check and the actual request). Full protection
    would mean pinning the resolved IP through the TLS handshake itself --
    more complexity than an internal productivity tool warrants; the
    redirect-hop re-check in _fetch_url_text is the more realistic bypass
    this guards against.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname or hostname.lower() in _BLOCKED_HOSTNAMES:
        return False
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for _family, _type, _proto, _canonname, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
        if ip.version == 4 and ip in _SHARED_ADDRESS_SPACE:
            return False
    return True


def _fetch_url_text(url: str) -> str:
    """Fetch `url` and return its visible text, capped and cleaned. Raises
    ValueError/requests exceptions on failure -- the fetch_url tool closure
    turns those into a friendly string."""
    current_url = url
    for _ in range(WEB_FETCH_MAX_REDIRECTS + 1):
        if not _is_safe_url(current_url):
            raise ValueError("that URL isn't allowed (must be a public http/https address)")

        response = requests.get(
            current_url,
            timeout=WEB_FETCH_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": WEB_FETCH_USER_AGENT},
        )
        if response.is_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("that URL redirected without a destination")
            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not any(marker in content_type.lower() for marker in ("text", "html", "json", "xml")):
            response.close()
            raise ValueError(f"that URL isn't a text/HTML page (content-type: {content_type or 'unknown'})")

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            total += len(chunk)
            if total > WEB_FETCH_MAX_BYTES:
                break
            chunks.append(chunk)
        response.close()

        soup = BeautifulSoup(b"".join(chunks), "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:WEB_FETCH_TEXT_LIMIT]

    raise ValueError("too many redirects")


# ================================================================
# web_search -- nested one-shot Gemini call with Google Search grounding
# ================================================================

_WEB_SEARCH_SYSTEM_INSTRUCTION = (
    "Answer the question concisely and factually using web search results. "
    "Stick to what the search results actually say -- don't speculate beyond them."
)


def _get_web_search_config():
    return types.GenerateContentConfig(
        system_instruction=_WEB_SEARCH_SYSTEM_INSTRUCTION,
        temperature=0.3,
        max_output_tokens=1024,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )


def _web_search_text(query: str, account) -> str:
    """Run one grounded web search and return the answer text. Pure
    generation -- credit-gated only (mirrors draft_client_summary_text in
    gtm/workspace_agent_chat.py); the caller (the web_search tool closure)
    is responsible for the quota-cooldown gate and error handling, since
    this is a standalone Gemini call triggered as a nested chat tool."""
    from .ai_chat import _get_chat_client
    from .ai_credits import record_spend
    from .ai_services import _request_budget_available

    if not _request_budget_available(account=account):
        raise RuntimeError("AI credits exhausted for this period")

    client = _get_chat_client()
    if not client:
        raise RuntimeError("AI client unavailable")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=query)])],
        config=_get_web_search_config(),
    )
    if hasattr(response, "usage_metadata"):
        total_tokens = response.usage_metadata.total_token_count
        if MONITORING_AVAILABLE:
            AIUsageTracker.log_usage(total_tokens, "web_search")
        record_spend(account, total_tokens, "web_search")
    if response.text is None or not response.text.strip():
        raise RuntimeError("Empty response from web search")
    return response.text.strip()


# ================================================================
# TOOLS (FUNCTION CALLING)
# ================================================================

def build_web_tools(*, workspace=None, session=None, user=None) -> List[Any]:
    """fetch_url + web_search tool closures. `web_search` resolves its
    credit-tracking `account` from `workspace` (workspace agents) or
    `session` (Charlie) -- same split used throughout this app."""
    from .ai_credits import resolve_account, resolve_account_for_session
    from .ai_services import (
        _extract_retry_delay_seconds,
        _is_quota_error,
        _quota_cooldown_active,
        _set_quota_cooldown,
    )

    def fetch_url(url: str) -> str:
        """Fetch a specific web page's visible text content -- e.g. a
        prospect's website or a competitor's landing page. `url` must be a
        full http(s) URL. Doesn't work for pages requiring login. Returns
        up to ~4000 characters of visible text.
        """
        try:
            return _fetch_url_text(url)
        except Exception as e:
            return f"I couldn't fetch that URL: {e}"

    def web_search(query: str) -> str:
        """Search the web for current, real-world information not in your
        training data or this workspace's own records -- e.g. industry
        news, a company's public info, competitor research.
        """
        if _quota_cooldown_active():
            return "I'm currently cooling down to stay within my API limits. Please try again in about 60 seconds."

        account = (
            resolve_account(workspace=workspace) if workspace
            else resolve_account_for_session(session, user=user)
        )
        try:
            return _web_search_text(query, account)
        except Exception as e:
            if _is_quota_error(e):
                _set_quota_cooldown(_extract_retry_delay_seconds(e))
                return "I've hit my temporary web search quota. Please try again shortly."
            logger.error(f"web_search tool failure: {e}")
            return "I ran into a technical error searching the web. Please try again."

    return [fetch_url, web_search]
