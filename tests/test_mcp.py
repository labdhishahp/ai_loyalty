"""The MCP adapter.

Run over the SDK's in-memory transport: real protocol, real serialisation, real
tool dispatch, no network and no subprocess. That is the right level for these
tests -- what matters is that MCP exposes the SAME tools with the SAME schemas
and the SAME authorization as every other caller, not that HTTP works.

No model calls anywhere.
"""

from __future__ import annotations

import json

import pytest
from mcp import Client

from core.auth import ROLE_SCOPES, Principal
from mcp_server.server import NOT_EXPOSED, build
from tools.registry import REGISTRY

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server_as(role: str, *, scopes=None, actor: str | None = None):
    principal = Principal(actor=actor or f"mcp-{role}", role=role,
                          scopes=scopes if scopes is not None else ROLE_SCOPES[role])
    return build(lambda: principal)


def payload(result) -> dict:
    return json.loads(result.content[0].text)


# ----------------------------------------------------------- the adapter

async def test_mcp_exposes_the_registrys_tools_and_nothing_else():
    """An adapter, not a second catalogue. Anything here that is not in the
    registry would be a capability with no validation and no authorization."""
    async with Client(server_as("analyst")) as client:
        exposed = {t.name for t in (await client.list_tools()).tools}
    assert exposed == {t["name"] for t in REGISTRY.schemas(include_writes=True)} - NOT_EXPOSED


async def test_submit_findings_is_not_exposed():
    """It ends an agent run. An MCP client has no run, so offering it would be
    a capability that cannot mean anything."""
    async with Client(server_as("analyst")) as client:
        assert "submit_findings" not in {t.name for t in (await client.list_tools()).tools}


async def test_schemas_come_from_the_same_pydantic_models():
    """Generated from the registry's input models rather than restated. Two
    definitions of one tool's arguments would drift, and the MCP client would
    be told something the validator does not believe."""
    async with Client(server_as("analyst")) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for spec in REGISTRY.schemas(include_writes=True):
        if spec["name"] in NOT_EXPOSED:
            continue
        ours = set(spec["input_schema"].get("properties", {}))
        theirs = set(tools[spec["name"]].input_schema.get("properties", {}))
        assert ours == theirs, spec["name"]


# ------------------------------------------------------- authorization

async def test_a_read_only_caller_is_refused_a_write_tool():
    """The scope check is the registry's, reached through a different door."""
    async with Client(server_as("analyst", scopes=frozenset({"read"}),
                                actor="laptop")) as client:
        result = payload(await client.call_tool("create_campaign_proposal", {
            "name": "x", "objective": "x", "programme": "x", "channel": "email",
            "offer_type": "points_multiplier", "offer_value": 2.0,
            "rationale": "x", "evidence_call_ids": []}))
    assert result["ok"] is False
    assert result["error"] == "not_permitted"
    assert "propose" in result["message"] and "laptop" in result["message"]


async def test_an_unauthenticated_caller_can_do_nothing():
    """No token is not the same as read-only. An MCP server reachable over a
    network must refuse a caller it cannot identify."""
    async with Client(server_as("none", scopes=frozenset())) as client:
        result = payload(await client.call_tool("list_metrics", {}))
    assert result["ok"] is False and result["error"] == "not_permitted"


async def test_the_cohort_guard_rail_fires_through_mcp_too():
    """The rule worth ~21 percentage points is enforced by the metrics layer,
    so it applies to every caller rather than to the agent alone."""
    async with Client(server_as("analyst")) as client:
        result = payload(await client.call_tool("get_metric", {
            "metric": "orders_per_member", "period_start": "2026-06-01",
            "period_end": "2026-09-01", "tiers": ["GOLD"]}))
    assert result["ok"] is False
    assert "tier_as_of" in result["message"]


# ---------------------------------------------------------- resources

async def test_resources_and_tools_are_different_primitives():
    """A tool is an action a model chooses; a resource is content a client
    addresses by URI. Exposing both is what makes this MCP rather than a
    tool list with extra steps."""
    async with Client(server_as("analyst")) as client:
        static = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
    assert [str(r.uri) for r in static] == ["lmart://knowledge"]
    assert [r.uri_template for r in templates] == ["lmart://knowledge/{slug}"]


async def test_a_document_can_be_read_by_uri_with_no_model_involved():
    async with Client(server_as("analyst")) as client:
        result = await client.read_resource(
            "lmart://knowledge/campaign-governance-policy")
    body = result.contents[0].text
    assert body.startswith("# Campaign Governance Policy")
    assert "Discount ceilings" in body


async def test_a_superseded_document_is_not_served():
    """Retrieval excludes withdrawn policy, and so must direct addressing --
    otherwise the safest path to a stale rule is to ask for it by name."""
    async with Client(server_as("analyst")) as client:
        with pytest.raises(Exception):
            await client.read_resource(
                "lmart://knowledge/points-validity-policy-2024")


async def test_the_index_lists_only_current_documents():
    async with Client(server_as("analyst")) as client:
        result = await client.read_resource("lmart://knowledge")
    documents = json.loads(result.contents[0].text)
    slugs = {d["slug"] for d in documents}
    assert "campaign-governance-policy" in slugs
    assert "points-validity-policy-2024" not in slugs
    assert all(d["uri"] == f"lmart://knowledge/{d['slug']}" for d in documents)


# ---------------------------------------------------------- separation

async def test_the_agent_does_not_route_through_mcp():
    """The architectural decision, asserted rather than left in a comment.

    If the agent called its tools over MCP, the provider would reach the server
    directly and the application would never see the individual calls --
    ops.tool_calls would go blind, and with it the trace the eval reads and the
    UI renders.
    """
    import inspect

    from agent import runtime
    source = inspect.getsource(runtime)
    assert "REGISTRY.dispatch" in source
    assert "mcp" not in source.lower().replace("mcp_server", "")
