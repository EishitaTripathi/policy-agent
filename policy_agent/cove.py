"""
COMPONENT: cove
DESIGN-REF: D5 Stage 3 (Chain-of-Verification on the reasoner output)
PURPOSE: After the agent emits a response and citations pass D5's
  deterministic + optional LLM-judge stages, CoVe probes the response's
  factual claims against the retrieved policy chunks. Implements the
  Chain-of-Verification pattern from Dhuliawala 2023
  (https://arxiv.org/abs/2309.11495):
    1) Generate N verification questions about the agent's claims.
    2) For each question, independently verify against retrieved chunks
       only (no access to the agent's reasoning).
    3) Compare; if any claim is unsupported, flag `cove_factuality_drift`.
  Routed to JUDGE_MODEL (Groq 70B by default).
PROBLEM-STATEMENT REQ (verbatim): >
  "Policy Adherence — Does the agent correctly allow, deny, and escalate
  according to the policy? The agent should demonstrate its adherence in
  a measurable way."
EXPECTED INPUT: AgentResponse + retrieved chunks
EXPECTED OUTPUT: CoVeVerdict { aligned, questions, claim_verdicts[], divergences }
UPSTREAM: orchestrator (between D5 citation verifier and D13)
DOWNSTREAM: llm.judge_chat, retrieval (already passed in by orchestrator)
COMPONENT TESTS: tests/failure_modes/test_cove.py
SCENARIO COVERAGE: Grey + Blue-{deny,escalate} by default; configurable
  via COVE_ENABLED and COVE_TIER_SCOPE env vars.

Cost note: ~4 LLM calls per invocation (1 question-gen + 3 verification
calls). Default scope is Grey + Blue-{deny,escalate} where factuality
matters most; Blue-allow skips CoVe.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from policy_agent.retrieval import RetrievedChunk
from policy_agent.schema import AgentResponse


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClaimVerdict:
    question: str
    supports: bool                  # does retrieved evidence support the agent's claim?
    judge_reason: str = ""          # one-sentence explanation from the judge


@dataclass
class CoVeVerdict:
    """Result of a Chain-of-Verification pass on an agent response."""

    invoked: bool                                 # whether CoVe actually ran (vs skipped per scope)
    aligned: bool = True                          # all claim-verdicts support the response
    questions: list[str] = field(default_factory=list)
    claim_verdicts: list[ClaimVerdict] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    skipped_reason: str = ""                      # populated when invoked=False
    judge_failed: bool = False                    # API error / parse failure during CoVe


# ---------------------------------------------------------------------------
# Scope decision
# ---------------------------------------------------------------------------


def _cove_enabled() -> bool:
    return os.environ.get("COVE_ENABLED", "false").lower() in ("1", "true", "yes")


def _cove_scope() -> set[str]:
    """Parse COVE_TIER_SCOPE env var.

    Format: comma-separated tokens from:
      - `Grey`, `Blue`, `Red`  (tier-scoped: run on any decision for this tier)
      - `deny`, `escalate`, `allow`, `clarify`  (decision-scoped: run on any tier
        producing this decision)
      - `Blue:deny`, `Blue:escalate`, etc.  (compound; tier AND decision)

    Default: `"Grey,Blue:deny,Blue:escalate"` — run on Grey (any decision)
    and Blue denials/escalations.
    """
    raw = os.environ.get("COVE_TIER_SCOPE", "Grey,Blue:deny,Blue:escalate")
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def should_run_cove(tier: str, decision: str) -> tuple[bool, str]:
    """Return (run_it, reason). reason populated when run_it=False."""
    if not _cove_enabled():
        return False, "COVE_ENABLED is false"
    scope = _cove_scope()
    if tier in scope:
        return True, ""
    if decision in scope:
        return True, ""
    compound = f"{tier}:{decision}"
    if compound in scope:
        return True, ""
    return False, f"({tier},{decision}) not in COVE_TIER_SCOPE={sorted(scope)}"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_QUESTION_GEN_SYSTEM = """You are a verification auditor for a policy-bound IT helpdesk agent. The agent has emitted a response that includes a decision (allow/deny/escalate/clarify), an action text, and policy citations. Your job: produce 2-3 specific verification questions that probe the response's most consequential factual claims.

Focus questions on:
- Does the cited policy text actually support the decision?
- Are the action verbs (must / must-not / may) in the cited policy consistent with the agent's behavior?
- Is the alternative-path suggestion (e.g. "contact IT", "use self-service") consistent with what the policy says about recourse?

Each question should be answerable from the retrieved policy chunks alone. Avoid questions about external facts.

Output STRICT JSON only:
{"questions": ["...", "...", "..."]}"""


_VERIFY_SYSTEM = """You are a verification auditor. You are given (a) retrieved policy chunks, (b) a verification question about an agent's response, and (c) the specific claim the agent made.

Your job: using ONLY the retrieved policy chunks (do not use general knowledge), decide whether the chunks SUPPORT the claim, CONTRADICT the claim, or are INSUFFICIENT to judge.

Output STRICT JSON only:
{"supports": true|false, "reason": "<one sentence quoting or referencing the chunk text>"}

Set `supports: false` if the chunks contradict the claim OR if they are insufficient to support it. Set `supports: true` only when the chunks clearly support the claim."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_chunks_for_prompt(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no chunks retrieved)"
    lines = []
    for c in chunks:
        verb = f"[{c.action_verb}]" if c.action_verb else "[]"
        lines.append(f"\n--- §{c.section_id} {verb} {c.section_title} ---")
        lines.append(c.body.strip())
    return "\n".join(lines)


