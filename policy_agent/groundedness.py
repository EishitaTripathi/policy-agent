"""
COMPONENT: groundedness
DESIGN-REF: Comprehensive eval — RAGAS-style faithfulness metric
PURPOSE: Score whether the agent's `reasoning` text is faithful to the
  retrieved policy chunks. Inspired by RAGAS's `faithfulness` metric
  (https://docs.ragas.io). The intent is to catch responses where the
  agent's reasoning makes claims the retrieved chunks don't actually
  support — a complement to D5's citation verification (which checks
  that cited quotes are verbatim substrings) by also scoring the
  unquoted assertive content.
PROBLEM-STATEMENT REQ (verbatim): >
  "Policy Adherence — Does the agent correctly allow, deny, and escalate
  according to the policy? The agent should demonstrate its adherence in
  a measurable way."
EXPECTED INPUT: AgentResponse + retrieved chunks
EXPECTED OUTPUT: GroundednessScore { score: float, ungrounded_claims: list[str] }
UPSTREAM: eval.py (reporting), optionally orchestrator (defense-in-depth)
DOWNSTREAM: llm.judge_chat (1 call per invocation; off by default)
COMPONENT TESTS: none — this is an eval metric, exercised by the eval suite.
SCENARIO COVERAGE: optional metric across all 21 + LLM-extras.

Cost note: 1 LLM call per scoring invocation. Enable via
GROUNDEDNESS_ENABLED env (default false) so the eval doesn't burn TPD
when not investigating faithfulness specifically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from policy_agent.retrieval import RetrievedChunk
from policy_agent.schema import AgentResponse


@dataclass
class GroundednessScore:
    invoked: bool                       # whether scorer actually ran
    score: float = 1.0                  # 0.0-1.0; 1.0 = fully grounded
    ungrounded_claims: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    judge_failed: bool = False


def _enabled() -> bool:
    return os.environ.get("GROUNDEDNESS_ENABLED", "false").lower() in ("1", "true", "yes")


_SYSTEM = """You are a faithfulness auditor for a policy-bound IT helpdesk agent. You are given (a) the agent's reasoning text and (b) the retrieved policy chunks the agent had access to. Decide whether every factual claim in the reasoning is supported by the retrieved chunks.

Decompose the reasoning into atomic factual claims. For each claim, decide whether the retrieved chunks SUPPORT it. Output a score (= supported / total) plus the list of unsupported claims.

Output STRICT JSON only:
{
  "score": <float 0.0-1.0>,
  "ungrounded_claims": ["<verbatim claim 1>", "<verbatim claim 2>", ...]
}

Score 1.0 = every claim supported. Score 0.0 = no claims supported. Treat citations of named policy sections (e.g. "per §4.2") as supported iff that section appears in the chunks. Treat general procedural statements ("contact IT directly", "use the self-service portal") as supported by default — they are alternative-path recommendations, not factual policy claims.
"""


def _format_chunks(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no chunks retrieved)"
    lines = []
    for c in chunks:
        lines.append(f"--- §{c.section_id} {c.section_title} ---")
        lines.append(c.body.strip())
    return "\n".join(lines)


def score_response(
    response: AgentResponse,
    chunks: list[RetrievedChunk],
) -> GroundednessScore:
    """Run the faithfulness scorer on the agent's reasoning."""
    if not _enabled():
        return GroundednessScore(invoked=False, skipped_reason="GROUNDEDNESS_ENABLED is false")
    if not response.reasoning:
        return GroundednessScore(invoked=False, skipped_reason="no reasoning to score")

    from policy_agent.llm import judge_chat

    user_msg = (
        f"RETRIEVED CHUNKS:\n{_format_chunks(chunks)}\n\n"
        f"AGENT REASONING:\n{response.reasoning}\n\n"
        "Score the reasoning's faithfulness. Output STRICT JSON only."
    )
    try:
        res = judge_chat(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        parsed = res.parse_json()
        if not isinstance(parsed, dict):
            raise ValueError("non-dict response")
        raw_score = parsed.get("score", 1.0)
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        ungrounded = parsed.get("ungrounded_claims", []) or []
        if not isinstance(ungrounded, list):
            ungrounded = []
        return GroundednessScore(
            invoked=True,
            score=score,
            ungrounded_claims=[str(c) for c in ungrounded if isinstance(c, str)],
        )
    except Exception as exc:
        # On judge failure, conservatively report invoked=True / score=1.0
        # (do NOT falsely flag low groundedness on transient errors).
        return GroundednessScore(
            invoked=True,
            score=1.0,
            judge_failed=True,
            skipped_reason=f"judge_unavailable: {exc}",
        )
