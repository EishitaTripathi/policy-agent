"""
COMPONENT: consistency_reviewer
DESIGN-REF: D13 (Final consistency reviewer — REPAIR LOOP)
PURPOSE: Detect cross-component drift in the agent response, classify
  each drift as content (agent re-promptable) vs system (operator alert),
  and provide drift-specific repair feedback for the orchestrator to
  re-enter the agent step. Per-drift-kind repair budgets bound the loop.
  The agent's `decision` field is NEVER overwritten by D13 — `escalate`
  is reserved for the agent's intentional policy choice; D13 surfaces
  internal failures as `pipeline_status = "unresolved_drift"` or
  `"system_error"` on the OrchestratorResult (see orchestrator.py).
PROBLEM-STATEMENT REQ (verbatim): >
  "Failure Mode Awareness — Can you identify where your agent breaks and
  explain why? A thoughtful analysis of failures is more valuable than a
  perfect score."
EXPECTED INPUT: OrchestratorResult (so we have everything: response,
  authorized tool log, tier, etc.)
EXPECTED OUTPUT: ConsistencyReview { drifts[], judge_* fields }
UPSTREAM: orchestrator (final post-processing + repair loop)
DOWNSTREAM: schema (D9), llm.judge_chat (D13 secondary detection)
COMPONENT TESTS: tests/whitebox/test_consistency_reviewer.py +
  tests/failure_modes/* (synthetic drift cases per kind)
SCENARIO COVERAGE: every scenario passes through this gate.

Per-drift metadata (see DRIFT_CATEGORY / DRIFT_MAX_REPAIRS below):
  - "content" drifts are repair-eligible — the agent can fix them on a
    re-enter with drift-specific feedback. Budget is per kind (most are
    1; reasoning/cost reconciliation are 2 since they involve deeper
    alignment).
  - "system" drifts indicate a dispatcher bug or tampering (Red tier
    authorized a non-escalate tool; output tier ≠ input tier). The
    orchestrator MUST NOT re-prompt the agent for these — it produces
    `pipeline_status == "system_error"` and emits an operator alert.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DriftKind = Literal[
    "cost_path_mismatch",         # cost_assessment.chosen_path != decision
    "tool_not_authorized",        # response.tool_calls has a name not in dispatcher's authorized log
    "missing_alternative_path",   # decision in {deny, escalate} but no escalation block AND no alternative text
    "red_tool_violation",         # tier=Red but tool_calls includes something other than escalate_to_human
    "tier_mismatch",              # output context tier != input tier
    "missing_citations",          # decision in {deny, escalate} but no citations
    "missing_cost_assessment",    # tier=Grey but cost_assessment is None
    "reasoning_decision_drift",   # LLM judge disagreement
    "cove_factuality_drift",      # Chain-of-Verification found unsupported claim(s) in response
]

DriftCategory = Literal["content", "system"]

# Content drifts can be repaired by re-prompting the agent.
# System drifts indicate a dispatcher bug / tampering — no re-prompt;
# the orchestrator surfaces `pipeline_status = "system_error"`.
DRIFT_CATEGORY: dict[DriftKind, DriftCategory] = {
    "cost_path_mismatch": "content",
    "tool_not_authorized": "content",
    "missing_alternative_path": "content",
    "missing_citations": "content",
    "missing_cost_assessment": "content",
    "reasoning_decision_drift": "content",
    "cove_factuality_drift": "content",
    "red_tool_violation": "system",
    "tier_mismatch": "system",
}

# Per-drift-kind repair budget. Most wording/structural fixes are 1
# attempt (if the agent can't fix a simple wording issue in one retry,
# that's signal of a deeper problem). Reasoning/cost reconciliation get
# 2 because they involve deeper alignment. CoVe is expensive (~4 LLM
# calls each pass) so 1 repair attempt only — if first repair doesn't
# fix the factuality issue, surface as unresolved_drift rather than
# burning more tokens.
DRIFT_MAX_REPAIRS: dict[DriftKind, int] = {
    "missing_alternative_path": 1,
    "missing_citations": 1,
    "missing_cost_assessment": 1,
    "tool_not_authorized": 1,
    "cost_path_mismatch": 2,
    "reasoning_decision_drift": 2,
    "cove_factuality_drift": 1,
    "red_tool_violation": 0,   # system drift; never repaired
    "tier_mismatch": 0,
}


@dataclass
class Drift:
    kind: DriftKind
    detail: str

    @property
    def category(self) -> DriftCategory:
        return DRIFT_CATEGORY[self.kind]

    @property
    def max_repairs(self) -> int:
        return DRIFT_MAX_REPAIRS[self.kind]

    @property
    def is_system(self) -> bool:
        return self.category == "system"

    @property
    def is_repairable(self) -> bool:
        return self.category == "content"


@dataclass
class ConsistencyReview:
    drifts: list[Drift] = field(default_factory=list)
    judge_invoked: bool = False
    judge_passed: bool | None = None
    judge_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.drifts

    @property
    def has_system_drift(self) -> bool:
        return any(d.is_system for d in self.drifts)

    @property
    def content_drifts(self) -> list[Drift]:
        return [d for d in self.drifts if d.is_repairable]

    @property
    def system_drifts(self) -> list[Drift]:
        return [d for d in self.drifts if d.is_system]


# ---------------------------------------------------------------------------
# Deterministic structural checks (primary)
# ---------------------------------------------------------------------------


def review_structural(orch_result: Any) -> ConsistencyReview:
    """Run every D13 structural assertion. Returns a ConsistencyReview.

    `orch_result` is duck-typed (OrchestratorResult) to avoid a circular
    import.
    """
    drifts: list[Drift] = []
    response = orch_result.response
    tier = orch_result.tier

    # 1. Tier match (input tier must equal the tier declared on the
    #    OrchestratorResult; this is structural and cheap).
    # The architecture doesn't let the model overwrite tier so this is a
    # belt-and-braces check.
    if orch_result.tier not in ("Red", "Blue", "Grey"):
        drifts.append(Drift(kind="tier_mismatch", detail=f"unknown tier {tier!r}"))

    # 2. Red can only have escalate_to_human in its authorized list.
    #    (Note: the agent's response.tool_calls list is what the agent
    #    proposed; the dispatcher already gates them. This check is
    #    looking at AUTHORIZED tools, the actual side-effects.)
    if tier == "Red":
        for name in orch_result.authorized_tool_names:
            if name != "escalate_to_human":
                drifts.append(
                    Drift(
                        kind="red_tool_violation",
                        detail=(
                            f"Red request authorized non-escalate tool {name!r} "
                            "— this should be impossible per D3 allowlist; "
                            "investigate dispatcher state"
                        ),
                    )
                )

    # 3. tool_calls in response must be a subset of authorized tool names
    #    (the dispatcher may have rejected one or more proposed calls).
    proposed = [tc.name for tc in response.tool_calls]
    authorized = set(orch_result.authorized_tool_names)
    for name in proposed:
        if name not in authorized:
            drifts.append(
                Drift(
                    kind="tool_not_authorized",
                    detail=(
                        f"response proposes tool {name!r} but dispatcher "
                        f"did not authorize it (authorized: {sorted(authorized)})"
                    ),
                )
            )

    # 4. cost_assessment is required for Grey, optional for Blue, absent for Red.
    if tier == "Grey" and response.cost_assessment is None:
        drifts.append(
            Drift(
                kind="missing_cost_assessment",
                detail="Grey responses must populate cost_assessment per D12",
            )
        )

    # 5. cost_assessment.chosen_path must match decision (if present).
    if response.cost_assessment is not None:
        if response.cost_assessment.chosen_path != response.decision:
            drifts.append(
                Drift(
                    kind="cost_path_mismatch",
                    detail=(
                        f"cost_assessment.chosen_path={response.cost_assessment.chosen_path!r} "
                        f"!= decision={response.decision!r}"
                    ),
                )
            )

    # 6. deny / escalate must offer alternative path or carry escalation block.
    if response.decision in ("deny", "escalate"):
        has_escalation = response.escalation is not None
        has_alternative_text = bool(_alternative_path_signals_in(response.action))
        if not (has_escalation or has_alternative_text):
            drifts.append(
                Drift(
                    kind="missing_alternative_path",
                    detail=(
                        "deny/escalate decision must include either an "
                        "escalation block or an alternative-path mention "
                        "(e.g. 'contact IT', 'self-service portal', "
                        "'escalate', 'human operator')"
                    ),
                )
            )

    # 7. Citations required for deny / escalate (per §6.1).
    if response.decision in ("deny", "escalate") and not response.citations:
        drifts.append(
            Drift(
                kind="missing_citations",
                detail="deny/escalate decisions must cite at least one policy section per §6.1",
            )
        )

    return ConsistencyReview(drifts=drifts)


import re as _re

# Two-layer heuristic for "does the action text point the user somewhere?":
#   1. Specific channel / role keywords (escalation, HR, IT, self-service,
#      ticket systems, managers, approved channels, file-sharing, ...).
#   2. Directive phrases that signal recourse irrespective of the
#      specific channel ("please <verb>", "you can <verb>", "ask <them>",
#      "consider <gerund>", "instead, ...", "alternatively, ..."). These
#      catch perfectly valid alternative-path text that wouldn't match a
#      hard-coded channel list — e.g. "please ask her to share them with
#      you" or "please contact the HR department directly".
_CHANNEL_KEYWORDS = (
    # IT-related channels
    "contact it", "it directly", "it team", "it support", "it security",
    # HR-related channels
    "contact hr", "contact the hr", "hr directly", "hr department",
    "reach out to hr", "reach hr",
    # Self-service / portal / tooling
    "self-service", "self service", "self-service portal",
    "via the self-service",
    # Escalation / human handoff
    "escalat",                # matches escalate / escalating / escalation
    "human operator", "human it", "human review", "human handling",
    # Manager-mediated recourse
    "your manager", "their manager", "manager approval", "manager can",
    "speak to your manager", "speak with your manager",
    # Ticket / request systems
    "submit a ticket", "open a ticket", "file a ticket", "submit a request",
    "file a request", "helpdesk",
    # Generic "request through / via"
    "request through", "request via", "via the helpdesk",
    # Approved-channel recourse (for cases like §3.4 personal drive)
    "approved file-sharing", "approved file sharing", "approved channels",
    "approved methods", "approved procedure", "approved process",
    "approved tools",
    # Direct alternative-party phrasing
    "share them directly", "share them with you", "share with you directly",
    "share through", "directly through",
)

# Directive phrases that indicate the agent is pointing the user somewhere.
# Compiled regex with word boundaries to avoid over-matching.
_DIRECTIVE_REGEXES = (
    _re.compile(r"\bplease\s+(?:contact|ask|reach|submit|file|speak|talk|email|call|consult|consider|use|try|visit|head|see|share)\b", _re.IGNORECASE),
    _re.compile(r"\byou\s+(?:can|could|may|should|might|will)\s+(?:contact|ask|reach|submit|file|speak|talk|email|call|consult|consider|use|try|visit|head|see|share|request|escalate)\b", _re.IGNORECASE),
    _re.compile(r"\bask\s+(?:her|him|them|the|your|their)\b", _re.IGNORECASE),
    _re.compile(r"\b(?:consider|consider also)\s+\w+ing\b", _re.IGNORECASE),
    _re.compile(r"\binstead\s*,", _re.IGNORECASE),
    _re.compile(r"\balternatively\s*,", _re.IGNORECASE),
    _re.compile(r"\bif\s+you\s+(?:need|want|would like)\b.*\b(?:contact|ask|reach|submit|file|consult)\b", _re.IGNORECASE),
)


def _alternative_path_signals_in(text: str) -> list[str]:
    """Heuristic: does the action text mention an alternative path?

    Returns a list of matched signals. Empty list ⇒ the
    `missing_alternative_path` drift fires.

    Two layers (see comments above):
      1. Specific channel / role keywords.
      2. Directive phrases (please/you can/ask her/...).
    """
    lower = text.lower()
    hits: list[str] = [n for n in _CHANNEL_KEYWORDS if n in lower]
    for rx in _DIRECTIVE_REGEXES:
        m = rx.search(text)
        if m:
            hits.append(f"directive:{m.group(0)!r}")
    return hits


# ---------------------------------------------------------------------------
# Optional LLM-judge (secondary, behind a flag)
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = """You are a policy-decision auditor. You receive an agent's reasoning, decision, and action text. Decide whether the reasoning supports the decision.