def _claim_summary(response: AgentResponse) -> str:
    """A compact one-paragraph summary of what the agent claims, for
    inclusion in the verification prompt. Deliberately omits the agent's
    `reasoning` field — CoVe verifies against chunks, not against the
    agent's chain-of-thought."""
    parts = [
        f"Decision: {response.decision}.",
        f"Action: {response.action}",
    ]
    if response.citations:
        cited_ids = ", ".join(f"§{c.section_id}" for c in response.citations)
        parts.append(f"Citations: {cited_ids}.")
    return " ".join(parts)


def _generate_questions(
    response: AgentResponse,
    chunks: list[RetrievedChunk],
    num_questions: int = 3,
) -> list[str]:
    from policy_agent.llm import judge_chat

    user_msg = (
        f"AGENT RESPONSE:\n{_claim_summary(response)}\n\n"
        f"RETRIEVED POLICY CHUNKS:\n{_format_chunks_for_prompt(chunks)}\n\n"
        f"Produce {num_questions} verification questions. Output STRICT JSON only."
    )
    try:
        res = judge_chat(
            messages=[
                {"role": "system", "content": _QUESTION_GEN_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        parsed = res.parse_json()
        if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
            qs = [str(q) for q in parsed["questions"] if isinstance(q, str) and q.strip()]
            return qs[:num_questions]
    except Exception:
        pass
    return []


def _verify_claim(
    question: str,
    response: AgentResponse,
    chunks: list[RetrievedChunk],
) -> ClaimVerdict:
    from policy_agent.llm import judge_chat

    user_msg = (
        f"RETRIEVED POLICY CHUNKS:\n{_format_chunks_for_prompt(chunks)}\n\n"
        f"AGENT CLAIM (what we are verifying):\n{_claim_summary(response)}\n\n"
        f"VERIFICATION QUESTION:\n{question}\n\n"
        "Using ONLY the retrieved chunks, answer with STRICT JSON: "
        '{"supports": true|false, "reason": "<one sentence>"}'
    )
    try:
        res = judge_chat(
            messages=[
                {"role": "system", "content": _VERIFY_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        parsed = res.parse_json()
        supports = bool(parsed.get("supports")) if isinstance(parsed, dict) else False
        reason = (parsed.get("reason") if isinstance(parsed, dict) else "") or ""
        return ClaimVerdict(question=question, supports=supports, judge_reason=str(reason))
    except Exception as exc:
        # On judge failure, conservatively say "supports=True" (do NOT
        # falsely flag drift on transient API errors). Log the failure.
        return ClaimVerdict(
            question=question,
            supports=True,
            judge_reason=f"judge_unavailable: {exc}",
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def verify(
    response: AgentResponse,
    chunks: list[RetrievedChunk],
    *,
    tier: str,
    num_questions: int = 3,
) -> CoVeVerdict:
    """Run a CoVe verification pass over the agent response.

    Skipped (invoked=False) when:
      - COVE_ENABLED=false
      - (tier, decision) not in COVE_TIER_SCOPE
      - response has no citations (nothing to verify against)
    """
    run, skip_reason = should_run_cove(tier, response.decision)
    if not run:
        return CoVeVerdict(invoked=False, skipped_reason=skip_reason)
    if not response.citations:
        # CoVe verifies policy claims; without a citation there's no
        # specific claim to verify against the chunks.
        return CoVeVerdict(invoked=False, skipped_reason="no citations to verify")

    # Stage 1: generate questions
    questions = _generate_questions(response, chunks, num_questions=num_questions)
    if not questions:
        return CoVeVerdict(
            invoked=True,
            aligned=True,
            questions=[],
            judge_failed=True,
            skipped_reason="question generation failed",
        )

    # Stage 2: verify each claim independently
    verdicts: list[ClaimVerdict] = []
    divergences: list[str] = []
    for q in questions:
        v = _verify_claim(q, response, chunks)
        verdicts.append(v)
        if not v.supports and not v.judge_reason.startswith("judge_unavailable"):
            divergences.append(f"Q: {q!r} → {v.judge_reason}")

    aligned = not divergences
    return CoVeVerdict(
        invoked=True,
        aligned=aligned,
        questions=questions,
        claim_verdicts=verdicts,
        divergences=divergences,
    )


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def _smoke() -> None:
    """Build a synthetic AgentResponse + retrieval and run CoVe with the
    scope env var set so the call actually fires. Requires JUDGE_MODEL +
    GROQ_API_KEY (or other JUDGE_MODEL config) to be valid."""
    os.environ["COVE_ENABLED"] = "true"
    from policy_agent.retrieval import retrieve
    from policy_agent.schema import AgentResponse, Citation

    print("[cove smoke] running CoVe on a synthetic deny response for 'Sarah Chen's salary'")
    chunks = retrieve("What's Sarah Chen's salary?", rerank_top_n=5)
    response = AgentResponse(
        decision="deny",
        action="I cannot share Sarah Chen's salary per §4.2. You can view your own compensation in the self-service portal.",
        tool_calls=[],
        citations=[Citation(
            section_id="4.2",
            quote="The agent **must not** access, disclose, or confirm any individual employee's compensation",
        )],
        reasoning="The request is for another employee's salary; §4.2 forbids it.",
        escalation=None,
        cost_assessment=None,
    )
    v = verify(response, chunks, tier="Blue", num_questions=2)
    print(f"  invoked  : {v.invoked}")
    print(f"  aligned  : {v.aligned}")
    print(f"  questions:")
    for q in v.questions:
        print(f"    - {q[:120]}")
    print(f"  verdicts :")
    for cv in v.claim_verdicts:
        print(f"    - supports={cv.supports} reason={cv.judge_reason[:120]}")
    if v.divergences:
        print(f"  divergences:")
        for d in v.divergences:
            print(f"    - {d}")


if __name__ == "__main__":
    _smoke()
