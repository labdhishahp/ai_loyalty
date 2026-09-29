"""The OpenAI-compatible adapter, against a fake gateway.

WHY THIS EXISTS. This provider was written for an OpenAI-compatible gateway
whose base URL and model id were not available while it was built, so it has
never made a real request.
Until now it also had no tests, which meant the repository contained a working-
looking fallback that nothing had ever executed -- a claim rather than a fact.

WHAT THESE TESTS ACTUALLY COVER, stated honestly: the translation between the
neutral message format and the OpenAI wire format, in both directions, and the
failure paths. That is the part this file owns and the part that would silently
corrupt a conversation if it were wrong.

WHAT THEY CANNOT COVER: that a real gateway accepts these requests. No test
short of a live call can establish that, and a live call needs a URL that does
not exist. Whether `tools` is even implemented by a given endpoint is a property
of that endpoint, not of this code.

No network, no API key, no cost. The OpenAI client is replaced before it is
constructed, so nothing can dial out even by accident.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

# httpx2, not httpx: that is what the OpenAI SDK actually depends on, and
# openai.APIError annotates its own parameter as `request: httpx2.Request`.
# The old import worked only because a stale httpx happened to be present in
# one developer virtualenv; a clean install has never had it.
import httpx2
import pytest
from openai import APIError

from llm import openai_compatible
from llm.base import (AssistantMessage, LLMError, ToolCall, ToolOutcome,
                      ToolResultsMessage, ToolSpec, UserMessage)


def wire_response(*, content=None, tool_calls=(), finish_reason="stop",
                  prompt_tokens=11, completion_tokens=7):
    """A response shaped like the one an OpenAI-compatible server returns."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content,
                                    tool_calls=list(tool_calls)))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens))


def wire_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments))


class FakeGateway:
    """Records what was sent and returns what it was told to."""

    def __init__(self, response):
        self.response = response
        self.sent: dict | None = None
        self.init_kwargs: dict | None = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.sent = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def gateway(monkeypatch):
    """Build the provider with its HTTP client replaced.

    The substitution happens on the OpenAI class itself, before __init__ runs,
    so no client capable of making a request is ever constructed.
    """
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "some-org/some-model")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://gateway.invalid/v1")

    created: dict = {}

    def build(response):
        fake = FakeGateway(response)

        def fake_openai(**kwargs):
            created.update(kwargs)
            return fake

        monkeypatch.setattr(openai_compatible, "OpenAI", fake_openai)
        provider = openai_compatible.OpenAICompatibleProvider()
        provider.created_with = created
        provider.gateway = fake
        return provider

    return build


# ----------------------------------------------------------- configuration

def test_the_client_is_pointed_at_the_configured_gateway(gateway):
    """A gateway adapter that silently talked to api.openai.com would be worse
    than one that failed: it would work, and bill the wrong account."""
    provider = gateway(wire_response(content="ok"))
    assert provider.created_with["base_url"] == "https://gateway.invalid/v1"
    assert provider.created_with["api_key"] == "not-a-real-key"
    assert provider.model == "some-org/some-model"


def test_a_missing_base_url_is_refused_with_a_usable_message(monkeypatch):
    """The gateway's URL was never discovered, so this is the error a future
    operator will actually meet. It has to say what is missing."""
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    with pytest.raises(Exception) as exc:
        openai_compatible.OpenAICompatibleProvider()
    assert "OPENAI_COMPAT_BASE_URL" in str(exc.value)


# ------------------------------------------------------- request translation

def test_the_system_prompt_becomes_the_first_message(gateway):
    """This protocol has no separate system parameter. Dropping it would leave
    the model with no instructions and no error to say so."""
    provider = gateway(wire_response(content="ok"))
    provider.complete(system="You are an analyst.",
                      messages=[UserMessage("Why?")], tools=[])
    sent = provider.gateway.sent["messages"]
    assert sent[0] == {"role": "system", "content": "You are an analyst."}
    assert sent[1] == {"role": "user", "content": "Why?"}


def test_each_tool_result_becomes_its_own_message(gateway):
    """The one real divergence from Anthropic, and the reason the neutral
    message format exists at all.

    Anthropic takes every result in a single user message; OpenAI takes one
    message per result, each carrying its own tool_call_id. Collapsing two
    results into one message here would silently drop the second.
    """
    provider = gateway(wire_response(content="ok"))
    provider.complete(
        system="s",
        messages=[ToolResultsMessage(results=(
            ToolOutcome(call_id="call_a", content="first"),
            ToolOutcome(call_id="call_b", content="second")))],
        tools=[])
    results = [m for m in provider.gateway.sent["messages"]
               if m["role"] == "tool"]
    assert results == [
        {"role": "tool", "tool_call_id": "call_a", "content": "first"},
        {"role": "tool", "tool_call_id": "call_b", "content": "second"},
    ]


def test_assistant_tool_calls_are_sent_with_json_string_arguments(gateway):
    """OpenAI carries arguments as a JSON *string*, not an object. Sending the
    dict would be rejected by a strict gateway and silently mangled by a lax
    one."""
    provider = gateway(wire_response(content="ok"))
    provider.complete(
        system="s",
        messages=[AssistantMessage(
            text=None,
            tool_calls=(ToolCall("call_a", "get_metric",
                                 {"metric": "orders_per_member"}),))],
        tools=[])
    entry = [m for m in provider.gateway.sent["messages"]
             if m["role"] == "assistant"][0]
    function = entry["tool_calls"][0]["function"]
    assert entry["tool_calls"][0]["id"] == "call_a"
    assert function["name"] == "get_metric"
    assert json.loads(function["arguments"]) == {"metric": "orders_per_member"}


