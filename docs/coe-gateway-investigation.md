# CoE AI Gateway — investigation record

**Outcome: not identified. Anthropic used as the documented fallback.**

`COE_API_KEY` is present and well-formed (`sk-` + 32 characters, no dots — the
shape LiteLLM issues for virtual keys, which is the usual form for an
enterprise OpenAI-compatible gateway). But a key is not usable without a base
URL and a model id, and neither is discoverable from any source available here.

## What was searched

| Source | Result |
| ------ | ------ |
| `ai_loyalty` repo, all files and full git history | Only the placeholders this project wrote itself |
| Every `.env*` under `~/Documents/LABDHI` (5 projects) | No gateway URL. `RetailIQ` has an `llm_base_url` setting — empty |
| Sibling projects `rag`, `RetailIQ`, `internship_project`, `trial_project` | Gemini, Anthropic and OpenAI defaults only; no internal endpoint |
| Shell history (`.zsh_history`, `.bash_history`) | No `curl`, `base_url`, `litellm` or gateway invocation |
| `~/.config`, shell profiles, home directories | Nothing |
| Installed packages | No gateway SDK or vendored client |
| Gmail — broad (`CoE`/`AI Gateway`/`LiteLLM`/`Qwen`) | AI newsletters only |
| Gmail — internal senders + access/key/onboarding terms | aiRA error alerts, HR mail; no credential issue mail |
| Confluence + Jira (Rovo search, two query formulations) | Capillary has B2C Gateway, API Gateway Webhooks, NGINX Gateway Fabric, Neo AI Service — none is an OpenAI-compatible LLM gateway |
| Google Drive full-text | "COE" matches are AWS "Correction of Errors" documents |

## What was deliberately not done

**Hostname guessing.** Candidate internal hostnames were not probed. The
instruction was explicit, and probing guessed hosts with a live credential is
also how a key ends up in an unrelated party's logs.

**No unrelated public gateway** was substituted.

## What unblocks it

Two values in `.env`, and nothing else changes:

```
COE_BASE_URL=   # must end in /v1
COE_MODEL=      # exact id the gateway expects
LLM_PROVIDER=coe
```

`llm/openai_compatible.py` is written, exercises the same provider interface as
Anthropic, and is covered by the same contract tests. The switch is configuration.
On first successful connection the gateway is probed for tool calling, streaming
and structured output, and the result recorded here.

## Fallback in use

`LLM_PROVIDER=anthropic`, model `claude-opus-5`. Verified: tool calling returns a
well-formed `tool_use` block. Every run records the provider and model that
produced it, so a Qwen-vs-Claude comparison on identical eval questions is a
configuration change, not a rewrite — which was the point of the boundary.
