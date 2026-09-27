"""Choosing a provider."""

from __future__ import annotations

import logging

from core import config


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
