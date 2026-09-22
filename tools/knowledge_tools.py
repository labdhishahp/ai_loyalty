"""Knowledge retrieval as a capability the agent chooses to use.

NOT A PREPROCESSING STEP. A common shape is to retrieve documents for the user's
question and staple them to the prompt before the model sees anything. That
retrieves once, against the question as asked, before anything is known.

An investigation does not work that way. The useful query arrives in the middle:
having found that a cohort's decline is concentrated in one category, the
question worth asking the knowledge base is about that category, and it could
not have been asked at the start. So retrieval is a tool, callable repeatedly,
with a different query each time as hypotheses narrow.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from knowledge import retrieval

from .catalog import _Input
from .envelope import ToolResult
from .registry import tool

DOC_TYPES = ("policy", "playbook", "guideline", "postmortem", "memo")


class SearchKnowledgeInput(_Input):
    query: str = Field(
        description="What you want to know, in plain words. Full questions work "
                    "better than keywords.")
    jurisdiction: str | None = Field(
        default=None,
        description="Narrow to one market's documents plus global ones, using a "
                    "country code from get_reference_data. Omit to search "
                    "everything.")
    doc_types: list[Literal["policy", "playbook", "guideline", "postmortem",
                            "memo"]] = Field(
        default_factory=list,
        description="Restrict by kind: policy (rules that must be followed), "
                    "playbook (what to do in a situation), guideline (how to "
                    "measure or write something), postmortem (what happened "
                    "when we tried), memo (a decision or incident record). "
                    "Omit to search all kinds.")
    limit: int = Field(default=6, ge=1, le=12)


@tool("search_knowledge",
      "Searches L-Mart's written knowledge: loyalty and campaign policies, "
      "regional compliance rules, playbooks for common situations, post-mortems "
      "of past campaigns, and memos recording decisions and incidents. Use it "
      "to find WHY something happened when the data only shows THAT it "
      "happened, to check what a rule actually permits before proposing "
      "anything, and to find what was tried before. Call it repeatedly with "
      "different queries as your investigation narrows -- the most useful "
      "question is usually not the one you started with. Superseded documents "
      "are excluded automatically, so what comes back is current.",
      SearchKnowledgeInput)
def search_knowledge(inp: SearchKnowledgeInput, conn) -> ToolResult:
    passages = retrieval.search(
        conn, inp.query, jurisdiction=inp.jurisdiction,
        doc_types=list(inp.doc_types) or None, limit=inp.limit)

    if not passages:
        return ToolResult(
            tool="search_knowledge", call_id="", data=[],
            summary=f"Nothing found for {inp.query!r}. Try fewer or different "
                    f"words, or drop the doc_types filter.")

    return ToolResult(
        tool="search_knowledge", call_id="",
        data=[{"source": p.cite(), "title": p.title, "doc_type": p.doc_type,
               "jurisdiction": p.jurisdiction, "heading": p.heading,
               "effective_from": p.effective_from, "text": p.text}
              for p in passages],
        meta={"query": inp.query, "jurisdiction": inp.jurisdiction or "all",
              "doc_types": list(inp.doc_types) or "all",
              "as_of": date.today()},
        summary=f"{len(passages)} passage(s): "
                + "; ".join(f"{p.title} ({p.doc_type})" for p in passages[:4]))
