"""
TESTS-FOR: the 21 problem-statement scenarios end-to-end.

Each scenario is run through `policy_agent.eval.evaluate_scenario()`,
which composes orchestrator.handle_request() and applies the per-scenario
expectations declared in tests/scenarios.yaml (action_class, required /
forbidden tool_calls, cited_sections, redacted_fields, answer_must_contain,
pipeline_status). A scenario is green only when every check passes.

Markers: `llm` + `blackbox`. The full suite makes real LLM calls and is
paced via EVAL_SCENARIO_PACE_SECONDS so we stay under provider rate
limits.

Usage:
  pytest -m "not llm"                       # skip LLM-bound tests
  pytest tests/blackbox/test_scenarios.py   # run all 21
  pytest tests/blackbox/test_scenarios.py -k "scenario-1-"   # one
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from policy_agent.eval import evaluate_scenario


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCENARIOS_PATH = _REPO_ROOT / "tests" / "scenarios.yaml"


def _load_scenarios() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_SCENARIOS_PATH.read_text())
    return list(raw["scenarios"])


_SCENARIOS = _load_scenarios()


def _scenario_id(spec: dict[str, Any]) -> str:
    return f"scenario-{spec['id']}-{spec['tier']}"


# A module-level counter so we can pace consecutive scenarios in one
# pytest run without serializing them through a fixture parameter.
_TEST_INDEX = {"n": 0}


@pytest.fixture(autouse=True)
def _pace_between_scenarios():
    """Sleep before each scenario after the first, matching the cadence
    used by the eval CLI (`EVAL_SCENARIO_PACE_SECONDS`, default 4.0)."""
    pace = float(os.environ.get("EVAL_SCENARIO_PACE_SECONDS", "4.0"))
    if _TEST_INDEX["n"] > 0 and pace > 0:
        time.sleep(pace)
    _TEST_INDEX["n"] += 1
    yield


@pytest.mark.llm
@pytest.mark.blackbox
@pytest.mark.parametrize("spec", _SCENARIOS, ids=_scenario_id)
def test_scenario(spec: dict[str, Any]):
    result = evaluate_scenario(spec)

    if result.passed:
        return

    # Build a triage-friendly assertion message that surfaces failed
    # checks and the raw error (if any) without forcing the user back
    # to the CLI runner.
    lines: list[str] = [
        f"\nScenario #{result.scenario_id} ({result.tier}) FAILED:",
        f"  user_message: {result.message!r}",
    ]
    if result.orchestrator is not None:
        lines.append(f"  decision: {result.orchestrator.response.decision}")
        lines.append(f"  pipeline_status: {result.orchestrator.pipeline_status}")
        lines.append(
            f"  authorized_tools: {result.orchestrator.authorized_tool_names}"
        )
        cited = [c.section_id for c in result.orchestrator.response.citations]
        lines.append(f"  cited_sections: {cited}")
        if result.orchestrator.repair_attempts:
            lines.append(
                f"  repair_attempts: {[a.drift_kinds for a in result.orchestrator.repair_attempts]}"
            )
        lines.append(f"  action[:240]: {result.orchestrator.response.action[:240]!r}")
    if result.error:
        lines.append(f"  error: {result.error.splitlines()[0]}")
    lines.append("  failed checks:")
    for c in result.checks:
        if not c.passed:
            lines.append(f"    - {c.name}: {c.detail}")
    pytest.fail("\n".join(lines))