Output STRICT JSON only:
{"supports": true|false, "reason": "<one sentence>"}

A 'no' is appropriate when the reasoning concludes one outcome but the decision is the opposite (e.g. reasoning argues for denial but decision is allow). Procedural detail or caveats are not 'no's.
"""


def review_with_llm_judge(
    orch_result: Any,
    *,
    structural: ConsistencyReview | None = None,
) -> ConsistencyReview:
    """Run the optional LLM-judge step. Only invoked if structural review
    came back clean — no point asking the judge whether the reasoning
    supports a decision that's already structurally broken."""
    base = structural or review_structural(orch_result)
    if base.drifts:
        return base  # structural drift dominates; don't burn a judge call
    from policy_agent.llm import judge_chat

    response = orch_result.response
    msg = (
        f"Reasoning: {response.reasoning}\n"
        f"Decision: {response.decision}\n"
        f"Action: {response.action}\n"
    )
    try:
        res = judge_chat(
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": msg},
            ],
            temperature=0.0,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        parsed = res.parse_json()
        supports = isinstance(parsed, dict) and parsed.get("supports") is True
        reason = parsed.get("reason") if isinstance(parsed, dict) else None
        if supports:
            return ConsistencyReview(
                drifts=[],
                judge_invoked=True,
                judge_passed=True,
                judge_reason=reason,
            )
        # Drift detected: surface as a content drift the repair loop can address.
        return ConsistencyReview(
            drifts=[
                Drift(
                    kind="reasoning_decision_drift",
                    detail=f"LLM judge: {reason or 'reasoning does not support decision'}",
                )
            ],
            judge_invoked=True,
            judge_passed=False,
            judge_reason=reason,
        )
    except Exception as exc:
        # Judge failure is non-fatal — structural already clean.
        return ConsistencyReview(
            drifts=[],
            judge_invoked=True,
            judge_passed=None,
            judge_reason=f"LLM judge unavailable: {exc}",
        )


