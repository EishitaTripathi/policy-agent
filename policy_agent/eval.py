"""
COMPONENT: eval
DESIGN-REF: D7 (Automated eval runner with structured assertions)
PURPOSE: Runs every declared scenario through the orchestrator and
  asserts on response.decision, dispatcher-authorized tools, citations,
  and filter redactions. Produces a pass/fail report plus a per-scenario
  reasoning trace and the architectural-layer attribution (which gate
  decided the outcome).
PROBLEM-STATEMENT REQ (verbatim): >
  "Use an LLM to generate additional test scenarios beyond the 21
  provided. Include your generation approach and results. Analyze where
  your agent gets it right, where it fails, and why."
EXPECTED INPUT: tests/scenarios.yaml
EXPECTED OUTPUT: docs/eval-report.md plus a stdout summary; non-zero exit
  if any required assertion fails (per-scenario `expected` block).
UPSTREAM: invoked manually or via `python -m policy_agent.eval`
DOWNSTREAM: orchestrator (D), schema (D9), pyyaml
COMPONENT TESTS: itself acts as the integration suite for blackbox tests.
SCENARIO COVERAGE: all 21 + LLM-generated extras.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from policy_agent.orchestrator import OrchestratorResult, handle_request

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = REPO_ROOT / "tests" / "scenarios.yaml"
DEFAULT_REPORT_PATH = REPO_ROOT / "docs" / "eval-report.md"

SMOKE_IDS = {1, 6, 7, 11, 15, 17, 21}  # small representative subset


# ---------------------------------------------------------------------------
# Per-scenario assertion checks
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario_id: int
    tier: str
    message: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    orchestrator: OrchestratorResult | None = None
    error: str | None = None
    duration_s: float = 0.0


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _redacted_fields_across_executions(orch: OrchestratorResult) -> set[str]:
    out: set[str] = set()
    for ex in orch.tool_executions:
        if ex.filter is not None:
            out.update(ex.filter.redacted_fields)
    return out


def _exposed_fields_across_executions(orch: OrchestratorResult) -> set[str]:
    out: set[str] = set()
    for ex in orch.tool_executions:
        if ex.filter is not None:
            out.update(ex.filter.filtered_output.keys())
    return out


def evaluate_scenario(spec: dict[str, Any]) -> ScenarioResult:
    sid = int(spec["id"])
    tier = spec["tier"]
    message = spec["message"]
    requester_id = spec.get("requester_employee_id")
    expected = spec.get("expected", {})

    sr = ScenarioResult(scenario_id=sid, tier=tier, message=message, passed=True)
    t0 = time.time()
    try:
        orch = handle_request(
            user_message=message,
            tier=tier,
            employee_id=requester_id,
            request_id=f"scenario-{sid}",
        )
    except Exception as exc:
        sr.passed = False
        sr.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        sr.duration_s = time.time() - t0
        sr.checks.append(CheckResult("orchestrator_completed", False, str(exc)))
        return sr
    sr.orchestrator = orch
    sr.duration_s = time.time() - t0

    decision = orch.response.decision
    expected_actions = _as_list(expected.get("action_class"))
    if expected_actions:
        ok = decision in expected_actions
        sr.checks.append(
            CheckResult(
                name="action_class",
                passed=ok,
                detail=f"got={decision!r}, expected_one_of={expected_actions}",
            )
        )

    required_tools = _as_list(expected.get("tool_calls"))
    if required_tools:
        authorized = set(orch.authorized_tool_names)
        missing = [t for t in required_tools if t not in authorized]
        ok = not missing
        sr.checks.append(
            CheckResult(
                name="tool_calls_required",
                passed=ok,
                detail=(
                    f"authorized={sorted(authorized)}; "
                    f"required={required_tools}; "
                    f"missing={missing}"
                ),
            )
        )

    forbidden_tools = _as_list(expected.get("tool_calls_forbidden"))
    if forbidden_tools:
        authorized = set(orch.authorized_tool_names)
        leaked = [t for t in forbidden_tools if t in authorized]
        ok = not leaked
        sr.checks.append(
            CheckResult(
                name="tool_calls_forbidden",
                passed=ok,
                detail=f"forbidden={forbidden_tools}; authorized={sorted(authorized)}; leaked={leaked}",
            )
        )

    expected_sections = _as_list(expected.get("cited_sections"))
    if expected_sections:
        cited = {c.section_id for c in orch.response.citations}
        hits = [s for s in expected_sections if s in cited]
        ok = len(hits) > 0
        sr.checks.append(
            CheckResult(
                name="cited_sections_any",
                passed=ok,
                detail=f"expected_any_of={expected_sections}; cited={sorted(cited)}; hits={hits}",
            )
        )

    forbidden_fields = _as_list(expected.get("redacted_fields"))
    if forbidden_fields:
        exposed = _exposed_fields_across_executions(orch)
        leaked = [f for f in forbidden_fields if f in exposed]
        ok = not leaked
        sr.checks.append(
            CheckResult(
                name="filter_redaction",
                passed=ok,
                detail=f"forbidden={forbidden_fields}; exposed={sorted(exposed)}; leaked={leaked}",
            )
        )

    # Semantic answer-presence check (Fix B per the post-UI bug sweep).
    # `answer_must_contain` in the scenario spec lists substrings that
    # MUST appear (case-insensitive) in `response.action`. Catches the
    # "agent said 'I will look up X' but never included the actual X"
    # failure mode that the structural checks miss.
    required_substrings = _as_list(expected.get("answer_must_contain"))
    if required_substrings:
        action_lower = (orch.response.action or "").lower()
        missing = [s for s in required_substrings if s.lower() not in action_lower]
        sr.checks.append(
            CheckResult(
                name="answer_must_contain",
                passed=not missing,
                detail=(
                    f"required={required_substrings}; "
                    f"missing={missing}; "
                    f"action[:200]={(orch.response.action or '')[:200]!r}"
                ),
            )
        )

    # Inverse semantic check: substrings that MUST NOT appear in the action.
    # Catches leak vectors that the structural redaction check might miss —
    # e.g., a salary number the filter stripped from the tool output but the
    # model still emitted from training data, or a personal_email the agent
    # fabricated. Complements `answer_must_contain` for deny/escalate scenarios.
    forbidden_substrings = _as_list(expected.get("answer_must_not_contain"))
    if forbidden_substrings:
        action_lower = (orch.response.action or "").lower()
        present = [s for s in forbidden_substrings if s.lower() in action_lower]
        sr.checks.append(
            CheckResult(
                name="answer_must_not_contain",
                passed=not present,
                detail=(
                    f"forbidden={forbidden_substrings}; "
                    f"present={present}; "
                    f"action[:200]={(orch.response.action or '')[:200]!r}"
                ),
            )
        )

    # Pipeline status check. Two pass criteria per the plan (Step 13b):
    #   - STRICT (default; applied to the 21 declared problem-statement
    #     scenarios): pipeline_status == "clean" AND repair_attempts == 0.
    #     Repair indicates an upstream design defect to remediate, not a
    #     workaround to celebrate.
    #   - PERMISSIVE (opt-in via `permissive: true` in the YAML; intended
    #     for LLM-generated extras that probe edge cases): pipeline_status
    #     in {"clean", "repaired_ok"}.
    is_permissive = bool(spec.get("permissive", False))
    if is_permissive:
        passed_status = orch.pipeline_status in ("clean", "repaired_ok")
        detail_mode = "permissive"
    else:
        passed_status = (
            orch.pipeline_status == "clean"
            and len(orch.repair_attempts) == 0
        )
        detail_mode = "strict"
    sr.checks.append(
        CheckResult(
            name="pipeline_status",
            passed=passed_status,
            detail=(
                f"mode={detail_mode}; "
                f"status={orch.pipeline_status}; "
                f"repair_attempts={len(orch.repair_attempts)}; "
                f"drift_kinds={[a.drift_kinds for a in orch.repair_attempts]}"
            ),
        )
    )

    # Citation verification (D5) status
    cv = orch.citation_verification
    if cv is not None:
        # Don't fail the scenario on verification miss when no citations
        # are required (deny/escalate already covered by cited_sections).
        if orch.response.citations:
            sr.checks.append(
                CheckResult(
                    name="citations_grounded",
                    passed=cv.ok,
                    detail=(
                        f"failures={[v.status for v in cv.verdicts if v.status != 'ok']}"
                    ),
                )
            )

    sr.passed = all(c.passed for c in sr.checks)
    return sr


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(results: list[ScenarioResult]) -> str:
    n_pass = sum(1 for r in results if r.passed)
    n_total = len(results)
    lines: list[str] = []
    lines.append("# Eval Report")
    lines.append("")
    lines.append(f"**Summary:** {n_pass}/{n_total} scenarios passing.\n")
    lines.append(f"Total wall-clock: {sum(r.duration_s for r in results):.1f}s")
    lines.append("")
    lines.append(
        "| # | Tier | Decision | Pipeline | Repairs | Injection | Leak | CoVe | Checks | Time | |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    # Defense-fired aggregate counters (for the summary block below).
    n_injection_flagged = 0
    n_leaks_detected = 0
    n_cove_invoked = 0
    n_cove_misaligned = 0
    for r in results:
        if r.orchestrator is None:
            dec = "ERROR"
            status = "—"
            repairs = "—"
            inj = "—"
            leak = "—"
            cove = "—"
        else:
            dec = r.orchestrator.response.decision
            status = r.orchestrator.pipeline_status
            repairs = str(len(r.orchestrator.repair_attempts))
            inj = "✓" if (r.orchestrator.prompt_guard_verdict and r.orchestrator.prompt_guard_verdict.is_injection) else "·"
            if r.orchestrator.prompt_guard_verdict and r.orchestrator.prompt_guard_verdict.is_injection:
                n_injection_flagged += 1
            if r.orchestrator.leak_detection and r.orchestrator.leak_detection.events:
                leak = f"{len(r.orchestrator.leak_detection.events)}"
                n_leaks_detected += 1
            elif r.orchestrator.leak_detection and r.orchestrator.leak_detection.invoked:
                leak = "·"
            else:
                leak = "—"
            if r.orchestrator.cove_verdict and r.orchestrator.cove_verdict.invoked:
                cove = "✓" if r.orchestrator.cove_verdict.aligned else "✗"
                n_cove_invoked += 1
                if not r.orchestrator.cove_verdict.aligned:
                    n_cove_misaligned += 1
            else:
                cove = "·"
        passed = sum(1 for c in r.checks if c.passed)
        total = len(r.checks)
        mark = "✓" if r.passed else "✗"
        lines.append(
            f"| {r.scenario_id} | {r.tier} | {dec} | {status} | {repairs} | {inj} | {leak} | {cove} | "
            f"{passed}/{total} | {r.duration_s:.1f}s | {mark} |"
        )
    lines.append("")
    lines.append("**Defense-layer firing summary:**")
    lines.append(f"- Prompt Guard 2 flagged: **{n_injection_flagged}** of {n_total} scenarios")
    lines.append(f"- Leak detector caught events on: **{n_leaks_detected}** of {n_total} scenarios")
    lines.append(
        f"- CoVe invoked: **{n_cove_invoked}** of {n_total} scenarios "
        f"(misaligned: {n_cove_misaligned})"
    )
    lines.append("")
    for r in results:
        lines.append(f"## Scenario #{r.scenario_id} ({r.tier})")
        lines.append(f"**User:** {r.message}")
        lines.append("")
        if r.error:
            lines.append("**ERROR**")
            lines.append("```")
            lines.append(r.error[:2000])
            lines.append("```")
            continue
        assert r.orchestrator is not None
        o = r.orchestrator
        lines.append(f"**Path:** {o.architectural_path}  ")
        lines.append(f"**Pipeline status:** {o.pipeline_status}  ")
        lines.append(f"**Decision:** {o.response.decision}  ")
        lines.append(f"**Action:** {o.response.action}")
        lines.append("")
        # Defense-layer state
        defense_lines: list[str] = []
        if o.prompt_guard_verdict:
            pg = o.prompt_guard_verdict
            defense_lines.append(
                f"- Prompt Guard 2: is_injection={pg.is_injection} score={pg.score:.2f} "
                f"method={pg.method}"
            )
        if o.cove_verdict and o.cove_verdict.invoked:
            cv = o.cove_verdict
            defense_lines.append(
                f"- CoVe: aligned={cv.aligned} questions={len(cv.questions)} "
                f"divergences={len(cv.divergences)}"
            )
        if o.leak_detection:
            ld = o.leak_detection
            if ld.invoked:
                defense_lines.append(
                    f"- Leak detector: events={len(ld.events)} clean={ld.clean}"
                )
                if ld.events:
                    for ev in ld.events:
                        defense_lines.append(
                            f"  - LEAK: {ev.entity_type} span={ev.span!r} "
                            f"field={ev.source_field}"
                        )
        if defense_lines:
            lines.append("**Defense layers:**")
            lines.extend(defense_lines)
            lines.append("")
        if o.repair_attempts:
            lines.append("**Repair attempts (D13 repair loop):**")
            for ra in o.repair_attempts:
                lines.append(
                    f"- attempt {ra.attempt_index}: drifts={ra.drift_kinds}; "
                    f"detail={ra.drift_details}"
                )
            lines.append("")
        if o.response.citations:
            lines.append("**Citations:**")
            for c in o.response.citations:
                lines.append(f"- §{c.section_id}: {c.quote[:200]}{'...' if len(c.quote) > 200 else ''}")
            lines.append("")
        if o.tool_executions:
            lines.append("**Tool calls:**")
            for ex in o.tool_executions:
                lines.append(
                    f"- proposed `{ex.proposed['name']}({json.dumps(ex.proposed['args'])})` "
                    f"→ dispatch=**{ex.dispatch.status}**"
                    + (f" — {ex.dispatch.rejection_reason}" if ex.dispatch.rejection_reason else "")
                )
                if ex.filter is not None:
                    kept = sorted(ex.filter.filtered_output.keys())
                    redacted = sorted(ex.filter.redacted_fields)
                    lines.append(
                        f"   - filter (relationship={ex.relationship}): kept={kept}; redacted={redacted}"
                    )
            lines.append("")
        if o.response.cost_assessment:
            ca = o.response.cost_assessment
            lines.append(
                f"**Cost assessment:** harm_act={ca.harm_if_acted_wrongly}, "
                f"harm_refuse={ca.harm_if_refused_wrongly}, "
                f"reversibility={ca.reversibility}, "
                f"chosen={ca.chosen_path}"
            )
            lines.append("")
        lines.append(f"**Reasoning:** {o.response.reasoning}")
        lines.append("")
        lines.append("**Checks:**")
        for c in r.checks:
            mark = "✓" if c.passed else "✗"
            lines.append(f"- {mark} `{c.name}` — {c.detail}")
        lines.append("")
        if o.consistency_review and o.consistency_review.drifts:
            lines.append("**Drifts in final response (D13 post-repair):**")
            for d in o.consistency_review.drifts:
                lines.append(f"- {d.kind} [{d.category}]: {d.detail}")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scenarios",
        default=str(SCENARIOS_PATH),
        help=f"Path to scenarios YAML (default {SCENARIOS_PATH})",
    )
    ap.add_argument(
        "--report",
        default=str(DEFAULT_REPORT_PATH),
        help=f"Where to write the markdown report (default {DEFAULT_REPORT_PATH})",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="Run a small representative subset (faster; for pre-commit).",
    )
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated scenario IDs to run (e.g. '1,7,17').",
    )
    args = ap.parse_args(argv)

    raw = yaml.safe_load(Path(args.scenarios).read_text())
    scenarios = raw["scenarios"]

    if args.smoke:
        scenarios = [s for s in scenarios if int(s["id"]) in SMOKE_IDS]
    if args.only:
        ids = {int(x.strip()) for x in args.only.split(",") if x.strip()}
        scenarios = [s for s in scenarios if int(s["id"]) in ids]

    # Pace scenarios so we don't burst past Gemini's 20 req/min free-tier
    # cap. ~4s between scenarios keeps us under the limit even with the
    # agent's own retries.
    pace_seconds = float(os.environ.get("EVAL_SCENARIO_PACE_SECONDS", "4.0"))
    results: list[ScenarioResult] = []
    for i, spec in enumerate(scenarios):
        sid = spec["id"]
        print(f"[scenario {sid}] {spec['tier']} — {spec['message'][:80]}{'...' if len(spec['message']) > 80 else ''}")
        if i > 0 and pace_seconds > 0:
            time.sleep(pace_seconds)
        r = evaluate_scenario(spec)
        results.append(r)
        if r.passed:
            print(f"  ✓ PASS ({len(r.checks)} checks, {r.duration_s:.1f}s)")
        else:
            print(f"  ✗ FAIL ({sum(1 for c in r.checks if c.passed)}/{len(r.checks)} checks, {r.duration_s:.1f}s)")
            for c in r.checks:
                if not c.passed:
                    print(f"    - {c.name}: {c.detail}")
            if r.error:
                print(f"    - error: {r.error.splitlines()[0]}")

    report = render_report(results)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    n_pass = sum(1 for r in results if r.passed)
    print(f"\n{n_pass}/{len(results)} scenarios passing. Report: {report_path.relative_to(REPO_ROOT)}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
