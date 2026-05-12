"""
COMPONENT: citation_verifier
DESIGN-REF: D5 (Citation verification: deterministic-first, LLM-judge-second)
PURPOSE: Verify that every citation the agent emitted is grounded in the
  retrieved chunks. Two-stage:
    1. Deterministic span check (load-bearing, ms): section_id exists in
       the retrieved chunks AND quote is a verbatim substring of one
       (whitespace-normalized).
    2. Optional LLM-judge semantic check (advisory, behind --llm-judge):
       routed to JUDGE_MODEL (Groq 70B by default) to confirm the cited
       text actually justifies the action/denial.
PROBLEM-STATEMENT REQ (verbatim): >
  "Every action or denial must cite the relevant policy section. ...
   How you make adherence verifiable is a design choice."
EXPECTED INPUT: AgentResponse + list[RetrievedChunk]
EXPECTED OUTPUT: VerificationResult { ok, citation_status[], reasons[] }
UPSTREAM: orchestrator
DOWNSTREAM: schema (D9), retrieval (D4), llm.judge_chat (D5 secondary)
COMPONENT TESTS: tests/whitebox/test_citation_verifier.py
SCENARIO COVERAGE: all scenarios where the agent emits citations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from policy_agent.retrieval import RetrievedChunk
from policy_agent.schema import AgentResponse, Citation

CitationStatus = Literal["ok", "missing_section", "quote_not_in_chunk", "advisory_failed"]


@dataclass
class CitationVerdict:
    citation: Citation
    status: CitationStatus
    matched_section_id: str | None = None
    note: str | None = None


@dataclass
class VerificationResult:
    ok: bool
    verdicts: list[CitationVerdict] = field(default_factory=list)

    @property
    def failures(self) -> list[CitationVerdict]:
        return [v for v in self.verdicts if v.status != "ok"]


# ---------------------------------------------------------------------------
# Deterministic substring check (primary)
# ---------------------------------------------------------------------------


_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Whitespace-normalize for substring comparison. Keeps the rest of
    the text verbatim (case, punctuation, Markdown markers) so the
    agent's quote must genuinely match the policy text."""
    return _WS.sub(" ", text).strip()


def _section_id_index(chunks: list[RetrievedChunk]) -> dict[str, RetrievedChunk]:
    """Map section_id -> chunk for the retrieved set. Section_ids must
    exactly match (e.g., '4.2' in the citation must match a chunk with
    section_id '4.2')."""
    return {c.section_id: c for c in chunks}


def verify_deterministic(
    response: AgentResponse,
    chunks: list[RetrievedChunk],
) -> VerificationResult:
    """Stage 1 — substring + section_id check.

    For each citation:
      - section_id must equal a retrieved chunk's section_id.
      - quote (whitespace-normalized) must be a substring of that chunk's
        body (also whitespace-normalized).

    Returns ok=True iff every citation passes; otherwise ok=False with
    per-citation verdicts.
    """
    idx = _section_id_index(chunks)
    verdicts: list[CitationVerdict] = []
    for c in response.citations:
        chunk = idx.get(c.section_id)
        if chunk is None:
            verdicts.append(
                CitationVerdict(
                    citation=c,
                    status="missing_section",
                    note=(
                        f"section_id {c.section_id!r} not in retrieved chunks "
                        f"(retrieved: {sorted(idx.keys())})"
                    ),
                )
            )
            continue
        if _normalize(c.quote) not in _normalize(chunk.body):
            verdicts.append(
                CitationVerdict(
                    citation=c,
                    status="quote_not_in_chunk",
                    matched_section_id=c.section_id,
                    note=(
                        f"quote not found verbatim in §{c.section_id} "
                        f"(quote[:60]={c.quote[:60]!r})"
                    ),
                )
            )
            continue
        verdicts.append(
            CitationVerdict(
                citation=c,
                status="ok",
                matched_section_id=c.section_id,
            )
        )
    return VerificationResult(ok=all(v.status == "ok" for v in verdicts), verdicts=verdicts)


# ---------------------------------------------------------------------------
# Optional LLM-judge semantic check (secondary, behind a flag)
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = """You are a policy-citation auditor. You receive (a) an agent's decision and action text, and (b) a citation it made (section_id + verbatim quote). Decide whether the cited text is genuinely on-point support for the agent's decision.

Output STRICT JSON only, no prose, no code fences:
{"appropriate": true|false, "reason": "<one sentence>"}

Be conservative: a citation is appropriate if the quoted text directly relates to the decision (allow/deny/escalate) on the topic at hand. A citation is INAPPROPRIATE only when the quote is off-topic, or when the agent denied based on a may-clause, or allowed based on a must-not clause.
"""


def verify_with_llm_judge(
    response: AgentResponse,
    chunks: list[RetrievedChunk],
    *,
    deterministic: VerificationResult | None = None,
) -> VerificationResult:
    """Stage 2 — LLM-judge semantic check on top of stage 1.

    For each citation that passed the deterministic check, ask the
    judge whether the cited quote is on-point support. Failures are
    advisory (status='advisory_failed') — they do not invalidate a
    deterministically-verified citation; they're surfaced for review.
    """
    from policy_agent.llm import judge_chat

    base = deterministic or verify_deterministic(response, chunks)
    out_verdicts: list[CitationVerdict] = []
    for v in base.verdicts:
        if v.status != "ok":
            out_verdicts.append(v)
            continue
        msg = (
            f"Decision: {response.decision}\n"
            f"Action: {response.action}\n\n"
            f"Citation: §{v.citation.section_id}\n"
            f"Quote: {v.citation.quote}\n"
        )
        try:
            res = judge_chat(
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": msg},
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            parsed = res.parse_json()
            if isinstance(parsed, dict) and parsed.get("appropriate") is True:
                out_verdicts.append(v)  # confirmed
            else:
                reason = (parsed.get("reason") if isinstance(parsed, dict) else None) or "judge: not appropriate"
                out_verdicts.append(
                    CitationVerdict(
                        citation=v.citation,
                        status="advisory_failed",
                        matched_section_id=v.matched_section_id,
                        note=f"LLM judge: {reason}",
                    )
                )
        except Exception as exc:  # parse / API failure
            out_verdicts.append(
                CitationVerdict(
                    citation=v.citation,
                    status="ok",  # don't downgrade on judge failure
                    matched_section_id=v.matched_section_id,
                    note=f"LLM judge unavailable: {exc}",
                )
            )
    deterministic_ok = all(
        v.status in ("ok",)
        for v in out_verdicts
    )
    return VerificationResult(ok=deterministic_ok, verdicts=out_verdicts)
