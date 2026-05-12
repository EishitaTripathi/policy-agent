"""
COMPONENT: dispatcher
DESIGN-REF: D3 (Auth-gated tool dispatcher / PEP)
PURPOSE: Single entry point for all tool calls. Receives a caller context
  (tier + identity, system-supplied) plus a proposed tool call, checks it
  against policies/tier-tool-allowlist.yaml (D3) including any argument-
  level constraints, and either invokes the tool or rejects with a logged
  reason. Maintains a per-request authorization log used by D13's
  structural check.
PROBLEM-STATEMENT REQ (verbatim): >
  "Trust Tier Enforcement — Does the agent behave differently based on the
  user's trust classification? Does it refuse tool calls for Team Red
  users? Does it apply appropriate caution for Team Grey?"
EXPECTED INPUT: (caller_context, tool_name, args)
EXPECTED OUTPUT: DispatchResult with status='authorized'|'rejected', plus
  per-request log retrievable via authorized_tools(request_id).
UPSTREAM: orchestrator (sends structured tool_calls from the agent here)
DOWNSTREAM: policy_config.load_allowlist (D3), tools.get_tool (registry)
COMPONENT TESTS: tests/whitebox/test_dispatcher.py
SCENARIO COVERAGE: #7 (Blue svc-deploy reset → §1.2 deny via filter not
  dispatcher), #15/#21 (Red attempting non-escalate tools → dispatcher
  rejects), #17/#18 (Red prompt-injection attempts → dispatcher rejects).

Design principle: the model NEVER calls tools directly. The agent emits
a structured response with proposed tool_calls; this dispatcher is the
sole code path that invokes a tool. (LLM proposes, dispatcher disposes.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from policy_agent.policy_config import (
    Tier,
    TierToolAllowlist,
    load_allowlist,
)
from policy_agent.tools import ToolSchema, get_tool


@dataclass(frozen=True)
class CallerContext:
    """The trusted (system-supplied) context attached to every request.

    Fields here are NEVER inferred from the user's message; they come
    from the upstream session/auth layer. The dispatcher trusts them
    absolutely and the model has no way to overwrite them.
    """

    request_id: str
    conversation_id: str
    tier: Tier
    employee_id: str | None  # the requesting employee, if any

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "tier": self.tier,
            "employee_id": self.employee_id,
        }


@dataclass
class DispatchResult:
    status: Literal["authorized", "rejected", "tool_unknown"]
    tool_name: str
    args: dict[str, Any]
    output: dict[str, Any] | None = None
    rejection_reason: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def is_authorized(self) -> bool:
        return self.status == "authorized"


class Dispatcher:
    """Stateful PEP. One instance per process; per-request authorization
    history is keyed on `caller.request_id` so D13 can audit it later."""

    def __init__(self, allowlist: TierToolAllowlist | None = None) -> None:
        self.allowlist: TierToolAllowlist = allowlist or load_allowlist()
        # request_id -> list[DispatchResult] (in arrival order)
        self._log: dict[str, list[DispatchResult]] = {}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(
        self,
        caller: CallerContext,
        tool_name: str,
        args: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """Attempt to invoke `tool_name(**args)` on behalf of `caller`.

        Returns a DispatchResult; never raises for policy reasons (the
        rejection is the structured outcome). Raises only on programmer
        error (e.g., a tool that errors mid-execution; we let those
        propagate so they're loud in tests).
        """
        args = dict(args or {})

        # Stage 0 — does the tool even exist?
        try:
            tool: ToolSchema = get_tool(tool_name)
        except KeyError:
            result = DispatchResult(
                status="tool_unknown",
                tool_name=tool_name,
                args=args,
                rejection_reason=f"unknown tool: {tool_name!r}",
            )
            self._record(caller.request_id, result)
            return result

        # Stage 1 — (tier, tool_name, args) allowlist check.
        if not self.allowlist.is_allowed(caller.tier, tool_name, args):
            reason = self.allowlist.reason_for_denial(caller.tier, tool_name, args)
            result = DispatchResult(
                status="rejected",
                tool_name=tool_name,
                args=args,
                rejection_reason=reason,
            )
            self._record(caller.request_id, result)
            return result

        # Stage 2 — invoke the tool. Argument-validation errors from the
        # tool callable are programmer errors (the agent built a bad call);
        # we surface them as rejections to keep the caller path clean.
        try:
            output = tool.callable(**args)
        except TypeError as exc:
            result = DispatchResult(
                status="rejected",
                tool_name=tool_name,
                args=args,
                rejection_reason=f"tool argument error: {exc}",
            )
            self._record(caller.request_id, result)
            return result

        result = DispatchResult(
            status="authorized",
            tool_name=tool_name,
            args=args,
            output=output,
        )
        self._record(caller.request_id, result)
        return result

    # ------------------------------------------------------------------
    # Per-request audit log (used by D13)
    # ------------------------------------------------------------------

    def _record(self, request_id: str, result: DispatchResult) -> None:
        self._log.setdefault(request_id, []).append(result)

    def history(self, request_id: str) -> list[DispatchResult]:
        return list(self._log.get(request_id, ()))

    def authorized_tools(self, request_id: str) -> list[str]:
        """Tool names that were authorized for this request. D13's
        structural check asserts: response.tool_calls ⊆ this list."""
        return [r.tool_name for r in self._log.get(request_id, ()) if r.is_authorized()]

    def rejected_attempts(self, request_id: str) -> list[DispatchResult]:
        return [r for r in self._log.get(request_id, ()) if not r.is_authorized()]


# ---------------------------------------------------------------------------
# Self-check / smoke when run as a script
# ---------------------------------------------------------------------------


def _smoke() -> None:
    """Demonstrate the rejection cases that protect tests #17, #18, #21."""
    d = Dispatcher()
    cases = [
        # tier         tool                  args                                          expected
        ("Red",  "reset_password",     {"employee_id": "EMP-0001"},                "rejected"),
        ("Red",  "lookup_employee",    {"query": "Sarah Chen"},                    "rejected"),
        ("Red",  "escalate_to_human",  {"reason": "x", "conversation_summary": "y"}, "authorized"),
        ("Red",  "query_hr_database",  {"query_type": "individual", "employee_id": "EMP-1042"}, "rejected"),
        ("Red",  "query_hr_database",  {"query_type": "policy", "employee_id": "pto"}, "authorized"),
        ("Blue", "reset_password",     {"employee_id": "EMP-2011"},                "authorized"),
        ("Grey", "grant_file_access",  {"employee_id": "EMP-1500", "drive_id": "DRV-design", "access_level": "read"}, "authorized"),
    ]
    for i, (tier, tool, args, want) in enumerate(cases):
        ctx = CallerContext(
            request_id=f"smoke-{i}",
            conversation_id=f"smoke-{i}",
            tier=tier,
            employee_id="EMP-9999",
        )
        res = d.dispatch(ctx, tool, args)
        ok = "OK" if res.status == want else "FAIL"
        extra = f" reason={res.rejection_reason!r}" if res.rejection_reason else ""
        print(f"  [{ok}] {tier:5} {tool:22} -> {res.status}{extra}")


if __name__ == "__main__":
    _smoke()
