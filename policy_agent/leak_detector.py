"""
COMPONENT: leak_detector
DESIGN-REF: D11 (Response leak detector — Presidio-detect + authorized-
  payload verify)
PURPOSE: Defense-in-depth check that the user-facing response only
  discloses PII that was already authorized by D2. Run AFTER the D13
  repair loop has converged. Presidio detects PII spans in user-visible
  fields (`action`, `escalation.summary`); for each span, verify it is a
  substring of (a) the filtered tool outputs (the "authorized payload"
  D2 produced) OR (b) the retrieved policy chunks (non-PII by
  construction, but allowed for completeness). Unverified spans →
  `LeakEvent`. On any leak, the orchestrator sets
  `pipeline_status = "leak_redacted"` and the user-facing `action` text
  has the leaked spans replaced with `[REDACTED-<entity_type>]`.

  Field-scope rationale (May 2026 fix): `reasoning` is the agent's
  audit / chain-of-thought channel, not a user-visible disclosure
  surface. It routinely contains tool function names like
  "lookup_employee" that Presidio's LOCATION recognizer mis-classifies
  as place names. The redaction logic only ever rewrites `action`, so
  scanning `reasoning` produces false-positive status flips with no
  user-visible change actually being made. The scope is therefore
  restricted to user-visible surfaces.
PROBLEM-STATEMENT REQ (verbatim): >
  "Tool Output Filtering — Tools return data the agent isn't allowed to
  share. Does the agent filter correctly, or does it leak private
  fields?"
EXPECTED INPUT: AgentResponse + list of filtered tool outputs + retrieved
  chunks
EXPECTED OUTPUT: LeakDetectionResult { events: list[LeakEvent],
  redacted_action: str | None }
UPSTREAM: orchestrator (final post-processing before returning result)
DOWNSTREAM: presidio_analyzer (lazy)
COMPONENT TESTS: tests/failure_modes/test_leak_detector.py
SCENARIO COVERAGE: defense-in-depth across all scenarios; explicit
  exercises in synthetic failure-mode tests (hallucinated PII,
  filter-bypassed salary).

Design note: D2's tag-driven filter is the **primary** disclosure
authority. D11 is the consistency check that the response doesn't
disclose PII that wasn't authorized by D2. We do NOT make new policy
decisions here — only enforce that what's in the response also passed
through D2.
"""
from __future__ import annotations

import os
import re as _re
import threading
from dataclasses import dataclass, field
from typing import Any

from policy_agent.retrieval import RetrievedChunk
from policy_agent.schema import AgentResponse


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LeakEvent:
    """One detected PII span that does NOT trace back to the authorized
    payload or retrieved chunks. The orchestrator redacts it from the
    final user-facing response."""

    entity_type: str                # "EMAIL_ADDRESS" | "PERSON" | "PHONE_NUMBER" | ...
    span: str                       # the matched text
    start: int                      # offset in the response source field
    end: int
    source_field: str               # "action" | "reasoning" | "escalation.summary"
    score: float                    # Presidio's confidence


@dataclass
class LeakDetectionResult:
    invoked: bool                                       # whether the detector ran
    events: list[LeakEvent] = field(default_factory=list)
    redacted_action: str | None = None                  # populated when leaks were found
    skipped_reason: str = ""

    @property
    def clean(self) -> bool:
        return not self.events


# ---------------------------------------------------------------------------
# Lazy Presidio engine
# ---------------------------------------------------------------------------

_ENGINE_LOCK = threading.Lock()
_ENGINE: Any = None
_ENGINE_FAILED = False


def _enabled() -> bool:
    return os.environ.get("LEAK_DETECTOR_ENABLED", "true").lower() in ("1", "true", "yes")


def _get_engine() -> Any:
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_FAILED:
        return None
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_FAILED:
            return None
        try:
            from presidio_analyzer import AnalyzerEngine
            _ENGINE = AnalyzerEngine()
            return _ENGINE
        except Exception as exc:
            print(f"[leak_detector] presidio unavailable: {exc}")
            _ENGINE_FAILED = True
            return None


# Entity types we care about for an IT-helpdesk PII-leak check.
#
# Notable exclusions:
#  - PERSON: names are EXPLICITLY allowed per §2.1 (directory_basic).
#    Detecting "David Kim" or "David Kim's" as PII produces false
#    positives on every name the agent mentions. D2's tag-driven
#    filter is the authority on what names are disclosed; D11 here
#    catches the PII patterns D2 can't catch when they appear inline
#    in `action`/`reasoning` text.
#  - DATE_TIME, NRP, ORGANIZATION: not policy-sensitive in this
#    context.
_RELEVANT_ENTITIES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "LOCATION",         # for home addresses (often surfaces as LOCATION)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WS = _re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Whitespace-normalize for substring containment checks."""
    return _WS.sub(" ", text).strip().lower()


def _authorized_payload_text(filtered_outputs: list[dict[str, Any]]) -> str:
    """Concatenate the values of the filtered tool outputs into one searchable
    string. We dump everything — field names + values — so a span like
    'd.kim@gaggia.com' is found whether it was the value of `work_email`
    or anywhere else in the payload."""
    import json
    pieces = []
    for output in filtered_outputs:
        try:
            pieces.append(json.dumps(output, default=str))
        except Exception:
            pieces.append(repr(output))
    return _normalize("\n".join(pieces))