def test_tool_schemas_are_wrapped_in_the_function_envelope(gateway):
    provider = gateway(wire_response(content="ok"))
    schema = {"type": "object", "properties": {}}
    provider.complete(system="s", messages=[UserMessage("hi")],
                      tools=[ToolSpec(name="ping", description="Pings.",
                                      input_schema=schema)])
    assert provider.gateway.sent["tools"] == [
        {"type": "function",
         "function": {"name": "ping", "description": "Pings.",
                      "parameters": schema}}]


def test_no_tools_sends_null_rather_than_an_empty_list(gateway):
    """An empty array is rejected by some gateways. None is the documented way
    to say "no tools"."""
    provider = gateway(wire_response(content="ok"))
    provider.complete(system="s", messages=[UserMessage("hi")], tools=[])
    assert provider.gateway.sent["tools"] is None


# ------------------------------------------------------ response translation

def test_a_plain_answer_comes_back_as_neutral_text_and_usage(gateway):
    provider = gateway(wire_response(content="Because of one thing.",
                                     prompt_tokens=120, completion_tokens=34))
    completion = provider.complete(system="s", messages=[UserMessage("Why?")],
                                   tools=[])
    assert completion.text == "Because of one thing."
    assert completion.tool_calls == ()
    assert completion.stop_reason == "end_turn"
    assert completion.usage.input_tokens == 120
    assert completion.usage.output_tokens == 34


def test_tool_calls_are_parsed_into_the_neutral_shape(gateway):
    provider = gateway(wire_response(
        finish_reason="tool_calls",
        tool_calls=[wire_tool_call("call_1", "get_metric",
                                   '{"metric": "orders_per_member"}')]))
    completion = provider.complete(system="s", messages=[UserMessage("Why?")],
                                   tools=[])
    assert completion.stop_reason == "tool_use"
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert (call.id, call.name) == ("call_1", "get_metric")
    assert call.arguments == {"metric": "orders_per_member"}


def test_absent_arguments_parse_as_an_empty_object(gateway):
    """A no-argument tool may arrive with arguments as "" or null."""
    provider = gateway(wire_response(
        finish_reason="tool_calls",
        tool_calls=[wire_tool_call("call_1", "data_as_of", "")]))
    completion = provider.complete(system="s", messages=[UserMessage("?")],
                                   tools=[])
    assert completion.tool_calls[0].arguments == {}


def test_malformed_arguments_are_a_tool_failure_not_a_crash(gateway):
    """A model emitting truncated JSON must not take the process down. The
    loop hands the error back and the model usually corrects itself."""
    provider = gateway(wire_response(
        finish_reason="tool_calls",
        tool_calls=[wire_tool_call("call_1", "get_metric", '{"metric": ')]))
    completion = provider.complete(system="s", messages=[UserMessage("?")],
                                   tools=[])
    assert completion.tool_calls[0].arguments == {
        "__invalid_json__": '{"metric": '}


@pytest.mark.parametrize("finish_reason,expected", [
    ("stop", "end_turn"),
    ("tool_calls", "tool_use"),
    ("length", "max_tokens"),
    ("content_filter", "refusal"),
    ("something_new", "other"),
])
def test_finish_reasons_map_onto_the_neutral_vocabulary(gateway, finish_reason,
                                                        expected):
    """The runtime branches on stop_reason. An unmapped value must land on
    "other" rather than leaking a provider-specific string into the loop."""
    provider = gateway(wire_response(content="x", finish_reason=finish_reason))
    completion = provider.complete(system="s", messages=[UserMessage("?")],
                                   tools=[])
    assert completion.stop_reason == expected


def test_missing_usage_counts_as_zero_rather_than_failing(gateway):
    """Not every gateway reports usage. Losing the token count is acceptable;
    crashing a run over it is not."""
    response = wire_response(content="x")
    response.usage = SimpleNamespace()
    provider = gateway(response)
    completion = provider.complete(system="s", messages=[UserMessage("?")],
                                   tools=[])
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (0, 0)


def test_an_empty_answer_is_none_not_an_empty_string(gateway):
    """A tool-only turn has no text. The runtime distinguishes "said nothing"
    from "said the empty string"."""
    provider = gateway(wire_response(content="", finish_reason="tool_calls"))
    completion = provider.complete(system="s", messages=[UserMessage("?")],
                                   tools=[])
    assert completion.text is None


# ------------------------------------------------------------------ failure

def test_a_gateway_error_is_raised_as_the_neutral_llm_error(gateway):
    """The runtime handles LLMError. An openai.APIError escaping from here
    would cross the provider boundary the whole abstraction exists to hold."""
    failure = APIError("gateway exploded",
                       request=httpx2.Request("POST", "https://gateway.invalid/v1"),
                       body=None)
    provider = gateway(failure)
    with pytest.raises(LLMError) as exc:
        provider.complete(system="s", messages=[UserMessage("?")], tools=[])
    assert "gateway" in str(exc.value).lower()
