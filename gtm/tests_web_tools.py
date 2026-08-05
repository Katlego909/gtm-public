"""
Tests for gtm/web_tools.py -- the SSRF-guarded fetch_url tool and the
Google-Search-grounded web_search tool.

Network calls and Gemini calls are always mocked here -- no real DNS
resolution, HTTP requests, or Vertex AI calls happen in this suite. See the
plan's verification step for a manual smoke test of _is_safe_url against
real values before trusting this alone.
"""

import socket
from unittest import mock

from django.test import TestCase

from gtm.web_tools import _fetch_url_text, _is_safe_url, _web_search_text, build_web_tools


def _addrinfo(ip: str):
    """Shape socket.getaddrinfo returns for one resolved IPv4 address."""
    return [(2, 1, 6, "", (ip, 0))]


class IsSafeUrlTests(TestCase):
    def test_blocks_bad_scheme(self):
        self.assertFalse(_is_safe_url("file:///etc/passwd"))

    def test_blocks_no_hostname(self):
        self.assertFalse(_is_safe_url("http:///path"))

    def test_blocks_metadata_hostname(self):
        self.assertFalse(_is_safe_url("http://metadata.google.internal/"))

    def test_blocks_unresolvable_hostname(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            self.assertFalse(_is_safe_url("http://does-not-resolve.invalid/"))

    def test_blocks_metadata_ip(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")):
            self.assertFalse(_is_safe_url("http://169.254.169.254/computeMetadata/v1/"))

    def test_blocks_loopback(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            self.assertFalse(_is_safe_url("http://localhost/"))

    def test_blocks_private_ip(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")):
            self.assertFalse(_is_safe_url("http://internal.example.com/"))

    def test_blocks_shared_address_space(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("100.100.100.200")):
            self.assertFalse(_is_safe_url("http://cgnat.example.com/"))

    def test_allows_public_url(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assertTrue(_is_safe_url("https://example.com/"))


class FetchUrlTextTests(TestCase):
    def _make_response(self, *, is_redirect=False, headers=None, chunks=None, location=None):
        response = mock.Mock()
        response.is_redirect = is_redirect
        response.headers = dict(headers or {})
        if location:
            response.headers["Location"] = location
        response.close = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.iter_content = mock.Mock(return_value=chunks or [])
        return response

    def test_extracts_visible_text_and_strips_script(self):
        html = b"<html><body><script>evil()</script><p>Hello world</p></body></html>"
        response = self._make_response(headers={"Content-Type": "text/html"}, chunks=[html])
        with mock.patch("gtm.web_tools._is_safe_url", return_value=True), \
             mock.patch("gtm.web_tools.requests.get", return_value=response):
            result = _fetch_url_text("https://example.com/")
        self.assertIn("Hello world", result)
        self.assertNotIn("evil()", result)

    def test_rejects_unsafe_url_before_any_request(self):
        with mock.patch("gtm.web_tools._is_safe_url", return_value=False), \
             mock.patch("gtm.web_tools.requests.get") as mock_get:
            with self.assertRaises(ValueError):
                _fetch_url_text("http://169.254.169.254/")
        mock_get.assert_not_called()

    def test_rejects_non_text_content_type(self):
        response = self._make_response(headers={"Content-Type": "image/png"})
        with mock.patch("gtm.web_tools._is_safe_url", return_value=True), \
             mock.patch("gtm.web_tools.requests.get", return_value=response):
            with self.assertRaises(ValueError):
                _fetch_url_text("https://example.com/logo.png")

    def test_redirect_to_unsafe_target_is_blocked(self):
        """The classic SSRF-via-redirect bypass: a URL that passes
        _is_safe_url once can still 302 to a blocked address -- this must
        be re-checked on every hop, not just the origin."""
        redirect_response = self._make_response(is_redirect=True, location="http://169.254.169.254/secret")

        def fake_is_safe(url):
            return "169.254.169.254" not in url

        with mock.patch("gtm.web_tools._is_safe_url", side_effect=fake_is_safe), \
             mock.patch("gtm.web_tools.requests.get", return_value=redirect_response):
            with self.assertRaises(ValueError):
                _fetch_url_text("https://example.com/redirect-me")

    def test_too_many_redirects_raises(self):
        redirect_response = self._make_response(is_redirect=True, location="https://example.com/next")
        with mock.patch("gtm.web_tools._is_safe_url", return_value=True), \
             mock.patch("gtm.web_tools.requests.get", return_value=redirect_response):
            with self.assertRaises(ValueError):
                _fetch_url_text("https://example.com/start")


class FetchUrlToolTests(TestCase):
    def test_tool_returns_fetched_text_on_success(self):
        tools = {t.__name__: t for t in build_web_tools()}
        with mock.patch("gtm.web_tools._fetch_url_text", return_value="Hello world"):
            result = tools["fetch_url"]("https://example.com")
        self.assertEqual(result, "Hello world")

    def test_tool_wraps_failure_as_friendly_message(self):
        tools = {t.__name__: t for t in build_web_tools()}
        with mock.patch("gtm.web_tools._fetch_url_text", side_effect=ValueError("blocked")):
            result = tools["fetch_url"]("http://bad.example.com")
        self.assertIn("couldn't fetch", result)


class WebSearchTextTests(TestCase):
    """Direct tests of _web_search_text -- the nested one-shot Gemini call."""

    def test_calls_generate_content_and_records_spend(self):
        mock_client = mock.Mock()
        usage = mock.Mock(total_token_count=123)
        mock_response = mock.Mock(text="Grounded answer", usage_metadata=usage)
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch("gtm.ai_services._request_budget_available", return_value=True), \
             mock.patch("gtm.ai_chat._get_chat_client", return_value=mock_client), \
             mock.patch("gtm.ai_credits.record_spend") as mock_record_spend:
            result = _web_search_text("what's trending in B2B SaaS", account="fake-account")

        self.assertEqual(result, "Grounded answer")
        mock_client.models.generate_content.assert_called_once()
        mock_record_spend.assert_called_once_with("fake-account", 123, "web_search")

    def test_raises_when_credits_exhausted(self):
        with mock.patch("gtm.ai_services._request_budget_available", return_value=False):
            with self.assertRaises(RuntimeError):
                _web_search_text("anything", account="fake-account")

    def test_raises_when_no_client(self):
        with mock.patch("gtm.ai_services._request_budget_available", return_value=True), \
             mock.patch("gtm.ai_chat._get_chat_client", return_value=None):
            with self.assertRaises(RuntimeError):
                _web_search_text("anything", account="fake-account")

    def test_raises_on_empty_response(self):
        mock_client = mock.Mock()
        mock_response = mock.Mock(spec=["text"], text="")
        mock_client.models.generate_content.return_value = mock_response
        with mock.patch("gtm.ai_services._request_budget_available", return_value=True), \
             mock.patch("gtm.ai_chat._get_chat_client", return_value=mock_client):
            with self.assertRaises(RuntimeError):
                _web_search_text("anything", account="fake-account")


class WebSearchToolTests(TestCase):
    """The web_search tool closure built by build_web_tools -- quota
    cooldown gating and quota-error classification, layered on top of
    _web_search_text (mocked here so these tests only exercise the
    closure's own logic)."""

    def test_short_circuits_when_cooldown_active(self):
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=True), \
             mock.patch("gtm.ai_credits.resolve_account", return_value="fake-account"), \
             mock.patch("gtm.web_tools._web_search_text") as mock_search_text:
            tools = {t.__name__: t for t in build_web_tools(workspace="fake-workspace")}
            result = tools["web_search"]("what's new in GTM strategy")
        mock_search_text.assert_not_called()
        self.assertIn("cooling down", result)

    def test_returns_search_result_on_success(self):
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_credits.resolve_account", return_value="fake-account"), \
             mock.patch("gtm.web_tools._web_search_text", return_value="Grounded answer text"):
            tools = {t.__name__: t for t in build_web_tools(workspace="fake-workspace")}
            result = tools["web_search"]("what's new in GTM strategy")
        self.assertEqual(result, "Grounded answer text")

    def test_quota_error_sets_cooldown(self):
        boom = RuntimeError("quota exceeded")
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_credits.resolve_account", return_value="fake-account"), \
             mock.patch("gtm.web_tools._web_search_text", side_effect=boom), \
             mock.patch("gtm.ai_services._is_quota_error", return_value=True), \
             mock.patch("gtm.ai_services._extract_retry_delay_seconds", return_value=30), \
             mock.patch("gtm.ai_services._set_quota_cooldown") as mock_set_cooldown:
            tools = {t.__name__: t for t in build_web_tools(workspace="fake-workspace")}
            result = tools["web_search"]("what's new in GTM strategy")
        mock_set_cooldown.assert_called_once_with(30)
        self.assertIn("quota", result.lower())

    def test_non_quota_error_returns_generic_message_without_setting_cooldown(self):
        boom = RuntimeError("something else broke")
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_credits.resolve_account", return_value="fake-account"), \
             mock.patch("gtm.web_tools._web_search_text", side_effect=boom), \
             mock.patch("gtm.ai_services._is_quota_error", return_value=False), \
             mock.patch("gtm.ai_services._set_quota_cooldown") as mock_set_cooldown:
            tools = {t.__name__: t for t in build_web_tools(workspace="fake-workspace")}
            result = tools["web_search"]("what's new in GTM strategy")
        mock_set_cooldown.assert_not_called()
        self.assertIn("technical error", result)

    def test_resolves_account_from_session_when_no_workspace(self):
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_credits.resolve_account") as mock_resolve_ws, \
             mock.patch("gtm.ai_credits.resolve_account_for_session", return_value="fake-account") as mock_resolve_session, \
             mock.patch("gtm.web_tools._web_search_text", return_value="answer"):
            tools = {t.__name__: t for t in build_web_tools(session="fake-session", user="fake-user")}
            tools["web_search"]("query")
        mock_resolve_ws.assert_not_called()
        mock_resolve_session.assert_called_once_with("fake-session", user="fake-user")