# ---------------------------------------------------------------------------
# Repair feedback construction (per-drift-kind)
# ---------------------------------------------------------------------------


def repair_feedback(drift: Drift) -> str:
    """Build the drift-specific feedback string the orchestrator appends
    to the user message on the agent's next attempt. Each branch
    describes the specific issue and what the agent should change.

    Caller is responsible for combining multiple drift feedbacks if more
    than one fired on the same response."""
    kind = drift.kind
    if kind == "missing_alternative_path":
        return (
            "ISSUE: Your previous response decided to deny or escalate but the "
            "`action` text did not mention an alternative path the user can "
            "take. Per requirement R3, a denial or escalation MUST explicitly "
            "tell the user what they can do instead — e.g. 'contact IT directly', "
            "'use the self-service portal', 'submit a ticket', 'escalate this to "
            "a human IT operator', or another concrete next step. "
            "FIX: Rewrite the `action` field to include such a sentence. Keep "
            "`decision`, `citations`, `reasoning`, `tool_calls`, and "
            "`cost_assessment` the same — only enrich `action`."
        )
    if kind == "missing_citations":
        return (
            "ISSUE: Your previous response decided to deny or escalate but "
            "emitted no citations. Per policy §6.1, the agent must cite the "
            "relevant policy section when denying a request. "
            "FIX: Re-emit with at least one citation in the `citations` array "
            "{section_id + verbatim quote substring from the retrieved chunks}."
        )
    if kind == "missing_cost_assessment":
        return (
            "ISSUE: Your previous response was for a Grey-tier request but the "
            "`cost_assessment` field was missing or null. Per D12, Grey "
            "responses REQUIRE a populated cost_assessment with every axis: "
            "harm_if_acted_wrongly, harm_if_acted_wrongly_reason, "
            "harm_if_refused_wrongly, harm_if_refused_wrongly_reason, "
            "reversibility, affects, chosen_path, justification. "
            "FIX: Re-emit with cost_assessment fully populated. chosen_path "
            "must match the top-level `decision` field."
        )
    if kind == "cost_path_mismatch":
        return (
            "ISSUE: Your previous response has an internal contradiction — "
            f"{drift.detail}. "
            "FIX: Reconcile them. Either change `decision` to match "
            "`cost_assessment.chosen_path`, or change `cost_assessment."
            "chosen_path` to match the decision. They MUST agree."
        )
    if kind == "reasoning_decision_drift":
        return (
            "ISSUE: The reasoning auditor flagged a contradiction between your "
            f"`reasoning` and your `decision` — {drift.detail}. "
            "FIX: Either revise the `reasoning` to genuinely support the "
            "stated `decision`, OR change the `decision` to match what the "
            "reasoning actually argues. The two MUST be consistent."
        )
    if kind == "tool_not_authorized":
        return (
            "ISSUE: Your previous response proposed a tool call that the "
            f"dispatcher rejected — {drift.detail}. "
            "FIX: Revise. Either drop the proposed tool call (remove it from "
            "`tool_calls`), or pick a different action that is authorized "
            "for this tier and request. Update `action` and `reasoning` to "
            "reflect the change."
        )
    if kind == "cove_factuality_drift":
        return (
            "ISSUE: A Chain-of-Verification pass independently checked your "
            "response's factual claims against the retrieved policy chunks "
            "and found unsupported claims:\n"
            f"{drift.detail}\n"
            "FIX: Re-emit the response with claims that ARE supported by the "
            "retrieved chunks. Either (a) change `decision` to align with "
            "what the chunks actually say, (b) tighten `action` and "
            "`reasoning` to only assert what the chunks support, or (c) "
            "change citations to ones whose text genuinely supports the "
            "decision. Do NOT make claims about policy that the retrieved "
            "chunks do not directly support."
        )
    # System drifts shouldn't reach here (orchestrator checks is_system first),
    # but be defensive.
    return f"Drift detected: {drift.detail}"


def combine_feedback(drifts: list[Drift]) -> str:
    """Combine repair feedback for multiple co-occurring content drifts
    into a single message the agent sees on its next attempt."""
    parts = [repair_feedback(d) for d in drifts]
    if len(parts) == 1:
        return parts[0]
    numbered = "\n\n".join(f"({i + 1}) {p}" for i, p in enumerate(parts))
    return (
        "Your previous response had multiple issues. Address ALL of them in "
        "your next response:\n\n" + numbered
    )
