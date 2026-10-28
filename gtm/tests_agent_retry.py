"""Transient Gemini errors (gtm/agent_runtime.py): rate limits and outages
are retried, but never after a write tool ran -- that would do it twice --
and Vertex 429s are recognised as quota errors."""

from unittest import mock

from django.test import SimpleTestCase
from google.genai import errors, types

from gtm import agent_runtime
from gtm.agent_runtime import PartialTurnError, run_agent_turn, send_with_retry
from gtm.ai_services import _is_quota_error
from gtm.tests_agent_runs import AgentRunTestBase, _FakeClient, _propose


def _server_error():
    return errors.ServerError(503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}})


def _rate_limit():
    return errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted.", "status": "RESOURCE_EXHAUSTED"}})


class _Response:
    def __init__(self, text):
        self.text, self.usage_metadata = text, None


class _ScriptedChat:
    """send_message runs the next step: an exception to raise, or a callable
    given the chat's tools by name."""

    def __init__(self, steps, tools=()):
        self.steps, self.tools, self.calls = list(steps), {t.__name__: t for t in tools}, 0

    def send_message(self, message):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Response(step(self.tools) if callable(step) else step)


class _Client:
    def __init__(self, steps):
        self.steps, self.chats, self.chat = steps, self, None

    def create(self, model, config, history):
        self.chat = _ScriptedChat(self.steps, config.tools or ())
        return self.chat


@mock.patch.object(agent_runtime.time, "sleep")
class SendWithRetryTests(SimpleTestCase):
    def test_vertex_rate_limit_counts_as_quota(self, _sleep):
        self.assertTrue(_is_quota_error(_rate_limit()))
        self.assertFalse(_is_quota_error(_server_error()))

    def test_transient_errors_are_retried(self, sleep):
        chat = _ScriptedChat([_server_error(), _rate_limit(), "Answer."])
        self.assertEqual(send_with_retry(chat, "hi").text, "Answer.")
        self.assertEqual(chat.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3, 8])

    def test_gives_up_after_the_retries(self, _sleep):
        chat = _ScriptedChat([_server_error(), _server_error(), _server_error()])
        with self.assertRaises(errors.ServerError):
            send_with_retry(chat, "hi")
        self.assertEqual(chat.calls, 3)

    def test_other_errors_are_not_retried(self, _sleep):
        chat = _ScriptedChat([ValueError("bad tool schema")])
        with self.assertRaises(ValueError):
            send_with_retry(chat, "hi")
        self.assertEqual(chat.calls, 1)


@mock.patch.object(agent_runtime.time, "sleep")
class RunAgentTurnTests(SimpleTestCase):
    def _config(self, *tools):
        return types.GenerateContentConfig(tools=list(tools))

    def test_failure_after_a_read_tool_is_retried(self, _sleep):
        def get_trend_overview() -> str:
            """Read-only."""
            return "Up 4 points."

        def read_then_fail(tools):
            tools["get_trend_overview"]()
            raise _server_error()

        client = _Client([read_then_fail, "Scores are up."])
        text = run_agent_turn(client, "m", self._config(get_trend_overview), [], "trend?")
        self.assertEqual(text, "Scores are up.")
        self.assertEqual(client.chat.calls, 2)

    def test_failure_after_a_write_tool_is_not_retried(self, _sleep):
        created = []

        def notify_teammate(username: str, title: str, message: str) -> str:
            """Write."""
            created.append(username)
            return "Notified."

        def write_then_fail(tools):
            tools["notify_teammate"](username="Priya", title="t", message="m")
            raise _server_error()

        client = _Client([write_then_fail, "should not be reached"])
        with self.assertRaises(PartialTurnError):
            run_agent_turn(client, "m", self._config(notify_teammate), [], "tell Priya")
        self.assertEqual(created, ["Priya"])
        self.assertEqual(client.chat.calls, 1)


class AgentRunRetryTests(AgentRunTestBase):
    @mock.patch.object(agent_runtime.time, "sleep")
    def test_planning_survives_a_transient_error(self, _sleep):
        planner = _propose(("create_document", "ICP one-pager"))

        def flaky(tools, config):
            if not getattr(self, "failed_once", False):
                self.failed_once = True
                raise _server_error()
            return planner(tools, config)

        self._use(flaky, planner)
        run = self._start()
        run.refresh_from_db()
        self.assertEqual(run.status, "awaiting_approval")


class _Usage:
    def __init__(self, total):
        self.total_token_count = total


class _UsageResponse:
    def __init__(self, total, text="ok"):
        self.usage_metadata, self.text = _Usage(total), text


class _FakeModels:
    """Stands in for client.models: each _generate_content is one request."""

    def __init__(self, totals):
        self.totals = list(totals)

    def _generate_content(self, **kwargs):
        return _UsageResponse(self.totals.pop(0))


class _FakeGenaiClient:
    def __init__(self, totals):
        self.models = _FakeModels(totals)


class UsageCountingTests(SimpleTestCase):
    def test_sdk_still_has_the_hooked_per_request_method(self):
        from google.genai import models

        self.assertTrue(hasattr(models.Models, "_generate_content"))

    def test_every_request_in_a_scope_is_counted(self):
        from gtm.agent_runtime import charged_tokens, count_usage, install_usage_counter

        client = install_usage_counter(_FakeGenaiClient([5000, 6000, 7000]))
        with count_usage() as usage:
            for _ in range(3):  # three function-calling rounds of one turn
                last = client.models._generate_content()
        self.assertEqual(charged_tokens(usage, last), 18000)
        install_usage_counter(client)  # idempotent: no double wrapping
        self.assertTrue(client.models._generate_content._counts_usage)

    def test_a_nested_turn_is_not_charged_to_the_outer_one(self):
        from gtm.agent_runtime import count_usage, install_usage_counter

        client = install_usage_counter(_FakeGenaiClient([1000, 2000, 3000]))
        with count_usage() as outer:
            client.models._generate_content()
            with count_usage() as nested:  # e.g. a consulted agent's own turn
                client.models._generate_content()
            client.models._generate_content()
        self.assertEqual(outer["tokens"], 4000)
        self.assertEqual(nested["tokens"], 2000)

    def test_falls_back_to_the_response_without_a_counter(self):
        from gtm.agent_runtime import charged_tokens, count_usage

        with count_usage() as usage:
            pass
        self.assertEqual(charged_tokens(usage, _UsageResponse(900)), 900)
