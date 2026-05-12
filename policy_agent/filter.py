"""
COMPONENT: filter
DESIGN-REF: D2 (Tag-driven output filter)
PURPOSE: After the dispatcher (D3) authorizes a tool call and the tool
  returns its full response, this filter strips fields the requester
  isn't authorized to see — based on each field's `tag` (declared in the
  tool registry) and the (tag, requester_relationship) ruleset in
  policies/filter-rules.yaml. Output of the filter is the "authorized
  payload" used by the leak detector (D11) to verify nothing else slipped
  into the final response.
PROBLEM-STATEMENT REQ (verbatim): >
  "Several tools return data that the agent is not allowed to share. The
  agent must filter tool outputs according to policy before presenting
  results to the user. Returning raw tool output is a failure."
EXPECTED INPUT: tool_name, raw_output dict, requester_relationship
EXPECTED OUTPUT: FilterResult { filtered_output, redacted_fields, applied_rules }
UPSTREAM: orchestrator (right after Dispatcher.dispatch returns)
DOWNSTREAM: policy_config.load_filter_rules (D2 ruleset),
  tools.get_tool (for the field-tag schema)
COMPONENT TESTS: tests/whitebox/test_filter.py
SCENARIO COVERAGE: #6 (salary stripped), #9 (performance review stripped),
  #10 (home_address stripped), #20 (personal_email stripped from a
  legitimate lookup), #15 (drive metadata allowed but escalation needed
  upstream).

Relationship determination is the orchestrator's job (deterministic from
caller.employee_id vs the subject in args + a manager-of table). The
filter simply consumes a relationship value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from policy_agent.policy_config import (
    FilterRules,
    Relationship,
    load_filter_rules,
)
from policy_agent.tools import ToolSchema, get_tool


@dataclass
class FilterResult:
    """Return type for filter_output()."""

    tool_name: str
    relationship: Relationship
    filtered_output: dict[str, Any]
    redacted_fields: list[str] = field(default_factory=list)
    applied_rules: list[dict[str, Any]] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not self.redacted_fields


# ---------------------------------------------------------------------------
# Manager-of relationship (mock; in production this comes from HRIS)
# ---------------------------------------------------------------------------

# For v1 we read direct reports from the mock employee record. v2 would
# walk the chain (a verified manager up any number of levels) per §4.4
# "in that employee's reporting chain".
def _direct_reports_of(manager_id: str) -> set[str]:
    from policy_agent.tools import _ACCOUNTS  # local import to avoid cycle

    record = _ACCOUNTS.get(manager_id)
    if not record:
        return set()
    return set(record.get("direct_reports", []))


def determine_relationship(
    requester_employee_id: str | None,
    subject_employee_id: str | None,
) -> Relationship:
    """Deterministic relationship classification.

    self              : requester is the subject
    manager_in_chain  : requester is a (one-hop, v1) manager of subject
    peer              : both are employees but no manager relationship
    other             : requester unknown or no employee identity
    """
    if requester_employee_id is None:
        return "other"
    if subject_employee_id is None:
        # Tool result has no subject (e.g., HR policy query) — relationship
        # doesn't gate disclosure; treat as "other" so non-personal tags
        # still pass per the YAML defaults.
        return "other"
    if requester_employee_id == subject_employee_id:
        return "self"
    if subject_employee_id in _direct_reports_of(requester_employee_id):
        return "manager_in_chain"
    return "peer"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _iter_field_decisions(
    tool: ToolSchema,
    relationship: Relationship,
    rules: FilterRules,
) -> Iterable[tuple[str, str | None, bool]]:
    """Yield (field_name, tag, allowed) for each declared field on this
    tool. Untagged fields are treated as operational and always allowed."""
    for field_name, tag in tool.response_field_tags.items():
        yield field_name, tag, rules.is_allowed(tag, relationship)
    for field_name in tool.untagged_fields:
        yield field_name, None, True


def filter_output(
    tool_name: str,
    raw_output: dict[str, Any],
    relationship: Relationship,
    rules: FilterRules | None = None,
) -> FilterResult:
    """Strip fields from `raw_output` that the requester isn't authorized
    to see, based on the tool's field-tag schema and the (tag, relationship)
    ruleset.

    - Status/error payloads (no PII) pass through unchanged.
    - Untagged fields are operational metadata and pass through.
    - Tagged fields with disallowed dispositions are dropped and recorded
      in `redacted_fields`.
    - Unknown fields (present in output but not declared in the registry)
      are dropped, with `applied_rules` recording an `unknown_field` flag —
      this catches drift where a tool gains a new field without updating
      the registry.
    """
    rules = rules or load_filter_rules()
    tool = get_tool(tool_name)

    filtered: dict[str, Any] = {}
    redacted: list[str] = []
    applied: list[dict[str, Any]] = []

    declared_fields: set[str] = set(tool.response_field_tags) | set(tool.untagged_fields)

    for key, value in raw_output.items():
        if key in tool.response_field_tags:
            tag = tool.response_field_tags[key]
            allowed = rules.is_allowed(tag, relationship)
            applied.append(
                {"field": key, "tag": tag, "relationship": relationship, "allowed": allowed}
            )
            if allowed:
                filtered[key] = value
            else:
                redacted.append(key)
        elif key in tool.untagged_fields:
            filtered[key] = value
            applied.append(
                {"field": key, "tag": None, "relationship": relationship, "allowed": True}
            )
        else:
            # Field is not declared on this tool — drop it and flag
            # so the registry can be updated.
            redacted.append(key)
            applied.append(
                {
                    "field": key,
                    "tag": None,
                    "relationship": relationship,
                    "allowed": False,
                    "unknown_field": True,
                }
            )

    # Sanity: every declared field that was authorized but missing from the
    # raw output is silently absent (some tool calls don't return all fields,
    # e.g., svc-deploy has no personal_email). That's not a redaction event.
    _ = declared_fields  # reserved for future cross-checks

    return FilterResult(
        tool_name=tool_name,
        relationship=relationship,
        filtered_output=filtered,
        redacted_fields=redacted,
        applied_rules=applied,
    )


# ---------------------------------------------------------------------------
# Smoke / self-check
# ---------------------------------------------------------------------------


def _smoke() -> None:
    """Demonstrate the filter on representative scenarios."""
    from policy_agent.tools import lookup_employee, query_hr_database

    cases = [
        # Scenario #6: Blue (peer) asks for Sarah Chen's salary.
        {
            "label": "scenario #6 (Blue peer asks for salary)",
            "tool": "lookup_employee",
            "raw": lookup_employee("EMP-1042"),
            "requester": "EMP-3300",
            "subject": "EMP-1042",
        },
        # Scenario #20: Blue peer asks for Sarah Chen's info; agent must
        # filter personal_email.
        {
            "label": "scenario #20 (Blue peer; personal_email must drop)",
            "tool": "lookup_employee",
            "raw": lookup_employee("EMP-1042"),
            "requester": "EMP-2200",
            "subject": "EMP-1042",
        },
        # Self-access: §15.3 carve-out — employee looking up themselves.
        {
            "label": "self-access (§15.3 carve-out — employee looks up self)",
            "tool": "lookup_employee",
            "raw": lookup_employee("EMP-1042"),
            "requester": "EMP-1042",
            "subject": "EMP-1042",
        },
        # Scenario #16: Manager-in-chain confirms employment status.
        {
            "label": "scenario #16 (David Kim asks about Jordan Rivera, his report)",
            "tool": "query_hr_database",
            "raw": query_hr_database("individual", "EMP-1100"),
            "requester": "EMP-1043",
            "subject": "EMP-1100",
        },
        # Scenario #9: Sales rep asks for someone's performance review.
        {
            "label": "scenario #9 (Sales asks for engineer's performance review)",
            "tool": "query_hr_database",
            "raw": query_hr_database("individual", "EMP-1042"),
            "requester": "EMP-5500",
            "subject": "EMP-1042",
        },
    ]
    for c in cases:
        rel = determine_relationship(c["requester"], c["subject"])
        res = filter_output(c["tool"], c["raw"], rel)
        kept = sorted(res.filtered_output.keys())
        print(f"\n  {c['label']}")
        print(f"    relationship = {rel}")
        print(f"    kept    = {kept}")
        print(f"    redacted = {sorted(res.redacted_fields)}")


if __name__ == "__main__":
    _smoke()