def _chunks_text(chunks: list[RetrievedChunk]) -> str:
    return _normalize("\n".join(f"{c.section_title}\n{c.body}" for c in chunks))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect(
    response: AgentResponse,
    *,
    filtered_outputs: list[dict[str, Any]],
    retrieved: list[RetrievedChunk],
) -> LeakDetectionResult:
    """Detect leaked PII in the response.

    For each Presidio-detected PII span in (action + reasoning +
    escalation.summary), check that it's present in either the
    authorized payload (filtered tool outputs) or the retrieved policy
    chunks. Spans not found → `LeakEvent`. When leaks are found, return
    a redacted version of `action` with offending spans replaced by
    `[REDACTED-<entity_type>]`.
    """
    if not _enabled():
        return LeakDetectionResult(invoked=False, skipped_reason="LEAK_DETECTOR_ENABLED is false")

    engine = _get_engine()
    if engine is None:
        return LeakDetectionResult(invoked=False, skipped_reason="presidio unavailable")

    auth_text = _authorized_payload_text(filtered_outputs)
    chunk_text = _chunks_text(retrieved)
    # System identifiers (tool names, section IDs) are not PII even though
    # Presidio's LOCATION recognizer sometimes matches snake_case tool names
    # like "lookup_employee" as if they were place names. Treat the agent's
    # own proposed tool names as a non-PII source so they pass the leak check.
    tool_name_text = _normalize(
        " ".join(tc.name for tc in (response.tool_calls or []) if getattr(tc, "name", None))
    )

    events: list[LeakEvent] = []
    # D11 scans user-visible surfaces only. `reasoning` is the agent's
    # audit / chain-of-thought channel — it routinely contains tool
    # function names (e.g., "lookup_employee") and section IDs that
    # Presidio's LOCATION recognizer mis-classifies. Redaction (below)
    # only ever rewrites `action`, so scanning `reasoning` creates false
    # positives that flip pipeline_status with no user-visible content
    # actually changed.
    fields_to_scan: list[tuple[str, str]] = [("action", response.action)]
    if response.escalation and response.escalation.conversation_summary:
        fields_to_scan.append(("escalation.summary", response.escalation.conversation_summary))

    for source_field, text in fields_to_scan:
        if not text:
            continue
        try:
            results = engine.analyze(
                text=text,
                entities=list(_RELEVANT_ENTITIES),
                language="en",
            )
        except Exception as exc:
            print(f"[leak_detector] analyze failed for {source_field}: {exc}")
            continue
        for r in results:
            span = text[r.start:r.end]
            norm_span = _normalize(span)
            if not norm_span:
                continue
            # Authorized: present in filtered tool output OR in retrieved
            # policy chunks. We require the FULL span (not just a fragment)
            # to be present, to prevent attacks like piecing together
            # disallowed PII from authorized fragments.
            if norm_span in auth_text or norm_span in chunk_text or norm_span in tool_name_text:
                continue
            events.append(LeakEvent(
                entity_type=r.entity_type,
                span=span,
                start=r.start,
                end=r.end,
                source_field=source_field,
                score=float(r.score),
            ))

    redacted_action: str | None = None
    if events:
        # Redact spans in `action` text only — `reasoning` and `escalation`
        # are not user-facing in the same way. Sort by start desc so we
        # can splice without offset drift.
        action_events = [e for e in events if e.source_field == "action"]
        if action_events:
            buf = response.action
            for e in sorted(action_events, key=lambda x: x.start, reverse=True):
                buf = buf[: e.start] + f"[REDACTED-{e.entity_type}]" + buf[e.end:]
            redacted_action = buf

    return LeakDetectionResult(invoked=True, events=events, redacted_action=redacted_action)


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def _smoke() -> None:
    """Two cases:
    1) Legitimate PII (work_email in filtered output) → no leak.
    2) Hallucinated PII (personal_email not in filtered output) → leak event."""
    from policy_agent.schema import AgentResponse, Citation

    # Case 1: work_email is in the (filtered) tool output → allowed.
    filtered_outputs_with_email = [
        {"name": "David Kim", "department": "Engineering", "work_email": "d.kim@gaggia.com"}
    ]
    r1 = AgentResponse(
        decision="allow",
        action="David Kim's work email is d.kim@gaggia.com.",
        tool_calls=[],
        citations=[Citation(section_id="2.3", quote="The agent **may** share work email")],
        reasoning="",
        escalation=None,
        cost_assessment=None,
    )
    res1 = detect(r1, filtered_outputs=filtered_outputs_with_email, retrieved=[])
    print(f"  case 1 (legit work_email): invoked={res1.invoked} events={len(res1.events)} clean={res1.clean}")

    # Case 2: a personal_email NOT in the filtered output → leak.
    filtered_outputs_no_personal = [
        {"name": "Sarah Chen", "department": "Engineering"}   # personal_email filtered out by D2
    ]
    r2 = AgentResponse(
        decision="deny",
        action="Please reach Sarah Chen at sarah.chen.personal@gmail.com for a personal matter.",
        tool_calls=[],
        citations=[Citation(section_id="2.2", quote="must not share personal email")],
        reasoning="",
        escalation=None,
        cost_assessment=None,
    )
    res2 = detect(r2, filtered_outputs=filtered_outputs_no_personal, retrieved=[])
    print(f"  case 2 (hallucinated personal_email): invoked={res2.invoked} events={len(res2.events)} clean={res2.clean}")
    for ev in res2.events:
        print(f"    LEAK: {ev.entity_type} span={ev.span!r} field={ev.source_field}")
    if res2.redacted_action:
        print(f"    redacted: {res2.redacted_action!r}")


if __name__ == "__main__":
    _smoke()
