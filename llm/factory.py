"""Choosing a provider, and finding out what it can actually do."""

from __future__ import annotations

import logging

from core import config

from .base import Capabilities, LLMError, ToolSpec, UserMessage

log = logging.getLogger(__name__)

PROVIDERS = ("coe", "anthropic")


def coe_is_configured() -> bool:
    """A key alone is not enough: without a base URL and a model id there is
    nothing to call."""
    return all(config.get(name) for name in
               ("COE_BASE_URL", "COE_MODEL", "COE_API_KEY"))


def create(name: str | None = None):
    """Build a provider.

    LLM_PROVIDER=coe with an incompletely configured gateway falls back to
    Anthropic, loudly. The fallback is deliberate rather than a convenience:
    the gateway's base URL and model id could not be discovered (see
    docs/coe-gateway-investigation.md), and stopping the whole project on two
    missing strings would be the wrong trade. Keeping the setting as `coe` means
    that filling those two values in switches everything over with no code
    change -- which is what the provider boundary was built for.

    It is never silent. A fallback nobody notices is how a Qwen evaluation
    quietly becomes a Claude evaluation.
    """
    chosen = (name or config.get("LLM_PROVIDER", "anthropic")).lower()

    if chosen == "coe" and not coe_is_configured():
        missing = [n for n in ("COE_BASE_URL", "COE_MODEL", "COE_API_KEY")
                   if not config.get(n)]
        log.warning(
            "LLM_PROVIDER=coe but %s not set; falling back to Anthropic. "
            "Set them to use the gateway -- no code change needed.",
            ", ".join(missing))
        chosen = "anthropic"

    if chosen == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if chosen == "coe":
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider()
    raise ValueError(f"Unknown LLM_PROVIDER {chosen!r}. Use one of: "
                     f"{', '.join(PROVIDERS)}")


PROBE_TOOL = ToolSpec(
    name="ping", description="Returns the word pong. Call it exactly once.",
    input_schema={"type": "object", "properties": {"note": {"type": "string"}},
                  "required": ["note"], "additionalProperties": False})


def probe(provider) -> Capabilities:
    """Establish what a provider supports, by asking it rather than assuming.

    Exists because "OpenAI-compatible" describes a URL shape, not a feature set:
    some gateways expose chat completions and no `tools` parameter at all. The
    entire agent design rests on tool calling, so this runs before anything is
    built on top of a new endpoint.
    """
    caps = Capabilities()
    try:
        plain = provider.complete(
            system="Answer in one word.",
            messages=[UserMessage("Say OK.")], tools=[], max_tokens=64)
        caps.reachable = True
        caps.generation = bool(plain.text)
        caps.notes.append(f"generation: stop_reason={plain.stop_reason}")
    except LLMError as exc:
        caps.notes.append(f"unreachable: {exc}")
        return caps

    try:
        tooled = provider.complete(
            system="Use the ping tool.",
            messages=[UserMessage("Call ping with note='hello'.")],
            tools=[PROBE_TOOL], max_tokens=256)
        caps.tool_calling = bool(tooled.tool_calls)
        caps.notes.append(
            f"tool calling: {len(tooled.tool_calls)} call(s), "
            f"stop_reason={tooled.stop_reason}")
    except LLMError as exc:
        caps.notes.append(f"tool calling unsupported: {exc}")

    return caps
