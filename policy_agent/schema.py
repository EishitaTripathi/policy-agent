"""
COMPONENT: schema
DESIGN-REF: D9 (structured response schema with quote-grounded citations)
            + D12 (Grey-required cost_assessment)
PURPOSE: Pydantic models that all components downstream of the reasoning
  agent consume. The schema is the contract between the agent (proposer),
  the dispatcher (PEP), the filter, the citation verifier, the leak
  detector, and the consistency reviewer.
PROBLEM-STATEMENT REQ (verbatim): >
  "Every action or denial must cite the relevant policy section. For
  denials, the agent should explain why and offer an alternative path
  (e.g., escalation). For ambiguous cases, the agent should explain its
  reasoning. ... The agent must log its reasoning for each decision."
EXPECTED INPUT: dict from the LLM (validated on construction)
EXPECTED OUTPUT: AgentResponse with all required fields populated
UPSTREAM: policy_agent.agent
DOWNSTREAM: every step after step 4 in the architecture diagram
COMPONENT TESTS: tests/whitebox/test_schema.py
SCENARIO COVERAGE: foundation for all 21 scenarios.

Quote-grounded citations (per the post-pilot research-backed revision):
  - section_id: must exist in the policy index
  - quote: must be a verbatim substring of one of the retrieved chunks
The citation verifier (D5) checks both.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Decision = Literal["allow", "deny", "escalate", "clarify"]
Severity = Literal["low", "medium", "high"]
Reversibility = Literal["reversible", "partially_reversible", "irreversible"]
AffectsScope = Literal["self", "single_other", "multiple", "system"]
# Aligned with `Decision` so D13's structural check (chosen_path ==
# decision) is a direct string equality with no equivalence mapping
# needed. The original D12 design used {act, refuse, escalate, clarify}
# which read more naturally in the cost-assessment frame, but it
# created a vocabulary mismatch with the top-level `decision` field
# and caused spurious cost_path_mismatch drifts on otherwise-correct
# Grey responses (e.g., scenario #12 in the v1 pilot). Per the
# "fix the source" principle, the two enums are now the same.
ChosenPath = Decision


class Citation(BaseModel):
    """A reference to a policy clause. The verifier (D5) asserts that
    `section_id` exists in the index and `quote` is a verbatim substring
    of one of the retrieved chunks (whitespace-normalized)."""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(
        description="The policy clause ID, e.g. '4.2' or '15.3'. "
        "Must match a clause in the retrieved chunks."
    )
    quote: str = Field(
        description="A verbatim quote from the cited policy clause. "
        "Must be a substring of one of the retrieved chunks."
    )

    @field_validator("section_id")
    @classmethod
    def _normalize_id(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("§"):
            v = v[1:].strip()
        return v


class ToolCall(BaseModel):
    """A proposed call. The dispatcher (D3) decides whether to authorize."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="One of the registered tool names.")
    args: dict[str, Any] = Field(default_factory=dict)


class CostAssessment(BaseModel):
    """D12 — required for Grey responses; optional for Blue when the
    agent flags ambiguity; absent for Red (Red has a deterministic path).

    The schema makes the rubric's 'weigh risk of acting versus cost of
    refusing' a structural requirement, not an unauditable vibe."""

    model_config = ConfigDict(extra="forbid")

    harm_if_acted_wrongly: Severity
    harm_if_acted_wrongly_reason: str
    harm_if_refused_wrongly: Severity
    harm_if_refused_wrongly_reason: str
    reversibility: Reversibility
    affects: AffectsScope
    chosen_path: ChosenPath
    justification: str = Field(
        description="Why this path was chosen given the asymmetry above."
    )


class Escalation(BaseModel):
    """Per §5.4: when escalating, the agent must provide the human
    operator with a summary of the conversation and the reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    conversation_summary: str


class AgentResponse(BaseModel):
    """The single contract emitted by the reasoning agent (D1) and
    consumed by every downstream component."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    action: str = Field(
        description="One- to two-sentence description of what the agent "
        "is doing or refusing to do, addressed to the user."
    )
    tool_calls: list[ToolCall] = Field(default_factory=list)
    citations: list[Citation] = Field(
        default_factory=list,
        description="Required for deny / escalate decisions per §6.1; "
        "recommended for allow when the decision references policy.",
    )
    reasoning: str = Field(
        description="The agent's chain of thought, 1-3 sentences. "
        "Inspectable in the decision log."
    )
    escalation: Escalation | None = None
    cost_assessment: CostAssessment | None = None

    @field_validator("citations")
    @classmethod
    def _no_empty_quotes(cls, v: list[Citation]) -> list[Citation]:
        for c in v:
            if not c.quote.strip():
                raise ValueError(
                    f"citation §{c.section_id} has an empty quote; "
                    "cite verbatim text from the retrieved chunks"
                )
        return v

    def format_for_user(self) -> str:
        """User-facing rendering: `action` followed by a deterministic
        **Policy:** block reproducing the verbatim policy text for every
        cited section.

        `Citation.quote` has already been verified verbatim against the
        retrieved chunks by D5 (deterministic substring check), so
        rendering it here cannot leak hallucinated policy text. This is the
        single source of truth for "how a decision is shown to an end
        user" — Gradio, a future API, the CLI, and any other surface
        consume this method instead of re-deriving the format.

        Omits the Policy block when there are no citations (rare; D13
        catches unjustified deny/escalate as `missing_citations`).
        """
        if not self.citations:
            return self.action
        quotes = "\n".join(f'- §{c.section_id}: "{c.quote}"' for c in self.citations)
        return f"{self.action}\n\n**Policy:**\n{quotes}"


def schema_for_prompt() -> str:
    """JSON-schema-ish summary for inclusion in the system prompt."""
    return (
        '{"decision":"allow|deny|escalate|clarify",'
        '"action":"<one or two sentences to the user>",'
        '"tool_calls":[{"name":"<tool>","args":{...}}],'
        '"citations":[{"section_id":"<X.Y>","quote":"<verbatim from retrieved chunks>"}],'
        '"reasoning":"<1-3 sentences chain-of-thought>",'
        '"escalation":{"reason":"...","conversation_summary":"..."} | null,'
        '"cost_assessment":{"harm_if_acted_wrongly":"low|medium|high",'
        '"harm_if_acted_wrongly_reason":"...",'
        '"harm_if_refused_wrongly":"low|medium|high",'
        '"harm_if_refused_wrongly_reason":"...",'
        '"reversibility":"reversible|partially_reversible|irreversible",'
        '"affects":"self|single_other|multiple|system",'
        '"chosen_path":"allow|deny|escalate|clarify",'
        '"justification":"..."} | null}'
    )
