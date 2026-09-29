"""Choosing a provider."""

from __future__ import annotations

import logging

from core import config


log = logging.getLogger(__name__)

PROVIDERS = ("openai_compatible", "anthropic")

GATEWAY_SETTINGS = ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL",
                    "OPENAI_COMPAT_API_KEY")


def openai_compatible_is_configured() -> bool:
    """A key alone is not enough: without a base URL and a model id there is
    nothing to call."""
    return all(config.get(name) for name in GATEWAY_SETTINGS)


def create(name: str | None = None):
    """Build a provider.

    LLM_PROVIDER=openai_compatible with an incompletely configured gateway
    falls back to Anthropic, loudly. The fallback is deliberate rather than a
    convenience: the OpenAI-compatible path has never been run against a real
    gateway (see llm/openai_compatible.py), and stopping the whole project on
    missing gateway settings would be the wrong trade. Keeping the setting as
    `openai_compatible` means that filling those values in switches everything
    over with no code change -- which is what the provider boundary was built
    for.

    It is never silent. A fallback nobody notices is how an evaluation of one
    model quietly becomes an evaluation of another.
    """
    chosen = (name or config.get("LLM_PROVIDER", "anthropic")).lower()

    if chosen == "openai_compatible" and not openai_compatible_is_configured():
        missing = [n for n in GATEWAY_SETTINGS if not config.get(n)]
        log.warning(
            "LLM_PROVIDER=openai_compatible but %s not set; falling back to "
            "Anthropic. "
            "Set them to use the gateway -- no code change needed.",
            ", ".join(missing))
        chosen = "anthropic"

    if chosen == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if chosen == "openai_compatible":
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider()
    raise ValueError(f"Unknown LLM_PROVIDER {chosen!r}. Use one of: "
                     f"{', '.join(PROVIDERS)}")
